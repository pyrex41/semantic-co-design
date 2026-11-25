"""A durable workflow engine on the kernel. Run: nix develop -c python3 -m tool check workflow

Regenerate the retained mutant evidence with: python3 -m tool record workflow
"""
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tool import check, maude, shen, sim
from tool.check import CheckFailure, require
from tool.compile import build, run_lines, specialize
from tool.pe import Specializer, Unsupported
from tool.schema import Schema

from .model import HERE, INVARIANTS, SYNTAX, TERMINAL, Definition
from .workload import Workers, maude as maude_workers

SEEDS = 400
SEEDED = sim.profile(budget={"crash": 2, "drop": 2, "dup": 2}, max_steps=600,
                     fault_rate=0.04, crash_mid_rate=0.02, client_rate=0.01)
# What an execution of each activity may return in the seeded runs; 0 is a failure and a
# repeated result is more likely. The environment chooses, the definition does not know.
OUTCOMES = {
    "order": {"reserve": (1, 1, 1, 1, 0), "charge": (1, 1, 2, 2, 0), "review": (1, 1, 0),
              "ship": (1, 1, 1, 0)},
    "docs": {"render": (1, 1, 1, 0), "bundle": (1, 1, 0)},
}
STATE = ["one-effect-per-key", "history-is-true", "history-is-a-log", "memory-is-replay",
         "terminal-is-final", "compensation-matches"]
TRACE = ["inv.append-only", "inv.recovery-reproduces", "inv.never-regresses"]
LIVE = "inv.settles"

# The exhaustive bounds, all on defs/small.shen: (fault budget, cancels, outcomes).
FLAKY = {"hold": (1, 0), "pay": (1,)}
BOUNDS = {
    "cancel": ({"crash": 0, "drop": 0, "dup": 0}, 1, {"hold": (1, 0), "pay": (1, 0)}),
    "drop": ({"crash": 0, "drop": 1, "dup": 0}, 0, FLAKY),
    "duplicate": ({"crash": 0, "drop": 0, "dup": 1}, 0, FLAKY),
    "crash": ({"crash": 1, "drop": 0, "dup": 0}, 0, FLAKY),
}

# Each mutant is an edit of engine.shen, the invariant that must reject it, and the
# exhaustive bound in which a violation must also be reachable.
MUTANTS = {
    "run-before-intent": {
        "what": "schedules the activity in the same step that writes the intent",
        "edits": [("[act _ Key _ _ _] _ _ Seq -> (wf.intend Seq [sched Key 1])",
                   "[act Name Key _ _ _] _ _ Seq ->\n"
                   "    [[sched Key 1] [[run Name Key 1] [write Seq Seq sched Key 1]]]")],
        "invariant": "history-is-true", "bound": "drop"},
    "key-dropped-on-retry": {
        "what": "sends a retry without the idempotency key",
        "edits": [("[[run Name Key A] [arm Key Timeout]]",
                   "[[run Name (if (= A 1) Key 0) A] [arm Key Timeout]]")],
        "invariant": "one-effect-per-key", "bound": "drop"},
    "forward-compensation": {
        "what": "undoes the oldest started activity first",
        "edits": [("  0 K 1 -> K\n  Later _ _ -> Later)", "  _ K 1 -> K\n  Later _ _ -> Later)")],
        "invariant": "compensation-matches", "bound": "drop"},
    "any-completion-accepted": {
        "what": "takes any completion for the one it is waiting for",
        "edits": [("[done Key A Result] -> (wf.outcome Key A Result)\n"
                   "    where (= Key (wf.key Def Pc Item))",
                   "[done Key _ Result] -> (wf.outcome Key A Result)")],
        "invariant": "history-is-true", "bound": "drop"},
    "in-doubt-not-undone": {
        "what": "undoes only activities known to have completed, not the one in flight",
        "edits": [("[waiting Pc Item A Last (wf.mark (wf.keys (wf.program Def)) Marks Key 1)]",
                   "[waiting Pc Item A Last Marks]"),
                  ("  Def [_ Pc Item _ _ Marks] [ok _ Result] ->\n"
                   "    (wf.arrive (wf.after (wf.program Def) 0 Pc Item Result (wf.program Def))"
                   " Result Marks)",
                   "  Def [_ Pc Item _ _ Marks] [ok Key Result] ->\n"
                   "    (wf.arrive (wf.after (wf.program Def) 0 Pc Item Result (wf.program Def))"
                   " Result\n               (wf.mark (wf.keys (wf.program Def)) Marks Key 1))")],
        "invariant": "compensation-matches", "bound": "drop"},
    "cancel-after-finish": {
        "what": "accepts a cancel in any phase, a finished workflow included",
        "edits": [("  backoff -> true\n  _ -> false)", "  backoff -> true\n  _ -> true)")],
        "invariant": "terminal-is-final", "also": "inv.never-regresses", "bound": "cancel"},
    "restart-without-replay": {
        "what": "reads the history back after a restart but does not apply it",
        "edits": [("[[loading (+ Seq 1) [none 0 0] (wf.apply Def Pos Row)]",
                   "[[loading (+ Seq 1) [none 0 0] Pos]")],
        "invariant": "memory-is-replay", "also": "inv.recovery-reproduces", "bound": "crash"},
    "sequence-restarts": {
        "what": "numbers its rows from 0 again after a restart",
        "edits": [("  Def Seq Pos _ _ -> (wf.enter Def Seq Pos))",
                   "  Def Seq Pos _ _ -> (wf.enter Def 0 Pos))")],
        "invariant": "history-is-a-log", "also": "inv.append-only", "bound": "crash"},
    "no-resume": {
        "what": "comes back from a restart without resuming what the position asks for",
        "edits": [("  Def Seq Pos _ _ -> (wf.enter Def Seq Pos))",
                   "  Def Seq Pos _ _ -> [[live Seq [none 0 0] Pos] []])")],
        "invariant": LIVE, "bound": "crash"},
}


