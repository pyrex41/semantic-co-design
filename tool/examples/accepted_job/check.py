"""AcceptedJob on the kernel. Run: nix develop -c python3 -m tool check accepted_job

Regenerate the retained mutant evidence with: python3 -m tool record accepted_job
"""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ... import check, maude, sim
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
    report("accepted_job: ok")
