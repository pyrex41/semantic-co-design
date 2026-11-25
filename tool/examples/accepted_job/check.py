"""AcceptedJob on the kernel. Run: nix develop -c python3 -m tool check accepted_job

Regenerate the retained mutant evidence with: python3 -m tool record accepted_job
"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ... import check, maude, sim
from ...check import CheckFailure, require
from ...compile import build, run_lines
from ...schema import Schema
from ...sim import mutants as worlds
from .workload import Clients, maude as maude_clients

HERE = Path(__file__).parent
SEEDS = 500
SEEDED = sim.profile(budget={"crash": 2, "drop": 2, "dup": 1}, max_steps=200)
SUBMISSIONS = 8
CAPACITY = 2
WITNESSES = ["wit.retry-after-lost-reply", "wit.retry-after-precommit-crash",
             "wit.conflict-rejected", "wit.table-full"]

# The exhaustive bounds. Each is (clients, Maude clients, profile).
TWO_KEYS = dict(submissions=2, keys=(7, 8))
ONE_KEY = dict(submissions=2, keys=(7,))
THREE_KEYS = dict(submissions=3, keys=(7, 8, 9), digests=(11,))
BOUNDS = {
    # Two keys, two digests, two submissions, one crash, one lost message.
    "two keys": (TWO_KEYS, sim.profile(timed=False, budget={"crash": 1, "drop": 1, "dup": 0})),
    # One key, with a duplicated message as well.
    "duplicate": (ONE_KEY, sim.profile(timed=False, budget={"crash": 1, "drop": 1, "dup": 1})),
    # Three keys for two receipts, no faults: the only way to reach `full`.
    "table full": (THREE_KEYS, sim.profile(timed=False, budget={"crash": 0, "drop": 0, "dup": 0})),
}
# Where the specification's reliance on the environment is shown: two crashes, one key.
RELIANCE = (ONE_KEY, sim.profile(timed=False, budget={"crash": 2, "drop": 0, "dup": 0}))

# Each mutant is one edit of spec.shen and the invariant that must reject it. `witness`
# names a required history that the mutant must make impossible on every seed.
MUTANTS = {
    "reply-before-ack": {
        "old": "[[write 0 Slot Key Digest]]",
        "new": "[[write 0 Slot Key Digest] [reply Req ok Slot]]",
        "invariant": "inv.ok-is-durable"},
    "insert-without-key-check": {
        "old": "(job.decide (job.find Key Jobs 0) [Jobs Pending] Req Key Digest)",
        "new": "(job.decide [-1 0] [Jobs Pending] Req Key Digest)",
        "invariant": "inv.one-row-per-key"},
    "reload-ignores-durable-row": {
        "old": "Slot 1 Key Digest -> [(job.put Slot [stored Key Digest] Jobs) Pending]",
        "new": "Slot 1 Key Digest -> [(job.put Slot [free 0 0] Jobs) Pending]",
        "invariant": "inv.rows-are-immutable"},
    "find-ignores-key": {
        "old": "Key [[stored Key Digest] | _] Slot -> [Slot Digest]",
        "new": "Key [[stored _ Digest] | _] Slot -> [Slot Digest]",
        "invariant": "inv.ok-is-durable"},
    "reply-names-receipt-0": {
        "old": "[idle 0 0 0 0]] [[reply Req ok Slot]]]",
        "new": "[idle 0 0 0 0]] [[reply Req ok 0]]]",
        "invariant": "inv.ok-is-durable"},
    "full-instead-of-second-receipt": {
        "old": "[_ | Jobs] Slot -> (job.free Jobs (+ Slot 1)))",
        "new": "[_ | Jobs] Slot -> -1)",
        "invariant": "inv.full-is-real"},
    "conflict-for-same-digest": {
        "old": "[Slot Digest] State Req _ Digest -> [State [[reply Req ok Slot]]]",
        "new": "[Slot Digest] State Req _ Digest -> [State [[reply Req conflict Slot]]]",
        "invariant": "inv.conflict-is-real", "witness": "wit.retry-after-lost-reply"},
    "ok-for-other-digest": {
        "old": "[Slot _] State Req _ _ -> [State [[reply Req conflict Slot]]]",
        "new": "[Slot _] State Req _ _ -> [State [[reply Req ok Slot]]]",
        "invariant": "inv.ok-is-durable", "witness": "wit.conflict-rejected"},
}


def mutate(source, mutant):
    require(source.count(mutant["old"]) == 1, f"mutation site not unique: {mutant['old']}")
    return source.replace(mutant["old"], mutant["new"])


def clients(submissions=SUBMISSIONS, **scope):
    return Clients(submissions, **scope)


# ---- predicates over observations (durable rows, delivered and dropped outputs, lost writes)

def parts(observation):
    rows = next(t[2] for t in observation if t[0] == "rows")
    history = next(t[3] for t in observation if t[0] == "client")
    request = lambda number: history[len(history) - 1 - number]         # (key, digest)
    named = lambda name: [t[1] for t in observation if t[0] == name]
    return rows, request, named("got"), named("dropped"), named("lostw")


def safe(observation):
    rows, request, got, dropped, _ = parts(observation)
    if len({row[1] for row in rows}) != len(rows):
        return False
    for reply in got + dropped:
        if reply[0] != "reply":
            continue
        _, number, status, receipt = reply
        key, digest = request(number)
        if status == "ok" and (receipt, key, digest) not in rows:
            return False
        if status == "conflict" and not any(
                row[0] == receipt and row[1] == key and row[2] != digest for row in rows):
            return False
        if status == "full" and not (len(rows) == CAPACITY and all(row[1] != key for row in rows)):
            return False
    return True


def lost_reply_retry(observation):
    rows, request, got, dropped, _ = parts(observation)
    return any(a[0] == b[0] == "reply" and a[2] == b[2] == "ok" and a[3] == b[3] and a[1] < b[1]
               and request(a[1]) == request(b[1])
               and sum(row[1] == request(a[1])[0] for row in rows) == 1
               for a in dropped for b in got)


def precommit_crash_retry(observation):
    """A write for some key was lost in a crash, and still one row for that key is durable
    and a request for it was answered ok."""
    rows, request, got, _, lost = parts(observation)
    return any(sum(row[1] == gone[1] for row in rows) == 1
               and any(r[0] == "reply" and r[2] == "ok" and request(r[1])[0] == gone[1]
                       for r in got)
               for gone in lost)


def conflict_rejected(observation):
    return any(r[0] == "reply" and r[2] == "conflict" for r in parts(observation)[2])


def table_full(observation):
    return any(r[0] == "reply" and r[2] == "full" for r in parts(observation)[2])


# ---- the check

def violations(schema, traces):
    return [[name for name, _ in bad] for bad in check.invariants(schema, traces)]


def seeded(native, seeds, *, profile=SEEDED, world=sim.World, **scope):
    return [sim.run(native, clients(**scope), profile, seed, world=world) for seed in seeds]


def witnessed(schema, traces):
    found = check.predicates(schema.directory / schema.invariants["file"], WITNESSES, traces)
    return {name: sum(row[n] is True for row in found) for n, name in enumerate(WITNESSES)}


def crash_points(traces):
    """Where on the commit path nodes died, read from the world before and after each step.

    Nothing here trusts the `applied`, `persisted` or `lost_writes` fields of a record: a
    write counts as issued when it is in the disk queue, a reply as sent when it is in
    flight, and a pending row as durable when it is on the disk afterwards.
    """
    points = dict.fromkeys(("before the write", "write pending, lost",
                            "write pending, durable unacknowledged", "between ack and reply"), 0)
    for trace in traces:
        for before, step in zip(trace["steps"], trace["steps"][1:]):
            if not step.get("crashed"):
                continue
            node, old = step["node"], before["world"]
            at_death = step.get("before_crash", old)
            durable = [row in step["world"]["disks"][node] for row in at_death["pending"][node]]
            points["write pending, lost"] += not all(durable)
            points["write pending, durable unacknowledged"] += any(durable)
            if "before_crash" in step:
                tags = [effect[0] for effect in step["effects"]]
                points["before the write"] += (
                    step["event"][0] == "submit" and "write" in tags
                    and at_death["pending"] == old["pending"])
                points["between ack and reply"] += (
                    step["event"][0] == "written" and "reply" in tags
                    and at_death["outputs"] == old["outputs"])
    return points


# A node that answers a retry `ok`, twice, while the row is still only pending.
EAGER = {"old": "  _ [Jobs [writing | Rest]] _ _ _ -> [[Jobs [writing | Rest]] []]",
         "new": "  _ [Jobs [writing R Slot K D]] Req _ _ ->\n"
                "    [[Jobs [writing R Slot K D]] [[reply Req ok Slot] [reply Req ok Slot]]]"}


def judged_before_the_crash(work, schema, report):
    """A reply is judged against the disk as it was when the reply was made.

    The node dies after the first of two premature replies, and the crash then happens to
    make the pending row durable. Seen only after the crash, the reply would look durable.
    """
    broken = build(schema, work / "eager", mutate(schema.source, EAGER))
    untimed = sim.profile(timed=False, budget={"crash": 1, "drop": 0, "dup": 0})
    submit = lambda number: ("submit", number, 7, 11)
    actions = [(("local", 0, ("loaded", slot, slot, 0, 0, 0)), []) for slot in (0, 1)]
    actions += [(("client", "submit", 7, 11), []), (("deliver", 0, submit(0)), [0]),
                (("client", "submit", 7, 11), []),
                # crash-after 2: die after the first reply; persist 1: the pending row survives.
                (("deliver", 0, submit(1)), [2, 1])]
    trace = sim.script(broken, Clients(2, keys=(7,), digests=(11,)), untimed, actions)
    last = trace["steps"][-1]
    require(last["applied"] == 1 and last["before_crash"]["disks"] == [[]]
            and last["world"]["disks"] == [[[0, 7, 11]]], f"scenario not reached: {last}")
    require("inv.ok-is-durable" in violations(schema, [trace])[0],
            "a premature reply was excused by the crash that followed it")
    # Teeth: judged against the world after the crash, the same trace passes.
    del last["before_crash"]
    require(violations(schema, [trace]) == [[]], "the scenario does not need the split record")
    report("crash records: a reply made while its row was only pending violates "
           "inv.ok-is-durable although the crash that follows persists the row; judged "
           "against the world after the crash the same trace would pass")


def specification(work, schema, report):
    native = build(schema, work / "spec")
    report(f"compiled spec.shen: {native.stats}")

    traces = seeded(native, range(SEEDS))
    ends = sorted({t["end"] for t in traces})
    require(ends == ["quiescent"], f"simulation: runs ended {ends}")
    steps = sum(len(t["steps"]) for t in traces)
    kinds = {}
    for trace in traces:
        for step in trace["steps"]:
            name = "crash" if step.get("crashed") else step["kind"]
            kinds[name] = kinds.get(name, 0) + 1
    report(f"simulation: {SEEDS} seeds, {steps} steps, all quiescent; {dict(sorted(kinds.items()))}")

    # The crash points of the commit path must each occur in the seeded runs, and the
    # records must say what the world shows.
    check.effects_applied(schema, traces)
    points = crash_points(traces)
    require(all(points.values()), f"simulation: a crash point never occurred: {points}")
    report(f"crash points reached, by the state of the disk queue and the outputs in flight: "
           f"{points}")
    # Teeth: with a simulator that performs every effect of a step it cuts, or cuts one
    # effect late, no node ever dies before its write or between acknowledgement and reply.
    for world in (worlds.AllEffectsOnCut, worlds.LateCut):
        wrong = crash_points(seeded(native, range(SEEDS), world=world))
        require(not wrong["before the write"] and not wrong["between ack and reply"],
                f"crash points: {world.__name__} still shows them: {wrong}")
    report("crash point teeth: in a simulator that performs every effect of a cut step, and in "
           "one that cuts an effect late, 'before the write' and 'between ack and reply' "
           "never occur")

    # Same seed, same bytes; and every recorded trace replays.
    again = seeded(native, range(SEEDS))
    require([sim.dumps(t) for t in again] == [sim.dumps(t) for t in traces],
            "determinism: a seed produced different trace bytes on a second run")
    for trace in traces:
        sim.replay(native, clients(), trace)
    combined = hashlib.sha256("".join(sim.digest(t) for t in traces).encode()).hexdigest()
    report(f"determinism: {SEEDS} seeds rerun byte-identical and all {SEEDS} traces replay from "
           f"their records (the replay's own teeth are in `tool check sim`); sha256 over all "
           f"trace digests {combined[:16]}")

    pairs = check.step_pairs(traces)
    check.refinement(schema, native, pairs)
    vectors = [schema.encode_input(schema.encode_state(state), event) for state, event in pairs]
    require(run_lines(native.executable, vectors) == [native.raw(v) for v in vectors],
            "abi: the line executable and the shared library disagree")
    report(f"refinement: {steps} simulated steps reduce to {len(pairs)} distinct (state, event) "
           f"pairs; native equals shen-jit on all of them and on the initial state; the line "
           f"executable agrees")

    bad = [(n, names) for n, names in enumerate(violations(schema, traces)) if names]
    require(not bad, f"invariants: violated on seeds {bad[:5]}")
    counts = witnessed(schema, traces)
    require(all(counts.values()), f"witnesses: a required history never occurred: {counts}")
    names = schema.invariants["state"] + schema.invariants["trace"]
    report(f"invariants: {', '.join(names)} hold on all {SEEDS} traces")
    report(f"witnesses by simulation (seeds showing each history): {counts}")
    # Teeth for the two witnesses no spec mutant below removes: without crashes nothing is
    # lost before a commit, and with two keys the table of two receipts never fills.
    calm = witnessed(schema, seeded(native, range(100), profile=sim.profile(
        budget={"crash": 0, "drop": 2, "dup": 1}, max_steps=200)))
    roomy = witnessed(schema, seeded(native, range(100), keys=(7, 8)))
    require(calm["wit.retry-after-precommit-crash"] == 0 and roomy["wit.table-full"] == 0,
            f"witness teeth: {calm} {roomy}")
    report("witness teeth: 100 seeds without crashes never show retry-after-precommit-crash; "
           "100 seeds with two keys never show table-full")
    return native, traces, pairs


def bounded(schema, native, pairs, report):
    explored = {}
    for label, (scope, bound) in BOUNDS.items():
        mine, reached, facts = check.maude_agreement(
            schema, native, Clients(**scope), maude_clients(**scope), bound)
        require(facts["maude_trap"] is None, f"maude: a reachable step traps: {facts['maude_trap']}")
        require(mine == reached, f"maude and simulator disagree on '{label}': "
                                 f"{len(reached - mine)} observations only in Maude, "
                                 f"{len(mine - reached)} only in the simulator")
        require(all(map(safe, reached)), f"bounded search '{label}': an unsafe observation is reachable")
        counts = {f.__name__: sum(map(f, reached)) for f in (
            lost_reply_retry, precommit_crash_retry, conflict_rejected, table_full)}
        wanted = {"two keys": ["lost_reply_retry", "precommit_crash_retry", "conflict_rejected"],
                  "duplicate": ["lost_reply_retry", "precommit_crash_retry", "conflict_rejected"],
                  "table full": ["table_full"]}[label]
        require(all(counts[name] for name in wanted),
                f"bounded search '{label}': a required history is unreachable: {counts}")
        pairs.update(facts["pairs"])
        explored[label] = mine
        report(f"bounded search '{label}' ({scope}, budget {bound['budget']}): Maude "
               f"{facts['maude_states']} states, simulator {facts['sim_states']}; the same "
               f"{len(reached)} observations, all safe; witnesses reachable: {counts}")
    count = check.refinement(schema, native, pairs)
    check.maude_refinement(maude.step_module(schema.source), schema, pairs)
    report(f"refinement over simulation and search: native, shen-jit and the Maude translation "
           f"agree on all {count} (state, event) pairs that the seeded runs and the three "
           f"searches executed")

    # What the specification relies on: no completion crosses a crash. With that rule the
    # two-crash bound is safe; in a simulator without it the same binary is not.
    scope, bound = RELIANCE
    stock, _ = sim.explore(native, Clients(**scope), bound)
    stale, _ = sim.explore(native, Clients(**scope), bound, world=worlds.KeepsCompletions)
    unsafe = sum(not safe(o) for o in stale)
    require(all(map(safe, stock)) and unsafe,
            "the specification no longer depends on completions being discarded at a crash")
    report(f"environment rule the specification relies on ({scope}, budget {bound['budget']}): "
           f"all {len(stock)} observations are safe; in a simulator that delivers "
           f"acknowledgements and read results across a crash, {unsafe} of {len(stale)} are not")
    return explored["two keys"]


def one_step_removed(trace, native):
    actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
    for n in range(len(actions)):
        shorter = actions[:n] + actions[n + 1:]
        try:
            yield sim.script(native, clients(), trace["profile"], shorter)
        except sim.Divergence:
            continue


def mutants(work, schema, native, pairs, explored, report, record):
    retained_path = HERE / "mutants.json"
    retained = {} if record else json.loads(retained_path.read_text())
    scope, bound = BOUNDS["two keys"]
    for name, mutant in MUTANTS.items():
        source = mutate(schema.source, mutant)
        broken = build(schema, work / name, source)
        wanted = mutant["invariant"]
        fails = lambda traces: [wanted in names for names in violations(schema, traces)]
        if record:
            seed = next(s for s in range(5000) if fails(seeded(broken, [s]))[0])
            trace = seeded(broken, [seed])[0]
            minimal = check.shrink(broken, clients(), trace, fails)
            (HERE / "mutants").mkdir(exist_ok=True)
            (HERE / "mutants" / f"{name}.trace.json").write_text(sim.dumps(minimal))
            retained[name] = {"seed": seed, "invariant": wanted, "sha256": sim.digest(trace),
                              "steps": len(trace["steps"]),
                              "minimal_steps": len(minimal["steps"]),
                              "minimal_sha256": sim.digest(minimal)}
        kept = retained[name]
        require(kept["invariant"] == wanted, f"{name}: retained record is for another invariant")
        trace = seeded(broken, [kept["seed"]])[0]
        require(sim.digest(trace) == kept["sha256"],
                f"{name}: seed {kept['seed']} no longer reproduces the retained trace")
        require(fails([trace])[0], f"{name}: seed {kept['seed']} does not violate {wanted}")
        clean = seeded(native, [kept["seed"]])
        require(violations(schema, clean) == [[]], f"{name}: the unedited spec fails the same seed")
        minimal = json.loads((HERE / "mutants" / f"{name}.trace.json").read_text())
        require(sim.digest(minimal) == kept["minimal_sha256"], f"{name}: minimal trace changed")
        sim.replay(broken, clients(), minimal)
        require(fails([minimal])[0], f"{name}: the minimal trace does not violate {wanted}")
        require(not any(fails(list(one_step_removed(minimal, broken)))),
                f"{name}: the retained trace is not 1-minimal")
        # The refinement check must reject this binary against the unedited source, and
        # the Maude translation of the mutant must not pass for the unedited native code.
        for label, attempt in (
                ("refinement accepted a binary built from the mutant",
                 lambda: check.refinement(schema, broken, check.step_pairs([trace, minimal]))),
                ("the Maude step check accepted a translation of the mutant",
                 lambda: check.maude_refinement(maude.step_module(source), schema, pairs))):
            try:
                attempt()
            except CheckFailure:
                continue
            raise CheckFailure(f"{name}: {label}")
        # Bounded search on the mutant: Maude and the simulator still agree with each other,
        # an unsafe observation is reachable, and the set differs from the specification's.
        mine, reached, _ = check.maude_agreement(
            schema, broken, Clients(**scope), maude_clients(**scope), bound, source=source)
        require(mine == reached, f"{name}: Maude and the simulator disagree on the mutant")
        unsafe = sum(not safe(o) for o in reached)
        require(unsafe and reached != explored, f"{name}: bounded search does not catch it")
        gone = ""
        if "witness" in mutant:
            counts = witnessed(schema, seeded(broken, range(SEEDS)))
            require(counts[mutant["witness"]] == 0,
                    f"{name}: {mutant['witness']} still occurs on {counts[mutant['witness']]} seeds")
            gone = f"; {mutant['witness']} occurs on 0 of {SEEDS} seeds"
        report(f"mutant {name}: {wanted} fails at seed {kept['seed']} "
               f"({kept['steps']} steps, sha256 {kept['sha256'][:16]}); minimal trace "
               f"{kept['minimal_steps']} steps replays and still fails; refinement and the "
               f"Maude step check reject it; bounded search reaches {unsafe} unsafe "
               f"observations{gone}")
    if record:
        retained_path.write_text(json.dumps(retained, indent=2, sort_keys=True) + "\n")
    declared = set(schema.invariants["state"] + schema.invariants["trace"])
    covered = {mutant["invariant"] for mutant in MUTANTS.values()}
    require(covered == declared, f"invariants without a retained mutant: {declared - covered}")


def main(report=print, record=False):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        native, traces, pairs = specification(work, schema, report)
        judged_before_the_crash(work, schema, report)
        explored = bounded(schema, native, pairs, report)
        mutants(work, schema, native, pairs, explored, report, record)
    report("accepted_job: ok")