def workers(definition, cancels=1):
    return Workers(definition.keys, OUTCOMES[definition.name], cancels)


def seeded(native, definition, seeds):
    return [sim.run(native, workers(definition), SEEDED, seed) for seed in seeds]


# ---- what the invariants see

def situation(world, ledger):
    state = world["states"][0]
    return ["down" if state is None else state, world["disks"][0], world["pending"][0], ledger]


def situations(trace):
    """A trace as [[Kind Situation] ...]. The ledger is the workers' own record, carried in
    the step records. A step during which the node died is two steps, the second `crash`."""
    out, ledger = [], []
    for step in trace["steps"]:
        ledger = step.get("ledger", ledger)
        if "before_crash" in step:
            out.append([step["kind"], situation(step["before_crash"], ledger)])
            out.append(["crash", situation(step["world"], ledger)])
        else:
            out.append([step["kind"], situation(step["world"], ledger)])
    return out


def judge(definition, traces):
    """The Shen invariants over traces, in one shen-jit process, against the engine in
    `definition`. Returns per trace {invariant: detail} for the ones that do not hold.

    An invariant that raises an error does not hold. `inv.settles` is asked only of a run
    in which nothing is left to happen.
    """
    name = definition.name
    calls = []
    for trace in traces:
        calls.append(f"(do (set wf.run {shen.term(situations(trace))}) ok)")
        calls += [f"(inv.first {inv} ({name}.definition) (value wf.run) 0)" for inv in STATE]
        calls += [f"({inv} (value wf.run))" for inv in TRACE]
        calls.append(f"({LIVE} ({name}.settles-within) (value wf.run))")
    values = iter(shen.evaluate(definition.files, calls, timeout=540))
    out = []
    for trace in traces:
        require(next(values) == "ok", "invariants: a trace could not be read by shen-jit")
        bad = {inv: f"step {v}" for inv in STATE if (v := next(values)) != -1}
        bad.update({inv: v for inv in TRACE if (v := next(values)) is not True})
        settled = next(values)
        if trace["end"] == "quiescent" and settled is not True:
            bad[LIVE] = settled
        out.append(bad)
    return out


# ---- 1. specialization

class Recording(Specializer):
    """Notes every symbol that is turned into an integer for the residual program."""
    residual = None

    def code(self, symbol):
        type(self).residual.add(symbol)
        return super().code(symbol)


def residual_symbols(schema, source=None):
    Recording.residual = set()
    tree, _, _ = specialize(schema, source, specializer=Recording)
    return json.dumps(tree), set(Recording.residual)


