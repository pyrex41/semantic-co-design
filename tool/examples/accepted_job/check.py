"""AcceptedJob on the kernel. Run: nix develop -c python3 -m tool check accepted_job

Regenerate the retained mutant evidence with: python3 -m tool record accepted_job
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from ... import check, maude, sim
from ...sim import hegel as generated
from ...check import CheckFailure, require
from ...compile import build, run_lines
from ...schema import Schema
from .workload import KEY, Clients, maude as maude_clients

HERE = Path(__file__).parent
SEEDS = 500
SEEDED = sim.profile(budget={"crash": 2, "drop": 2, "dup": 1}, max_steps=200)
SUBMISSIONS = 6
# The exhaustive bound: one key, two digests, two submissions, one crash, one loss.
BOUND = sim.profile(timed=False, budget={"crash": 1, "drop": 1, "dup": 0})
BOUND_SUBMISSIONS = 2
HEGEL_CASES, HEGEL_SEED = 200, 0
# What Hegel shrinks each mutant's first failure to; a longer result fails the check.
HEGEL_SHRUNK = {"reply-before-ack": 5, "insert-without-key-check": 11,
                "reload-ignores-durable-row": 12, "no-conflict-clause": 9}
# Settings Hegel would take from its environment. Each changed a run until falsify pinned it.
HOSTILE = {"HEGEL_PHASES": "generate", "HEGEL_BACKEND": "urandom", "HEGEL_SEED": "7",
           "HEGEL_STOP_ON_FIRST_FAILURE": "false", "HEGEL_REPORT_MULTIPLE_FAILURES": "true",
           "HEGEL_TEST_CASES": "3", "HEGEL_DERANDOMIZE": "true", "CI": "true"}
WITNESSES = ["wit.retry-after-lost-reply", "wit.retry-after-precommit-crash",
             "wit.conflict-rejected"]

# Each mutant is one edit of spec.shen and the invariant that must reject it.
MUTANTS = {
    "reply-before-ack": {
        "old": "[[write Next Slot Key Digest]]",
        "new": "[[write Next Slot Key Digest] [reply Req ok Slot]]",
        "invariant": "inv.ok-is-durable"},
    "insert-without-key-check": {
        "old": "(job.decide (job.find Key Jobs 0) [Jobs Pending Next] Req Key Digest)",
        "new": "(job.decide [-1 0] [Jobs Pending Next] Req Key Digest)",
        "invariant": "inv.one-row-per-key"},
    "reload-ignores-durable-row": {
        "old": "Slot 1 Key Digest -> [(job.put Slot [stored Key Digest] Jobs) Pending Next]",
        "new": "Slot 1 Key Digest -> [(job.put Slot [free 0 0] Jobs) Pending Next]",
        "invariant": "inv.rows-are-immutable"},
}
# For the generated schedules only: a step with no result, which no invariant names.
TRAP_MUTANT = {
    "old": "\n  [Slot _] State Req _ _ -> [State [[reply Req conflict Slot]]])",
    "new": ")"}


def mutate(source, mutant):
    require(source.count(mutant["old"]) == 1, f"mutation site not unique: {mutant['old']}")
    return source.replace(mutant["old"], mutant["new"])


# ---- predicates over observations (durable rows, delivered and dropped outputs, lost writes)

def parts(observation):
    rows = next(t[2] for t in observation if t[0] == "rows")
    history = next(t[3] for t in observation if t[0] == "client")
    digest = lambda request: history[len(history) - 1 - request]
    named = lambda name: [t[1] for t in observation if t[0] == name]
    return rows, digest, named("got"), named("dropped"), named("lostw")


def safe(observation):
    rows, digest, got, dropped, _ = parts(observation)
    if len({row[1] for row in rows}) != len(rows):
        return False
    for reply in got + dropped:
        if reply[0] != "reply":
            continue
        _, request, status, receipt = reply
        if status == "ok" and (receipt, KEY, digest(request)) not in rows:
            return False
        if status == "conflict" and not any(
                row[0] == receipt and row[2] != digest(request) for row in rows):
            return False
    return True


def lost_reply_retry(observation):
    rows, digest, got, dropped, _ = parts(observation)
    return len(rows) == 1 and any(
        a[0] == b[0] == "reply" and a[2] == b[2] == "ok" and a[3] == b[3] and a[1] < b[1]
        and digest(a[1]) == digest(b[1]) for a in dropped for b in got)


def precommit_crash_retry(observation):
    rows, _, got, _, lost = parts(observation)
    return len(rows) == 1 and bool(lost) and any(r[0] == "reply" and r[2] == "ok" for r in got)


def conflict_rejected(observation):
    return any(r[0] == "reply" and r[2] == "conflict" for r in parts(observation)[2])


# ---- the check

def violations(schema, traces):
    return [[name for name, _ in bad] for bad in check.invariants(schema, traces)]


def seeded(native, inits, seeds):
    return [sim.run(native, Clients(SUBMISSIONS), SEEDED, inits, seed) for seed in seeds]


def specification(work, schema, report):
    native = build(schema, work / "spec")
    inits = check.inits_of(schema, 1)
    report(f"compiled spec.shen: {native.stats}")

    traces = seeded(native, inits, range(SEEDS))
    ends = sorted({t["end"] for t in traces})
    require(ends == ["quiescent"], f"simulation: runs ended {ends}")
    steps = sum(len(t["steps"]) for t in traces)
    kinds = {}
    for trace in traces:
        for step in trace["steps"]:
            name = "crash" if step.get("crashed") else step["kind"]
            kinds[name] = kinds.get(name, 0) + 1
    report(f"simulation: {SEEDS} seeds, {steps} steps, all quiescent; {dict(sorted(kinds.items()))}")

    # Same seed, same bytes; and a recorded trace replays without its seed.
    again = seeded(native, inits, range(SEEDS))
    require([sim.dumps(t) for t in again] == [sim.dumps(t) for t in traces],
            "determinism: a seed produced different trace bytes on a second run")
    for trace in traces[:50]:
        sim.replay(native, Clients(SUBMISSIONS), inits, trace)
    combined = hashlib.sha256("".join(sim.digest(t) for t in traces).encode()).hexdigest()
    report(f"determinism: {SEEDS} seeds rerun byte-identical, 50 traces replayed from their "
           f"records; sha256 over all trace digests {combined[:16]}")
    # Teeth: a trace with one recorded value changed, and one with a step removed, must not replay.
    victim = next(t for t in traces if any(s["kind"] == "sync" for s in t["steps"]))
    edited = copy.deepcopy(victim)
    step = next(s for s in edited["steps"] if s["kind"] == "sync")
    step["world"]["disks"][0][0][2] += 1
    removed = copy.deepcopy(victim)
    removed["steps"].remove(next(s for s in removed["steps"] if s["kind"] == "sync"))
    for label, broken in (("edited", edited), ("shortened", removed)):
        try:
            sim.replay(native, Clients(SUBMISSIONS), inits, broken)
        except sim.Divergence:
            continue
        raise CheckFailure(f"determinism: an {label} trace replayed without complaint")
    report("determinism teeth: an edited trace and a shortened trace are both rejected on replay")

    pairs = check.refinement(schema, traces, inits)
    vectors = [schema.encode_input(schema.encode_state(state), event)
               for state, event in check.step_pairs(traces)]
    require(run_lines(native.executable, vectors) == [native.raw(v) for v in vectors],
            "abi: the line executable and the shared library disagree")
    report(f"refinement: {steps} simulated steps reduce to {pairs} distinct (state, event) "
           f"pairs; native equals shen-jit on all of them; the line executable agrees")

    bad = [(n, names) for n, names in enumerate(violations(schema, traces)) if names]
    require(not bad, f"invariants: violated on seeds {bad[:5]}")
    found = check.predicates(schema.directory / schema.invariants["file"], WITNESSES, traces)
    counts = {name: sum(row[n] is True for row in found) for n, name in enumerate(WITNESSES)}
    require(all(counts.values()), f"witnesses: a required history never occurred: {counts}")
    names = schema.invariants["state"] + schema.invariants["trace"]
    report(f"invariants: {', '.join(names)} hold on all {SEEDS} traces")
    report(f"witnesses by simulation (seeds showing each history): {counts}")
    return native, inits, traces


def bounded(schema, native, inits, traces, report):
    modules = maude.step_module(schema.source)
    pairs = check.maude_refinement(modules, schema, traces)
    report(f"maude step: the translated module reduces all {pairs} simulated pairs to the "
           f"native result")
    explored, reached, facts = check.maude_agreement(
        schema, native, Clients(BOUND_SUBMISSIONS), maude_clients(BOUND_SUBMISSIONS), BOUND, inits)
    require(facts["maude_trap"] is None, f"maude: a reachable step traps: {facts['maude_trap']}")
    require(explored == reached, f"maude and simulator disagree: {len(reached - explored)} "
                                 f"observations only in Maude, {len(explored - reached)} only "
                                 f"in the simulator")
    report(f"bounded search ({BOUND_SUBMISSIONS} submissions, budget {BOUND['budget']}): Maude "
           f"{facts['maude_states']} states, simulator {facts['sim_states']} states; both reach "
           f"the same {len(reached)} observations")
    require(all(map(safe, reached)), "bounded search: an unsafe observation is reachable")
    counts = {f.__name__: sum(map(f, reached))
              for f in (lost_reply_retry, precommit_crash_retry, conflict_rejected)}
    require(all(counts.values()), f"bounded search: a required history is unreachable: {counts}")
    report(f"bounded search: every reachable observation is safe; witnesses reachable: {counts}")
    return explored


def mutants(work, schema, native, inits, explored, report, record):
    retained_path = HERE / "mutants.json"
    retained = {} if record else json.loads(retained_path.read_text())
    for name, mutant in MUTANTS.items():
        source = mutate(schema.source, mutant)
        broken = build(schema, work / name, source)
        wanted = mutant["invariant"]
        fails = lambda traces: [wanted in names for names in violations(schema, traces)]
        if record:
            seed = next(s for s in range(5000) if fails(seeded(broken, inits, [s]))[0])
            trace = seeded(broken, inits, [seed])[0]
            minimal = check.shrink(broken, Clients(SUBMISSIONS), inits, trace, fails)
            (HERE / "mutants").mkdir(exist_ok=True)
            (HERE / "mutants" / f"{name}.trace.json").write_text(sim.dumps(minimal))
            retained[name] = {"seed": seed, "invariant": wanted, "sha256": sim.digest(trace),
                              "steps": len(trace["steps"]),
                              "minimal_steps": len(minimal["steps"]),
                              "minimal_sha256": sim.digest(minimal)}
        kept = retained[name]
        require(kept["invariant"] == wanted, f"{name}: retained record is for another invariant")
        trace = seeded(broken, inits, [kept["seed"]])[0]
        require(sim.digest(trace) == kept["sha256"],
                f"{name}: seed {kept['seed']} no longer reproduces the retained trace")
        require(fails([trace])[0], f"{name}: seed {kept['seed']} does not violate {wanted}")
        clean = seeded(native, inits, [kept["seed"]])
        require(violations(schema, clean) == [[]], f"{name}: the unedited spec fails the same seed")
        minimal = json.loads((HERE / "mutants" / f"{name}.trace.json").read_text())
        require(sim.digest(minimal) == kept["minimal_sha256"], f"{name}: minimal trace changed")
        sim.replay(broken, Clients(SUBMISSIONS), inits, minimal)
        require(fails([minimal])[0], f"{name}: the minimal trace does not violate {wanted}")
        shorter = [sim.script(broken, Clients(SUBMISSIONS), minimal["profile"], inits, actions)
                   for actions in one_step_removed(minimal, broken, inits)]
        require(not any(fails(shorter)), f"{name}: the retained trace is not 1-minimal")
        # The refinement check must reject this binary against the unedited source.
        try:
            check.refinement(schema, [trace, minimal], inits)
        except CheckFailure:
            pass
        else:
            raise CheckFailure(f"{name}: refinement accepted a binary built from the mutant")
        # Bounded search on the mutant: Maude and the simulator still agree with each other,
        # an unsafe observation is reachable, and the set differs from the specification's.
        mine, reached, _ = check.maude_agreement(
            schema, broken, Clients(BOUND_SUBMISSIONS), maude_clients(BOUND_SUBMISSIONS), BOUND,
            inits, source=source)
        require(mine == reached, f"{name}: Maude and the simulator disagree on the mutant")
        unsafe = sum(not safe(o) for o in reached)
        require(unsafe and reached != explored, f"{name}: bounded search does not catch it")
        report(f"mutant {name}: {wanted} fails at seed {kept['seed']} "
               f"({kept['steps']} steps, sha256 {kept['sha256'][:16]}); minimal trace "
               f"{kept['minimal_steps']} steps replays and still fails; refinement rejects the "
               f"binary; bounded search reaches {unsafe} unsafe observations")
    if record:
        retained_path.write_text(json.dumps(retained, indent=2, sort_keys=True) + "\n")


@contextmanager
def environment(**values):
    """Run with these variables set; the old values come back afterwards."""
    old = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in old.items():
            os.environ.pop(name) if value is None else os.environ.update({name: value})


def unshrunk(found, limit):
    """Why `found` is not a shrunk failure, or [] when it is one."""
    first, trace = len(found["first"]["steps"]), len(found["trace"]["steps"])
    return ([f"{trace} steps, the first failure had {first}"] * (trace >= first)
            + [f"{trace} steps, more than the retained {limit}"] * (trace > limit)
            + ["no run after the first failure"] * (found["executions"] <= found["cases"]))


def rejected(replay, why):
    try:
        replay()
    except sim.Divergence:
        return
    raise CheckFailure(why)


def properties(work, schema, native, inits, report):
    """Hegel generates the schedules, faults and workload, and shrinks what fails."""
    clients = Clients(SUBMISSIONS)
    run = lambda binary, names=None, **more: generated.falsify(
        binary, clients, SEEDED, inits, invariants=names, cases=HEGEL_CASES, seed=HEGEL_SEED,
        **more)
    same = lambda a, b: (a["trace"] is not None and b["trace"] is not None
                         and sim.dumps(a["trace"]) == sim.dumps(b["trace"])
                         and a["executions"] == b["executions"])
    healthy = run(native)
    require(healthy["trace"] is None, f"hegel: the specification violates {healthy['violated']}")
    require(healthy["cases"] == HEGEL_CASES and not healthy["exhausted"],
            f"hegel: ran {healthy['cases']} cases")
    report(f"hegel: {healthy['cases']} generated schedules of spec.shen "
           f"({healthy['evaluations']} distinct), all invariants hold; {healthy['seconds']:.1f}s")
    # A run that covers less than it was asked for says so.
    none = generated.falsify(native, clients, sim.profile(max_steps=0), inits, cases=100)
    require(none["exhausted"] and none["cases"] == 1 and none["trace"] is None,
            f"hegel: a profile with one schedule ran {none['cases']} cases unremarked")
    try:
        generated.falsify(native, clients, SEEDED, inits, cases=0)
    except ValueError:
        pass
    else:
        raise CheckFailure("hegel: zero cases passed as a run")
    retained = json.loads((HERE / "mutants.json").read_text())
    for name, mutant in MUTANTS.items():
        broken = build(schema, work / f"{name}.hegel", mutate(schema.source, mutant))
        wanted = mutant["invariant"]
        fails = lambda traces: [wanted in names for names in violations(schema, traces)]
        found = run(broken, [wanted])
        trace, first = found["trace"], found["first"]
        require(trace is not None, f"hegel {name}: no failing schedule in {HEGEL_CASES} cases")
        require(found["failure"] == wanted, f"hegel {name}: found {found['failure']}")
        require(trace["format"] == sim.FORMAT, f"hegel {name}: not a simulator trace")
        again = sim.replay(broken, clients, inits, trace)
        require(sim.dumps(again) == sim.dumps(trace), f"hegel {name}: replay changed the bytes")
        require(fails([again, first]) == [True, True],
                f"hegel {name}: the first and the shrunk trace do not both violate {wanted}")
        # Teeth: these steps are the mutant's, so the unedited binary must not replay them.
        rejected(lambda: sim.replay(native, clients, inits, trace),
                 f"hegel {name}: the shrunk trace replays on the unedited spec")
        problems = unshrunk(found, HEGEL_SHRUNK[name])
        require(not problems, f"hegel {name}: not shrunk: {problems}")
        second = run(broken, [wanted])
        require(same(found, second), f"hegel {name}: the same seed gave a different shrunk trace")
        if name == "reply-before-ack":
            # Teeth: with Hegel's shrink phase off the failure is found and not shrunk, and
            # the test above says so.
            raw = run(broken, [wanted], shrink=False)
            require(sim.dumps(raw["trace"]) == sim.dumps(first), f"hegel {name}: shrunk anyway")
            require(unshrunk(raw, HEGEL_SHRUNK[name]),
                    f"hegel {name}: an unshrunk failure passed as shrunk")
            # Teeth: Hegel's own configuration channels must not reach the run.
            with environment(**HOSTILE, HEGEL_DATABASE=str(work / "examples")):
                third = run(broken, [wanted])
            require(same(found, third) and not (work / "examples").exists(),
                    f"hegel {name}: HEGEL_* variables changed the run")
        # The hand shrinker on the same first failure, next to its retained minimum from a seed.
        by_hand = check.shrink(broken, clients, inits, first, fails)
        report(f"hegel mutant {name}: {wanted} fails at case {found['cases']} "
               f"({len(first['steps'])} steps); Hegel shrinks it to {len(trace['steps'])} steps "
               f"in {found['executions']} runs, {found['seconds']:.1f}s (hand shrinker: "
               f"{len(by_hand['steps'])} steps from the same failure, "
               f"{retained[name]['minimal_steps']} retained); replays byte for byte and still "
               f"fails; a second run gives the same trace, sha256 {sim.digest(trace)[:16]}")
    traps(work, schema, native, inits, run, same, report)
    two_faults(work, schema, inits, run, report)


def traps(work, schema, native, inits, run, same, report):
    """A step with no result. A trace has no record of the step that trapped, so falsify
    returns that action beside it and generated.replay executes it after the others."""
    clients, name = Clients(SUBMISSIONS), "no-conflict-clause"
    broken = build(schema, work / "trap.hegel", mutate(schema.source, TRAP_MUTANT))
    found = run(broken, [])
    trace, trap = found["trace"], found["trap"]
    require(found["failure"] == generated.TRAP and found["violated"] == [generated.TRAP]
            and trace["end"].startswith("error: ") and found["first"]["end"].startswith("error: "),
            f"hegel {name}: expected a trap, got {found['failure']}")
    problems = unshrunk(found, HEGEL_SHRUNK[name])
    require(not problems, f"hegel {name}: not shrunk: {problems}")
    require(same(found, run(broken, [])), f"hegel {name}: the same seed gave a different trace")
    generated.replay(broken, clients, inits, trace, trap)
    # Teeth: the steps before the trap are also the specification's, so replaying only them
    # proves nothing. The trapping step must trap here, with this error, and nowhere else.
    sim.replay(native, clients, inits, trace)
    rejected(lambda: generated.replay(native, clients, inits, trace, trap),
             f"hegel {name}: the unedited spec traps on the same step")
    made_up = {**trace, "end": "error: made up"}
    rejected(lambda: generated.replay(broken, clients, inits, made_up, trap),
             f"hegel {name}: replay accepted an error the step does not raise")
    rejected(lambda: generated.replay(broken, clients, inits, trace),
             f"hegel {name}: replay accepted an error trace without its trapping step")
    report(f"hegel mutant {name}: a trap at case {found['cases']} "
           f"({len(found['first']['steps'])} steps); Hegel shrinks it to {len(trace['steps'])} "
           f"steps in {found['executions']} runs, {found['seconds']:.1f}s; the trapping step "
           f"{trap[0]} replays to the same error on the mutant and is rejected on the "
           f"unedited spec; {trace['end']}")


def two_faults(work, schema, inits, run, report):
    """Two faults in one binary: the failure found first is the one that is shrunk."""
    clients = Clients(SUBMISSIONS)
    kinds = lambda found: [(t["end"] or "").startswith("error: ")
                           for t in (found["first"], found["trace"])]
    # A trap and an invariant. Asked for the invariant, the trap comes first.
    wanted = MUTANTS["reply-before-ack"]["invariant"]
    source = mutate(mutate(schema.source, MUTANTS["reply-before-ack"]), TRAP_MUTANT)
    broken = build(schema, work / "two.trap.hegel", source)
    both = run(broken, [wanted])
    require(both["failure"] == generated.TRAP and kinds(both) == [True, True],
            f"hegel two faults: {both['failure']} with error traces {kinds(both)}")
    generated.replay(broken, clients, inits, both["trace"], both["trap"])
    only = run(broken, [wanted], traps=False)
    require(only["failure"] == wanted and kinds(only) == [False, False] and only["trapped"]
            and violations(schema, [only["first"], only["trace"]]) == [[wanted], [wanted]]
            and not unshrunk(only, len(only["first"]["steps"])),
            f"hegel two faults: with traps excluded, {only['failure']}")
    sim.replay(broken, clients, inits, only["trace"])
    # Two invariants, all of them asked for.
    names = ["reply-before-ack", "insert-without-key-check"]
    source = mutate(mutate(schema.source, MUTANTS[names[0]]), MUTANTS[names[1]])
    broken = build(schema, work / "two.invariants.hegel", source)
    pair = run(broken)
    first, shrunk = violations(schema, [pair["first"], pair["trace"]])
    require(pair["failure"] in first and pair["failure"] in shrunk and shrunk == pair["violated"],
            f"hegel two faults: {pair['failure']} pursued, first {first}, shrunk {shrunk}")
    sim.replay(broken, clients, inits, pair["trace"])
    size = lambda found: f"{len(found['first']['steps'])} to {len(found['trace']['steps'])} steps"
    report(f"hegel two faults: asked for {wanted}, a spec that also traps fails first as "
           f"{both['failure']} and stays that failure ({size(both)}); with traps excluded it "
           f"fails as {only['failure']} ({size(only)}, {only['trapped']} trapping runs not "
           f"counted as failures); {' with '.join(names)} first violates {first} and is "
           f"shrunk as {pair['failure']} ({size(pair)})")


def one_step_removed(trace, native, inits):
    actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
    for n in range(len(actions)):
        shorter = actions[:n] + actions[n + 1:]
        try:
            sim.script(native, Clients(SUBMISSIONS), trace["profile"], inits, shorter)
        except sim.Divergence:
            continue
        yield shorter


def main(report=print, record=False):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        native, inits, traces = specification(work, schema, report)
        explored = bounded(schema, native, inits, traces, report)
        mutants(work, schema, native, inits, explored, report, record)
        properties(work, schema, native, inits, report)
    report("accepted_job: ok")