def constants(tree, out):
    """Every integer constant of an IR tree (not the slot and input numbers)."""
    if isinstance(tree, list):
        for child in tree[2 if tree[0] in ("var", "at", "set") else 1:]:
            constants(child, out)
    elif type(tree) is int:
        out.add(tree)
    return out


def specialization(work, report):
    engine = (HERE / "engine.shen").read_bytes()
    built = {}
    for name in ("order", "docs", "small"):
        definition = Definition(name, work / name)
        native = build(definition.schema, work / name / "build")
        built[name] = (definition, native)
        # The words of the definition language have no integer code in this manifest, so
        # the build above already could not have compared, stored or emitted one at run
        # time. Now give them codes and look at what the specializer would do with them.
        require(not set(SYNTAX) & set(definition.schema.symbols), "syntax is in the manifest")
        plain, used = residual_symbols(definition.schema)
        probe = Definition(name, work / f"{name}-syntax", extra_symbols=SYNTAX)
        declared, used_declared = residual_symbols(probe.schema)
        require(not used_declared & set(SYNTAX),
                f"{name}: the residual program uses definition syntax: {used_declared & set(SYNTAX)}")
        require(plain == declared, f"{name}: declaring the syntax changed the residual program")
        report(f"specialized engine.shen to {name}: {native.stats}; residual symbols "
               f"{len(used)}, none of {'/'.join(SYNTAX)}; the residual program is identical "
               f"when those words are given integer codes")
    require((HERE / "engine.shen").read_bytes() == engine, "the engine changed between builds")
    trees = {name: (work / name / "build" / "node.ir.json").read_text() for name in built}
    require(len(set(trees.values())) == 3, "two definitions gave the same residual program")
    keys = {name: set(built[name][0].keys) for name in built}
    for name, tree in trees.items():
        seen = constants(json.loads(tree), set())
        others = set().union(*(keys[other] for other in keys if other != name))
        require(keys[name] <= seen and not others & seen,
                f"{name}: the residual program does not hold exactly its own keys")
    report(f"one engine (sha256 {hashlib.sha256(engine).hexdigest()[:16]}), one compiler, three "
           f"different residual programs; each contains the idempotency keys of its own "
           f"definition as constants and none of another's")

    # Teeth for the residual check: an interpreter that looks an instruction up as a value
    # and only then asks what it is. With its opcodes undeclared the specializer has to keep
    # each one static; with them declared they reach the residual program and are found.
    leak = json.loads((HERE / "leak" / "node.json").read_text())
    static, used = residual_symbols(Schema(leak, HERE / "leak"))
    require(not used & {"inc", "dec"}, "leak fixture: opcodes residual although undeclared")
    leaky, used = residual_symbols(Schema({**leak, "symbols": ["idle", "inc", "dec"]},
                                          HERE / "leak"))
    require({"inc", "dec"} <= used and leaky != static,
            "the residual check does not notice an interpreter that dispatches on syntax")
    storing = (HERE / "leak" / "spec.shen").read_text()
    require(storing.count(" Seen] []]") == 1, "leak fixture: mutation site")
    try:
        specialize(Schema(leak, HERE / "leak"), storing.replace(
            " Seen] []]", " (leak.opcode (leak.fetch (leak.program) 0 Pc))] []]"))
    except Unsupported as error:
        refused = str(error)
    else:
        raise CheckFailure("an undeclared opcode was stored in the state")
    report(f"residual check teeth: a fetch-then-dispatch interpreter stays free of its opcodes "
           f"while they are undeclared, residualizes {sorted(used & {'inc', 'dec'})} once they "
           f"are declared, and is refused when it stores one ({refused[:60]}...)")
    return built


# ---- 2. simulation

def phase(state):
    return None if state is None else state[3][0]


def boundaries(records):
    """For every shape of step (event, effects), how often the node died after 0, 1, ... of
    its effects."""
    shapes = {}
    for step in records:
        if step.get("effects"):
            shape = (step["event"][0], *(effect[0] for effect in step["effects"]))
            cuts = shapes.setdefault(shape, [0] * len(step["effects"]))
            if "crashed" in step and step["applied"] < len(step["effects"]):
                cuts[step["applied"]] += 1
    return shapes


def histories(traces):
    """How often the seeded runs show each history the task asks to be simulated."""
    seen = Counter()
    for trace in traces:
        armed, flight = {}, None          # timer id -> step it was armed at; key in flight
        cancelled = None
        for step in trace["steps"]:
            kind, event = step["kind"], step.get("event")
            tag = event[0] if event else None
            if kind in ("drop", "dup") and tag in ("done", "reverted"):
                seen[f"completion {'lost' if kind == 'drop' else 'duplicated'} in flight"] += 1
            if kind in ("drop", "dup") and tag in ("run", "revert"):
                seen[f"request {'lost' if kind == 'drop' else 'duplicated'} in flight"] += 1
            if step.get("crashed"):
                armed = {}
                seen["crash"] += 1
            if "pre" not in step:
                continue
            mode, _, pend, pos = step["pre"]
            quiet = not step["effects"] and step["post"] == step["pre"]
            if tag == "timer":
                if any(at < armed.get(event[1], -1) for at in armed.values()):
                    seen["timer fired before one armed earlier"] += 1
                armed.pop(event[1], None)
                if quiet:
                    seen["late timer ignored"] += 1
                elif step["effects"][0][3] == "fail":
                    seen["attempt timed out"] += 1
            if tag == "done" and quiet and mode == "live":
                seen["completion ignored (late or duplicate)"] += 1
                if cancelled == event[1]:
                    seen["cancel won the race: the completion came after it"] += 1
            if tag == "cancel":
                if not quiet:
                    cancelled = step["effects"][0][0] == "write" and pos[0] == "waiting" and flight
                    seen[f"cancel accepted while {pos[0]}"] += 1
                elif mode == "live" and pend[0] in ("ok", "completed"):
                    seen["completion won the race: cancel came while it was being written"] += 1
                elif mode == "live" and pos[0] in TERMINAL:
                    seen["cancel after the workflow finished"] += 1
            for effect in step["effects"][:step["applied"]]:
                if effect[0] == "arm":
                    armed[effect[1]] = step["i"]
                if effect[0] == "run":
                    flight = effect[2]
        final = trace["steps"][-1]["world"]["states"][0]
        seen[f"ended {phase(final) or 'down'}"] += 1
    return seen


REQUIRED = ["completion lost in flight", "completion duplicated in flight",
            "request lost in flight", "request duplicated in flight", "late timer ignored",
            "timer fired before one armed earlier", "attempt timed out",
            "completion ignored (late or duplicate)",
            "cancel won the race: the completion came after it",
            "completion won the race: cancel came while it was being written",
            "cancel after the workflow finished", "cancel accepted while waiting",
            "ended completed", "ended compensated"]


def settle_steps(trace):
    """Steps from the last fault to the first state at rest (what inv.settles bounds)."""
    steps = situations(trace)
    last = max((n for n, (kind, _) in enumerate(steps) if kind in ("crash", "drop", "dup", "lost")),
               default=0)
    for n in range(last, len(steps)):
        node = steps[n][1][0]
        if node != "down" and node[0] == "live" and node[2][0] == "none" and node[3][0] in TERMINAL:
            return n - last
    return None


def simulation(definition, native, report):
    name, schema = definition.name, definition.schema
    traces = seeded(native, definition, range(SEEDS))
    ends = sorted({t["end"] for t in traces})
    require(ends == ["quiescent"], f"{name}: runs ended {ends}")
    require([sim.dumps(t) for t in seeded(native, definition, range(SEEDS))]
            == [sim.dumps(t) for t in traces], f"{name}: a seed produced different trace bytes")
    for trace in traces:
        sim.replay(native, workers(definition), trace)
    steps = sum(len(t["steps"]) for t in traces)
    combined = hashlib.sha256("".join(sim.digest(t) for t in traces).encode()).hexdigest()
    report(f"{name}: {SEEDS} seeds, {steps} steps, all quiescent, byte-identical on rerun, "
           f"every trace replays; sha256 over the trace digests {combined[:16]}")

    check.effects_applied(schema, traces)
    cuts = boundaries(step for trace in traces for step in trace["steps"])
    covered = sum(bool(n) for shape in cuts.values() for n in shape)
    total = sum(len(shape) for shape in cuts.values())
    missing = sorted(f"{'/'.join(shape)}@{n}" for shape, counts in cuts.items()
                     for n, count in enumerate(counts) if not count)
    report(f"{name}: crashes inside a step: {covered} of the {total} effect boundaries of the "
           f"{len(cuts)} step shapes seen were hit; not hit: {missing or 'none'}")

    seen = histories(traces)
    require(all(seen[h] for h in REQUIRED), f"{name}: a required history never occurred: "
            f"{[h for h in REQUIRED if not seen[h]]}")
    report(f"{name}: histories (occurrences over all seeds): {dict(sorted(seen.items()))}")

    ledgers = [next((s["ledger"] for s in reversed(t["steps"]) if "ledger" in s), [])
               for t in traces]
    executions = sum(e[0] in ("commit", "fault") for ledger in ledgers for e in ledger)
    requests = sum(s["kind"] == "output" and s["event"][0] == "run"
                   for t in traces for s in t["steps"])
    commits = sum(e[0] == "commit" for ledger in ledgers for e in ledger)
    report(f"{name}: workers received {requests} run requests, really executed {executions} "
           f"times and committed {commits} effects; "
           f"{sum(e[0] == 'reverse' for ledger in ledgers for e in ledger)} were reversed and "
           f"{sum(e[0] == 'void' for ledger in ledgers for e in ledger)} undos found nothing "
           f"committed")

    pairs = check.step_pairs(traces)
    check.refinement(schema, native, pairs)
    vectors = [schema.encode_input(schema.encode_state(state), event) for state, event in pairs]
    require(run_lines(native.executable, vectors) == [native.raw(v) for v in vectors],
            f"{name}: the line executable and the shared library disagree")
    report(f"{name}: native equals shen-jit running engine.shen on all {len(pairs)} distinct "
           f"(state, event) pairs of the runs and on the initial state")

    bad = [(seed, sorted(found)) for seed, found in enumerate(judge(definition, traces)) if found]
    require(not bad, f"{name}: invariants violated on seeds {bad[:5]}")
    worst = max(settle_steps(t) for t in traces)
    bound, = shen.evaluate(definition.files, [f"({name}.settles-within)"])
    report(f"{name}: the six state invariants hold in every step and the three trace "
           f"invariants on every run; every run is at rest within {worst} steps of its last "
           f"fault (declared bound {bound})")
    return traces, pairs


# ---- 3. bounded search

def graph(native, workload, prof, max_states=400000):
    """Every reachable world of an untimed profile, as sim.explore visits them, with the
    edges kept. Returns observations, executed pairs, and per state its situation, whether
    anything but a fault can still happen there, and its successors."""
    world = sim.World(native, workload, prof)
    pairs, records = {}, []

    def note(rec, cuttable):
        if "pre" in rec:
            pairs[sim.freeze((rec["pre"], rec["event"]))] = sim.freeze((rec["post"], rec["effects"]))
            if cuttable:        # a step taken while a crash was still in the budget
                records.append(rec)

    for rec in world.boot():
        note(rec, False)
    index, queue = {world.key(): 0}, deque([world])
    sits, idle, edges, observations = [], [], [], set()
    while queue:
        world = queue.popleft()
        observations.add(world.observe())
        sits.append(sim.freeze(situation(world.snapshot(), world.client[0])))
        following, busy = [], False
        for action, _, after, rec in sim.successors(world):
            note(rec, world.budget["crash"] > 0)
            busy |= action[0] not in ("crash", "drop", "dup")
            key = after.key()
            if key not in index:
                require(len(index) < max_states, f"more than {max_states} states")
                index[key] = len(index)
                queue.append(after)
            following.append(index[key])
        edges.append(following)
        idle.append(not busy)
    return observations, pairs, records, sits, idle, edges


def longest_path(edges):
    """Length of the longest schedule, or None if the graph has a cycle."""
    incoming = [0] * len(edges)
    for following in edges:
        for n in following:
            incoming[n] += 1
    depth, ready, done = [0] * len(edges), [n for n, k in enumerate(incoming) if not k], 0
    while ready:
        n = ready.pop()
        done += 1
        for m in edges[n]:
            depth[m] = max(depth[m], depth[n] + 1)
            incoming[m] -= 1
            if not incoming[m]:
                ready.append(m)
    return max(depth) if done == len(edges) else None


def sit_term(node):
    return f"cons({node}, cons(RW:Val, cons(f.inv.tails(PD:Val), cons(LG:Val, nil))))"


def world_pattern(node):
    return f"{{ {node} disk(0, RW:Val, PD:Val) client(LG:Val, CC:Nat) REST:Soup }}"


QUIET = """mod WORKFLOW-SEARCH is
  protecting ENV .
  op busy : Soup -> Bool .
  eq busy(ev(I:Int, E:Val) R:Soup) = true .
  eq busy(lev(I:Int, E:Val) R:Soup) = true .
  eq busy(out(V:Val) R:Soup) = true .
  eq busy(down(I:Int) R:Soup) = true .
  eq busy(R:Soup) = false [owise] .
endm
"""


def maude_model(definition, prof, cancels, outcomes):
    return (maude.step_module(definition.text + "\n" + INVARIANTS.read_text())
            + maude.environment_module(definition.schema, prof,
                                       maude_workers(definition.keys, outcomes, cancels))
            + QUIET)


def maude_violation(definition, modules, wanted=None):
    """Search the Maude model for a reachable state in which a translated Shen invariant
    does not hold (`wanted`: that one in particular; inv.settles: a world with nothing left
    to happen that is not at rest). Returns the state as a situation, or None."""
    name = definition.name
    if wanted == LIVE:
        searches = [f"search [1] in WORKFLOW-SEARCH : init =>* {world_pattern('node(0, NS:Val)')} "
                    f"such that not busy(REST:Soup) and PD:Val == nil and CC:Nat == 0 "
                    f"and f.inv.resting?(NS:Val) =/= b(true) .\n"]
    else:
        verdict = lambda node: f"f.inv.verdict(f.{name}.definition, {sit_term(node)})"
        test = (lambda node: f"{verdict(node)} =/= nil") if wanted is None else (
            lambda node: f"f.element?(q('{wanted}), {verdict(node)}) =/= b(false)")
        searches = [f"search [1] in WORKFLOW-SEARCH : init =>* {world_pattern(thing)} "
                    f"such that {test(node)} .\n"
                    for thing, node in (("node(0, NS:Val)", "NS:Val"), ("down(0)", "q('down)"))]
    for search in searches:         # one at a time: a mutant's whole state space can be large
        output = maude.run(modules, [search], timeout=300)
        require(output.count("No solution.") + output.count("Solution 1") == 1,
                f"maude search did not finish:\n{output[-1500:]}")
        if "Solution 1" in output:
            break
    else:
        return None
    found = output.split("Solution 1", 1)[1]
    found = found.split("==========", 1)[0].split("\nBye.", 1)[0]

    def bound(variable, default=None):
        text = found.split(f"{variable} --> ", 1)
        if len(text) == 1:
            return default
        text = text[1]
        for other in ("NS:Val", "RW:Val", "PD:Val", "LG:Val", "CC:Nat", "REST:Soup"):
            text = text.split(f"{other} --> ", 1)[0]
        return maude.value(maude.read_terms(text)[0])

    pending = [row[1:] for row in bound("PD:Val")]
    return [bound("NS:Val", "down"), bound("RW:Val"), pending, bound("LG:Val")]


def bounded(definition, native, report):
    name, schema = definition.name, definition.schema
    limit, = shen.evaluate(definition.files, [f"({name}.settles-within)"])
    everything, results = {}, {}
    for label, (budget, cancels, outcomes) in BOUNDS.items():
        prof = sim.profile(timed=False, observe_states=True, budget=budget)
        mine, pairs, records, sits, idle, edges = graph(
            native, Workers(definition.keys, outcomes, cancels), prof)
        modules = maude_model(definition, prof, cancels, outcomes)
        reached, states, trap = maude.search(modules)
        require(trap is None, f"maude: a reachable step traps in bound '{label}': {trap}")
        require(mine == reached, f"maude and simulator disagree on '{label}': "
                f"{len(reached - mine)} observations only in Maude, {len(mine - reached)} only "
                f"in the simulator")
        # The Shen invariants, twice: translated to Maude and searched for a violation
        # over every reachable state, and evaluated by shen-jit on every distinct situation
        # of the simulator's graph.
        found = maude_violation(definition, modules)
        require(found is None, f"bound '{label}': Maude reaches a state violating an invariant: {found}")
        require(maude_violation(definition, modules, LIVE) is None,
                f"bound '{label}': Maude reaches a world with nothing left to happen that is not at rest")
        distinct = list(dict.fromkeys(sits))
        verdicts = shen.evaluate(definition.files, [
            f"(inv.verdict ({name}.definition) {shen.term(sit)})" for sit in distinct], timeout=540)
        bad = [(sit, v) for sit, v in zip(distinct, verdicts) if v != ()]
        require(not bad, f"bound '{label}': a reachable situation violates {bad[:1]}")
        # The two transition invariants on every edge, by what they read: the rows before
        # and after, and the node before and after.
        moves = [dict.fromkeys((sits[n][part], sits[m][part]) for n, following in enumerate(edges)
                               for m in following if sits[n][part] != sits[m][part])
                 for part in (1, 0)]
        calls = [f"(inv.append-only {shen.term([['step', ['down', a, [], []]], ['step', ['down', b, [], []]]])})"
                 for a, b in moves[0]]
        calls += [f"(inv.never-regresses {shen.term([['step', [a, [], [], []]], ['step', [b, [], [], []]]])})"
                  for a, b in moves[1]]
        require(all(v is True for v in shen.evaluate(definition.files, calls, timeout=540)),
                f"bound '{label}': a transition changes the durable history or regresses")
        moves = len(moves[0]) + len(moves[1])
        # Liveness, exhaustively: the graph has no cycle, so every schedule ends, and it
        # can only end where nothing but a fault could still happen; such a world is at rest.
        longest = longest_path(edges)
        require(longest is not None, f"bound '{label}': some schedule never ends")
        resting = shen.evaluate(definition.files, [
            f"(inv.resting? {shen.term(sit[0])})" for sit in dict.fromkeys(
                sit for sit, quiet in zip(sits, idle) if quiet)])
        require(resting and all(v is True for v in resting),
                f"bound '{label}': nothing is left to happen in a world that is not at rest")
        require(longest <= limit, f"bound '{label}': a schedule of {longest} steps exceeds "
                                  f"the declared {limit}")
        cuts = boundaries(records)
        require(budget["crash"] == 0 or all(all(counts) for counts in cuts.values()),
                f"bound '{label}': an effect boundary was never crashed at: {cuts}")
        everything.update(pairs)
        results[label] = (prof, cancels, outcomes, mine)
        report(f"bounded search '{label}' (budget {budget}, cancels {cancels}, outcomes "
               f"{outcomes}): Maude {states} states, simulator {len(sits)}; the same "
               f"{len(reached)} observations; no state violates a state invariant (Maude search "
               f"over the translated Shen, and shen-jit on the {len(distinct)} distinct "
               f"situations); {moves} distinct transitions keep the history and never "
               f"regress; no cycle, longest schedule {longest} steps (declared {limit}), all "
               f"{len(resting)} situations where nothing is left to happen are at rest"
               + (f"; the node died at every boundary of all {len(cuts)} step shapes"
                  if budget["crash"] else ""))
    count = check.refinement(schema, native, everything)
    check.maude_refinement(maude.step_module(definition.text), schema, everything)
    report(f"bounded search: native, shen-jit and the Maude translation agree on all {count} "
           f"(state, event) pairs the four searches executed")
    return everything, results


# ---- 4. mutants

def one_step_removed(native, workload, trace):
    actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
    for n in range(len(actions)):
        try:
            yield sim.script(native, workload, trace["profile"], actions[:n] + actions[n + 1:])
        except sim.Divergence:
            continue


def mutants(work, order, native, small, searched, report, record):
    retained_path = HERE / "mutants.json"
    retained = {} if record else json.loads(retained_path.read_text())
    small_pairs, bounds = searched
    for label, mutant in MUTANTS.items():
        wanted = mutant["invariant"]
        broken = order.mutant(label, mutant["edits"])
        binary = build(broken.schema, broken.directory / "build")
        fails = lambda traces: [wanted in found and (wanted != LIVE or t["end"] == "quiescent")
                                for t, found in zip(traces, judge(broken, traces))]
        if record:
            seed = next(s for s in range(2000) if fails(seeded(binary, broken, [s]))[0])
            trace = seeded(binary, broken, [seed])[0]
            minimal = check.shrink(binary, workers(broken), trace, fails)
            (HERE / "mutants").mkdir(exist_ok=True)
            (HERE / "mutants" / f"{label}.trace.json").write_text(sim.dumps(minimal))
            retained[label] = {"seed": seed, "invariant": wanted, "sha256": sim.digest(trace),
                               "steps": len(trace["steps"]), "minimal_steps": len(minimal["steps"]),
                               "minimal_sha256": sim.digest(minimal)}
        kept = retained[label]
        require(kept["invariant"] == wanted, f"{label}: retained record is for another invariant")
        trace = seeded(binary, broken, [kept["seed"]])[0]
        require(sim.digest(trace) == kept["sha256"],
                f"{label}: seed {kept['seed']} no longer reproduces the retained trace")
        found = judge(broken, [trace])[0]
        require(fails([trace])[0], f"{label}: seed {kept['seed']} does not violate {wanted}")
        require("also" not in mutant or mutant["also"] in found,
                f"{label}: seed {kept['seed']} does not violate {mutant.get('also')}")
        require(judge(order, seeded(native, order, [kept["seed"]])) == [{}],
                f"{label}: the unedited engine fails the same seed")
        minimal = json.loads((HERE / "mutants" / f"{label}.trace.json").read_text())
        require(sim.digest(minimal) == kept["minimal_sha256"], f"{label}: minimal trace changed")
        sim.replay(binary, workers(broken), minimal)
        require(fails([minimal])[0], f"{label}: the minimal trace does not violate {wanted}")
        require(not any(fails(list(one_step_removed(binary, workers(broken), minimal)))),
                f"{label}: the retained trace is not 1-minimal")
        # The refinement check must reject this binary against the unedited engine.
        try:
            check.refinement(order.schema, binary, check.step_pairs([trace, minimal]))
        except CheckFailure:
            pass
        else:
            raise CheckFailure(f"{label}: refinement accepted a binary built from the mutant")
        # The same mutant of the engine on the small definition, searched exhaustively.
        tiny = small.mutant(label, mutant["edits"])
        try:
            check.maude_refinement(maude.step_module(tiny.text), small.schema, small_pairs)
        except CheckFailure:
            pass
        else:
            raise CheckFailure(f"{label}: the Maude step check accepted a translation of the mutant")
        prof, cancels, outcomes, _ = bounds[mutant["bound"]]
        state = maude_violation(tiny, maude_model(tiny, prof, cancels, outcomes), wanted)
        require(state is not None, f"{label}: the bounded search '{mutant['bound']}' does not "
                                   f"reach a violation of {wanted}")
        if wanted == LIVE:
            verdict, = shen.evaluate(tiny.files, [f"(inv.resting? {shen.term(state[0])})"])
            require(verdict is False, f"{label}: shen-jit calls Maude's stuck state at rest")
        else:
            verdict, = shen.evaluate(tiny.files, [
                f"(inv.verdict ({tiny.name}.definition) {shen.term(state)})"])
            require(verdict == shen.TRAP or wanted in verdict,
                    f"{label}: shen-jit does not confirm Maude's violation: {verdict}")
        report(f"mutant {label} ({mutant['what']}): {wanted}"
               f"{' and ' + mutant['also'] if 'also' in mutant else ''} fail at seed "
               f"{kept['seed']} ({kept['steps']} steps, sha256 {kept['sha256'][:16]}); minimal "
               f"trace {kept['minimal_steps']} steps replays, still fails and is 1-minimal; "
               f"refinement rejects the binary; in the bounded search '{mutant['bound']}' on "
               f"the small definition Maude reaches a violating state and shen-jit confirms it")
    if record:
        retained_path.write_text(json.dumps(retained, indent=2, sort_keys=True) + "\n")
    covered = {m["invariant"] for m in MUTANTS.values()} | {
        m["also"] for m in MUTANTS.values() if "also" in m}
    declared = set(STATE + TRACE + [LIVE])
    require(covered == declared, f"invariants without a retained mutant: {declared - covered}")


def main(report=print, record=False):
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        built = specialization(work, report)
        pairs = {}
        for name in ("order", "docs"):
            pairs[name] = simulation(*built[name], report)
        searched = bounded(*built["small"], report)
        mutants(work, *built["order"], built["small"][0], searched, report, record)
    report("workflow: ok")
