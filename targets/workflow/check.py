"""A durable workflow engine on the kernel.

    nix develop -c python3 -m tool check workflow       every section
    nix develop -c python3 -m tool record workflow      the same, regenerating mutants.json and mutants/
    nix develop -c python3 -m targets.workflow.check simulation mutants     only some sections

Sections: specialization, simulation, bounded, reliance, mutants.
"""
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

from tool import check, maude, shen, sim
from tool.check import CheckFailure, require
from tool.compile import build, run_lines, specialize
from tool.pe import Specializer, Unsupported
from tool.schema import Schema
from tool.sim import mutants as worlds

from .model import HERE, INVARIANTS, SYNTAX, TERMINAL, Definition, manifest, problems
from .workload import Workers, maude as maude_workers

SECTIONS = ("specialization", "simulation", "bounded", "reliance", "mutants")
DEFINITIONS = ("order", "docs", "small", "single", "mix")
SEEDS = 400
SEEDED = sim.profile(budget={"crash": 2, "drop": 2, "dup": 2}, max_steps=600,
                     fault_rate=0.04, crash_mid_rate=0.02, client_rate=0.01)
# The same schedule with nothing in the fault budget: every run is free of crashes, losses
# and duplications, and whatever is compensated in it is compensated without a fault.
CALM_SEEDS = 200
CALM = sim.profile(budget={"crash": 0, "drop": 0, "dup": 0}, max_steps=600, client_rate=0.01)
# What an execution of each activity may return in the seeded runs; 0 is a failure and a
# repeated result is more likely. The environment chooses, the definition does not know.
OUTCOMES = {
    "order": {"reserve": (1, 1, 1, 1, 0), "charge": (1, 1, 2, 2, 0), "review": (1, 1, 0),
              "ship": (1, 1, 1, 0)},
    "docs": {"render": (1, 1, 1, 0), "bundle": (1, 1, 0)},
}
STATE = ["one-effect-per-activity", "history-is-true", "history-is-a-log",
         "history-follows-definition", "memory-is-replay", "terminal-is-final",
         "compensation-matches", "completed-is-whole", "reports-are-true"]
TRACE = ["inv.append-only", "inv.recovery-reproduces", "inv.never-regresses", "inv.backs-off",
         "inv.stuck-needs-a-fault"]
# Asked only of a run in which nothing is left to happen.
LIVE = "inv.settles"
TOLD = "inv.client-is-told"

# The exhaustive bounds: (definition, fault budget, cancels, outcomes). In an untimed search
# a timer may fire at any moment, so timeouts, late timers and every order of timers and
# completions are in each of them.
FLAKY = {"hold": (1, 0), "pay": (1,)}
BOUNDS = {
    "cancel": ("small", {"crash": 0, "drop": 0, "dup": 0}, 1, {"hold": (1, 0), "pay": (1, 0)}),
    "drop": ("small", {"crash": 0, "drop": 1, "dup": 0}, 0, FLAKY),
    "duplicate": ("small", {"crash": 0, "drop": 0, "dup": 1}, 0, FLAKY),
    "crash": ("small", {"crash": 1, "drop": 0, "dup": 0}, 0, FLAKY),
    # A second crash can hit the recovery from the first one.
    "two crashes": ("single", {"crash": 2, "drop": 0, "dup": 0}, 0, {"hold": (1,)}),
    # The instructions small and single do not have: each, branch (taken on 1, not on 2)
    # and sleep, with a failure and a cancel so that both of them are also compensated.
    "mix": ("mix", {"crash": 0, "drop": 0, "dup": 0}, 1, {"scan": (1, 2, 0), "fix": (1,)}),
}
# An engine that takes any acknowledgement for the write in flight. Under the environment's
# rule that no completion crosses a crash it cannot be told from the real one.
BLIND = [("  Def [live Seq Row Pos] [written Seq] ->", "  Def [live Seq Row Pos] [written _] ->")]

# Each mutant is an edit of engine.shen, the invariant that must reject it, and the
# exhaustive bound in which a violation must also be reachable (None for an invariant over
# clock times, which an untimed search cannot state).
MUTANTS = {
    "run-before-intent": {
        "what": "schedules the activity in the same step that writes the intent",
        "edits": [("[act _ Key _ _ _] _ _ Seq -> (wf.intend Seq [sched Key 1])",
                   "[act Name Key _ _ _] _ _ Seq ->\n"
                   "    [[sched Key 1] [[run Name Key 1] [write Seq Seq sched Key 1]]]")],
        "invariant": "history-is-true", "bound": "drop"},
    "key-dropped-on-retry": {
        "what": "sends a retry without the idempotency key",
        "edits": [("[[run Name Key A] [arm Seq Timeout]]",
                   "[[run Name (if (= A 1) Key 0) A] [arm Seq Timeout]]")],
        "invariant": "one-effect-per-activity", "bound": "drop"},
    "forward-compensation": {
        "what": "undoes the oldest started activity first",
        "edits": [("  0 K 1 -> K\n  Later _ _ -> Later)", "  _ K 1 -> K\n  Later _ _ -> Later)")],
        "invariant": "compensation-matches", "bound": "drop"},
    "completion-of-another-key": {
        "what": "takes the completion of any key for the one it is waiting for",
        "edits": [("[done Key A Result] -> (wf.outcome Key A Result)\n"
                   "    where (= Key (wf.key Def Pc Item))",
                   "[done Key A Result] -> (wf.outcome Key A Result)")],
        "invariant": "history-follows-definition", "bound": "drop"},
    "stale-attempt-accepted": {
        "what": "takes the completion of an earlier attempt for the one in flight",
        "edits": [("[done Key A Result] -> (wf.outcome Key A Result)",
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
    "cancel-skips-compensation": {
        "what": "treats an accepted cancel as the end of the workflow and undoes nothing",
        "edits": [("[cancel _ _] -> [unwinding Pc Item 0 Last Marks]",
                   "[cancel _ _] -> [completed Pc Item 0 Last Marks]")],
        "invariant": "memory-is-replay", "bound": "cancel"},
    "gives-up-early": {
        "what": "abandons compensation without trying a single undo",
        "edits": [("  Key Seq -> (wf.intend Seq [undo Key 1]))",
                   "  Key Seq -> (wf.intend Seq [stuck 0 0]))")],
        "invariant": "history-follows-definition", "bound": "cancel"},
    "extra-attempt": {
        "what": "tries an activity once more than the definition allows",
        "edits": [("(if (< A (wf.attempts (wf.activity (wf.program Def) 0 Pc Item)))",
                   "(if (<= A (wf.attempts (wf.activity (wf.program Def) 0 Pc Item)))")],
        "invariant": "history-follows-definition", "bound": "drop"},
    "fail-ignored": {
        "what": "carries on past an activity whose attempts are all used up",
        "edits": [("        [backoff Pc Item A Last Marks]\n        [unwinding Pc Item 0 Last Marks]))",
                   "        [backoff Pc Item A Last Marks]\n        (wf.arrive (wf.after (wf.program "
                   "Def) 0 Pc Item Last (wf.program Def)) Last Marks)))")],
        "invariant": "completed-is-whole", "bound": "drop"},
    "branch-inverted": {
        "what": "takes a branch exactly when the result is not the one it names",
        "edits": [("(if (= Result Value) (wf.settle Program Target Result) "
                   "(wf.settle Program (+ Pc 1) Result))",
                   "(if (= Result Value) (wf.settle Program (+ Pc 1) Result) "
                   "(wf.settle Program Target Result))")],
        "invariant": "completed-is-whole", "bound": "mix"},
    "report-lies": {
        "what": "tells the client `completed` whatever the outcome",
        "edits": [("  _ _ [Final | _] -> [[none 0 0] [[report Final]]])",
                   "  _ _ [Final | _] -> [[none 0 0] [[report completed]]])")],
        "invariant": "reports-are-true", "bound": "cancel"},
    "report-before-durable": {
        "what": "tells the client `completed` in the step that writes the row",
        "edits": [("  [end] _ _ Seq -> (wf.intend Seq [completed 0 0]))",
                   "  [end] _ _ Seq ->\n"
                   "    [[completed 0 0] [[write Seq Seq completed 0 0] [report completed]]])")],
        "invariant": "reports-are-true", "bound": "drop"},
    "never-reports": {
        "what": "finishes the workflow and tells the client nothing",
        "edits": [("  _ _ [Final | _] -> [[none 0 0] [[report Final]]])",
                   "  _ _ [Final | _] -> [[none 0 0] []])")],
        "invariant": TOLD, "bound": "cancel"},
    "restart-without-replay": {
        "what": "reads the history back after a restart but does not apply it",
        "edits": [("[[loading (+ Seq 1) [none 0 0] (wf.apply Def Pos Row)]",
                   "[[loading (+ Seq 1) [none 0 0] Pos]")],
        "invariant": "memory-is-replay", "also": "inv.recovery-reproduces", "bound": "crash",
        "regresses": True},
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
    "no-backoff": {
        "what": "retries at once instead of after the backoff",
        "edits": [("[[arm Seq (* Backoff A)]]", "[[arm Seq (* Backoff 0)]]")],
        "invariant": "inv.backs-off", "bound": None},
    "old-timer-accepted": {
        "what": "takes the expiry of any timer for the one of its position",
        "edits": [("  Def Seq [waiting Pc Item A _ _] [timer Seq] ->",
                   "  Def Seq [waiting Pc Item A _ _] [timer _] ->"),
                  ("  Def Seq [backoff Pc Item A _ _] [timer Seq] ->",
                   "  Def Seq [backoff Pc Item A _ _] [timer _] ->"),
                  ("  _ Seq [ready Pc _ _ _ _] [timer Seq] ->", "  _ Seq [ready Pc _ _ _ _] [timer _] ->"),
                  ("  Def Seq [undoing _ _ U _ Marks] [timer Seq] ->",
                   "  Def Seq [undoing _ _ U _ Marks] [timer _] ->")],
        "invariant": "inv.backs-off", "bound": None},
    "undo-not-awaited": {
        "what": "gives an undo no time to be acknowledged",
        "edits": [("[arm Seq (wf.undo-timeout Def)]", "[arm Seq 1]")],
        "invariant": "inv.stuck-needs-a-fault", "bound": None},
}
# Definitions that definition.shen must refuse, and the problem it must name.
REJECTED = {"duplicate-key": "duplicate-key", "backward-branch": "branch-not-forward",
            "branch-out-of-range": "branch-out-of-range", "no-end": "does-not-end"}


def workers(definition, cancels=1, **options):
    return Workers(definition.keys, OUTCOMES[definition.name], cancels, **options)


def seeded(native, definition, seeds, **options):
    return [sim.run(native, workers(definition, **options), SEEDED, seed) for seed in seeds]


# ---- what the invariants see

def situation(world, ledger, told):
    state = world["states"][0]
    return ["down" if state is None else state, world["disks"][0], world["pending"][0], ledger,
            told]


def situations(trace):
    """A trace as [[Kind Time Situation] ...]. The ledger and what the client was told are
    the workload's own record, carried in the step records. A step during which the node
    died is two steps, the second `crash`."""
    out, ledger, told = [], [], []
    for step in trace["steps"]:
        ledger, told = step.get("ledger", ledger), step.get("told", told)
        if "before_crash" in step:
            out.append([step["kind"], step["t"], situation(step["before_crash"], ledger, told)])
            out.append(["crash", step["t"], situation(step["world"], ledger, told)])
        else:
            out.append([step["kind"], step["t"], situation(step["world"], ledger, told)])
    return out


def judge(definition, traces):
    """The Shen invariants over traces, in one shen-jit process, for the definition in
    `definition`. Returns per trace {invariant: detail} for the ones that do not hold. The
    invariants call nothing of the engine, so an edited engine is judged like the real one.

    An invariant that raises an error does not hold. `inv.settles` and
    `inv.client-is-told` are asked only of a run in which nothing is left to happen.
    """
    name = definition.name
    calls = []
    for trace in traces:
        calls.append(f"(do (set wf.run {shen.term(situations(trace))}) ok)")
        calls += [f"(inv.first {inv} ({name}.definition) (value wf.run) 0)" for inv in STATE]
        calls += [f"({inv} ({name}.definition) (value wf.run))" for inv in TRACE]
        calls.append(f"({LIVE} ({name}.settles-within) (value wf.run))")
        calls.append(f"({TOLD} ({name}.definition) (value wf.run))")
    values = iter(shen.evaluate(definition.files, calls, timeout=540))
    out = []
    for trace in traces:
        require(next(values) == "ok", "invariants: a trace could not be read by shen-jit")
        bad = {inv: f"step {v}" for inv in STATE if (v := next(values)) != -1}
        bad.update({inv: v for inv in TRACE if (v := next(values)) is not True})
        for inv in (LIVE, TOLD):
            holds = next(values)
            if trace["end"] == "quiescent" and holds is not True:
                bad[inv] = holds
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


def tested(tree, leaf, out):
    """The constants a residual program compares input `leaf` with."""
    if isinstance(tree, list):
        if tree[0] == "eq" and tree[1] == ["at", leaf] and type(tree[2]) is int:
            out.add(tree[2])
        for child in tree[1:]:
            tested(child, leaf, out)
    return out


PC = 6          # the input index of Pc: [Mode Seq [Kind A B] [Phase Pc ...]]


def unspecialized(definition):
    """Try to compile the engine with its definition as runtime data: the definition's
    leaves become part of the state and only its list structure stays static. Returns the
    compiler's refusal, or None if it compiled."""
    name = definition.name
    value, = shen.evaluate(definition.files, [f"({name}.definition)"])
    shape = lambda v: [shape(x) for x in v] if isinstance(v, tuple) else (
        "sym" if type(v) is str else "num")
    described = manifest(name, definition.keys, definition.names, SYNTAX)
    described.update(state=[shape(value), described["state"]], step="generic.step",
                     init="generic.init")
    source = definition.text + f"""
(define generic.init _ _ -> [({name}.definition) (wf.init ({name}.definition))])
(define generic.step [Def State] Event -> (generic.pair Def (wf.step Def State Event)))
(define generic.pair Def [State Effects] -> [[Def State] Effects])
"""
    try:
        specialize(Schema(described, definition.directory), source)
    except Unsupported as error:
        return str(error)
    return None


def specialization(work, report):
    # A definition is validated in Shen before anything is compiled from it. The retained
    # fixtures are each refused for the reason they were written for; before this check
    # existed two of them compiled and ran, one without ever executing its second activity.
    for fixture, wanted in REJECTED.items():
        found = problems(HERE / "defs" / "rejected" / f"{fixture}.shen", "rejected")
        require([problem[0] for problem in found] == [wanted],
                f"defs/rejected/{fixture}.shen: expected only {wanted}, found {found}")
    report(f"definition.shen refuses the {len(REJECTED)} retained fixtures, each for its own "
           f"reason ({', '.join(REJECTED.values())}), and accepts the {len(DEFINITIONS)} "
           f"definitions compiled below")
    engine = (HERE / "engine.shen").read_bytes()
    built = {}
    for name in DEFINITIONS:
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
        # What is left of the definition's structure is its positions: Pc is runtime data,
        # and the residual program compares it with each index of the program.
        positions = tested(json.loads(plain), PC, set())
        require(positions == set(range(definition.length)),
                f"{name}: the residual program tests Pc against {sorted(positions)}")
        report(f"specialized engine.shen to {name}: {native.stats}; residual symbols "
               f"{len(used)}, none of {'/'.join(SYNTAX)}; the residual program is identical "
               f"when those words are given integer codes; it tests Pc against exactly the "
               f"{definition.length} positions of the program")
    require((HERE / "engine.shen").read_bytes() == engine, "the engine changed between builds")
    trees = {name: (work / name / "build" / "node.ir.json").read_text() for name in built}
    require(len(set(trees.values())) == len(built), "two definitions gave the same residual program")
    keys = {name: set(built[name][0].keys) for name in built}
    for name, tree in trees.items():
        seen = constants(json.loads(tree), set())
        others = set().union(*(keys[other] for other in keys if other != name))
        require(keys[name] <= seen and not others & seen,
                f"{name}: the residual program does not hold exactly its own keys")
    report(f"one engine (sha256 {hashlib.sha256(engine).hexdigest()[:16]}), one compiler, "
           f"{len(built)} different residual programs; each contains the idempotency keys of its own "
           f"definition as constants and none of another's")

    # There is no unspecialized native engine to measure against: the compiler refuses it.
    refusals = {name: unspecialized(built[name][0]) for name in ("single", "order")}
    require(all(refusals.values()), f"the engine compiled with a runtime definition: {refusals}")
    report(f"the engine with its definition as runtime data (leaves in the state, list "
           f"structure static) is refused by the compiler, for single and for order: "
           f"{refusals['single']}")

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
                elif step["effects"][0][3] == "late":
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


FAULTS = ("crash", "drop", "dup", "lost")


def faultless(trace):
    return not any(kind in FAULTS for kind, _, _ in situations(trace))


def final_phase(trace):
    return phase(trace["steps"][-1]["world"]["states"][0])


def settle_steps(trace):
    """Steps from the last fault to the first state at rest (what inv.settles bounds)."""
    steps = situations(trace)
    last = max((n for n, (kind, _, _) in enumerate(steps) if kind in FAULTS), default=0)
    for n in range(last, len(steps)):
        node = steps[n][2][0]
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
    # The Maude translation of the engine on the same pairs. For order and docs this is the
    # only place Maude meets a branch, an `each` and a sleep on these definitions.
    check.maude_refinement(maude.step_module(definition.text), schema, pairs)
    report(f"{name}: native equals shen-jit running engine.shen, and the Maude translation "
           f"of engine.shen, on all {len(pairs)} distinct (state, event) pairs of the runs; "
           f"native equals shen-jit on the initial state")

    bad = [(seed, sorted(found)) for seed, found in enumerate(judge(definition, traces)) if found]
    require(not bad, f"{name}: invariants violated on seeds {bad[:5]}")
    worst = max(settle_steps(t) for t in traces)
    bound, = shen.evaluate(definition.files, [f"({name}.settles-within)"])
    quiet = [t for t in traces if faultless(t)]
    require(quiet and not any(final_phase(t) == "stuck" for t in quiet),
            f"{name}: a run without a fault ended stuck")
    report(f"{name}: the {len(STATE)} state invariants hold in every step and the "
           f"{len(TRACE) + 2} run invariants on every run; every run is at rest within {worst} "
           f"steps of its last fault (declared bound {bound}); {len(quiet)} of the runs had no "
           f"fault at all and none of those ended stuck")

    # Fault-free schedules only, so that inv.stuck-needs-a-fault is asked of many runs in
    # which something is compensated: cancels, failures and timeouts still happen.
    calm = [sim.run(native, workers(definition), CALM, seed) for seed in range(CALM_SEEDS)]
    require({t["end"] for t in calm} == {"quiescent"} and all(map(faultless, calm)),
            f"{name}: a fault-free run did not end quiescent and fault-free")
    bad = [(seed, sorted(found)) for seed, found in enumerate(judge(definition, calm)) if found]
    require(not bad, f"{name}: invariants violated on fault-free seeds {bad[:5]}")
    ends = Counter(final_phase(t) for t in calm)
    require(ends["compensated"] and ends["completed"] and not ends["stuck"],
            f"{name}: fault-free runs ended {dict(ends)}")
    report(f"{name}: {CALM_SEEDS} more seeds with no fault in the budget: every invariant "
           f"holds; {ends['completed']} end completed, {ends['compensated']} compensated, "
           f"none stuck")
    return traces, pairs


# ---- 3. bounded search

def graph(native, workload, prof, max_states=400000, world=sim.World):
    """Every reachable world of an untimed profile, as sim.explore visits them, with the
    edges kept. Returns observations, executed pairs, and per state its situation, whether
    anything but a fault can still happen there, and its successors."""
    world = world(native, workload, prof)
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
        sits.append(sim.freeze(situation(world.snapshot(), world.client[0], world.client[2])))
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


def two_steps(before, after):
    """Two situations as a run of two steps, for an invariant over runs."""
    return shen.term([["step", 0, before], ["step", 0, after]])


def regressions(definition, sits, edges):
    """inv.never-regresses over every path of a graph, crashes and restarts included.

    For each state, the finished workflows (as inv.finished reads a node) that some path
    to it has shown are collected; the invariant is then evaluated on every distinct pair
    (a node that showed one, a later node), which is every pair it could fail on. Returns
    (pairs judged, pairs that regress).
    """
    nodes = list(dict.fromkeys(sit[0] for sit in sits))
    shown = shen.evaluate(definition.files, [f"(inv.finished {shen.term(node)})" for node in nodes])
    finished = [node for node, phase in zip(nodes, shown) if phase != "none"]
    bit = {node: 1 << n for n, node in enumerate(finished)}
    before = [0] * len(sits)           # the finished nodes on some path to a state, as bits
    queue, queued = deque(range(len(sits))), [True] * len(sits)
    while queue:
        n = queue.popleft()
        queued[n] = False
        here = before[n] | bit.get(sits[n][0], 0)
        for m in edges[n]:
            if here & ~before[m]:
                before[m] |= here
                if not queued[m]:
                    queued[m] = True
                    queue.append(m)
    pairs = dict.fromkeys((earlier, sit[0]) for sit, mask in zip(sits, before)
                          for earlier in finished if mask & bit[earlier] and earlier != sit[0])
    blank = lambda node: [node, [], [], [], []]
    verdicts = shen.evaluate(definition.files, [
        f"(inv.never-regresses ({definition.name}.definition) {two_steps(blank(a), blank(b))})"
        for a, b in pairs], timeout=540)
    return len(pairs), [pair for pair, verdict in zip(pairs, verdicts) if verdict is not True]


def edge_regressions(definition, sits, edges):
    """The same invariant on single transitions only. This is what the check used to do; it
    cannot see a regression across a crash, and is kept to show that."""
    moves = dict.fromkeys((sits[n][0], sits[m][0]) for n, following in enumerate(edges)
                          for m in following if sits[n][0] != sits[m][0])
    blank = lambda node: [node, [], [], [], []]
    verdicts = shen.evaluate(definition.files, [
        f"(inv.never-regresses ({definition.name}.definition) {two_steps(blank(a), blank(b))})"
        for a, b in moves], timeout=540)
    return [move for move, verdict in zip(moves, verdicts) if verdict is not True]


def verdict_term(name, node):
    """inv.verdict of the matched world, as a Maude term. `verdict` is memoized: a search
    visits many more worlds than there are distinct situations."""
    return f"verdict(f.{name}.definition, {node}, RW:Val, PD:Val, LG:Val, TD:Val)"


def world_pattern(node):
    return f"{{ {node} disk(0, RW:Val, PD:Val) client(LG:Val, CC:Nat, TD:Val) REST:Soup }}"


QUIET = """mod WORKFLOW-SEARCH is
  protecting ENV .
  op busy : Soup -> Bool .
  eq busy(ev(I:Int, E:Val) R:Soup) = true .
  eq busy(lev(I:Int, E:Val) R:Soup) = true .
  eq busy(out(V:Val) R:Soup) = true .
  eq busy(down(I:Int) R:Soup) = true .
  eq busy(R:Soup) = false [owise] .
  op who : Soup -> Val .                    --- the node's memory, or down
  eq who(node(0, S:Val) R:Soup) = S:Val .
  eq who(down(0) R:Soup) = q('down) .
  --- inv.verdict of a situation, from its parts as the world holds them
  op verdict : Val Val Val Val Val Val ~> Val [memo] .
  eq verdict(D:Val, S:Val, RW:Val, PD:Val, LG:Val, TD:Val)
   = f.inv.verdict(D:Val, cons(S:Val, cons(RW:Val, cons(f.inv.tails(PD:Val),
       cons(LG:Val, cons(TD:Val, nil)))))) .
endm
"""


def maude_model(definition, prof, cancels, outcomes):
    return (maude.step_module(definition.text + "\n" + INVARIANTS.read_text())
            + maude.environment_module(definition.schema, prof,
                                       maude_workers(definition.keys, outcomes, cancels))
            + QUIET)


def at_rest_term(node, told):
    """The translated Shen predicate a world with nothing left to happen must satisfy: the
    node at rest, and, where no report can have been lost, the client told."""
    if not told:
        return f"f.inv.resting?({node})"
    return (f"f.inv.told-at-rest?(cons({node}, cons(RW:Val, cons(f.inv.tails(PD:Val), "
            f"cons(LG:Val, cons(TD:Val, nil))))))")


def maude_clean(definition, modules, told):
    """One search of the Maude model for any reachable state that violates a translated
    state invariant, or in which nothing is left to happen and the node is not at rest
    (`told`: or the client was not told). True when there is none. (maude_violation asks
    the questions separately and reads the state back; this asks them in one pass.)"""
    node = "who(REST:Soup)"
    output = maude.run(modules, [
        f"search [1] in WORKFLOW-SEARCH : init =>* "
        f"{{ disk(0, RW:Val, PD:Val) client(LG:Val, CC:Nat, TD:Val) REST:Soup }} "
        f"such that {verdict_term(definition.name, node)} =/= nil "
        f"or (not busy(REST:Soup) and PD:Val == nil and CC:Nat == 0 "
        f"and {at_rest_term(node, told)} =/= b(true)) .\n"], timeout=540)
    require(output.count("No solution.") + output.count("Solution 1") == 1,
            f"maude search did not finish:\n{output[-1500:]}")
    return "No solution." in output


def maude_violation(definition, modules, wanted=None):
    """Search the Maude model for a reachable state in which a translated Shen invariant
    does not hold (`wanted`: that one in particular; inv.settles: a world with nothing left
    to happen that is not at rest; inv.client-is-told: one in which the client was not told
    either). Returns the state as a situation, or None."""
    name = definition.name
    if wanted in (LIVE, TOLD):
        searches = [f"search [1] in WORKFLOW-SEARCH : init =>* {world_pattern('node(0, NS:Val)')} "
                    f"such that not busy(REST:Soup) and PD:Val == nil and CC:Nat == 0 "
                    f"and {at_rest_term('NS:Val', wanted == TOLD)} =/= b(true) .\n"]
    else:
        test = (lambda node: f"{verdict_term(name, node)} =/= nil") if wanted is None else (
            lambda node: f"f.element?(q('{wanted}), {verdict_term(name, node)}) =/= b(false)")
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
        for other in ("NS:Val", "RW:Val", "PD:Val", "LG:Val", "CC:Nat", "TD:Val", "REST:Soup"):
            text = text.split(f"{other} --> ", 1)[0]
        return maude.value(maude.read_terms(text)[0])

    pending = [row[1:] for row in bound("PD:Val")]
    return [bound("NS:Val", "down"), bound("RW:Val"), pending, bound("LG:Val"), bound("TD:Val")]


def bounded(built, report):
    everything = {}
    for label, (name, budget, cancels, outcomes) in BOUNDS.items():
        definition, native = built[name]
        limit, = shen.evaluate(definition.files, [f"({name}.settles-within)"])
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
        # A report in flight can be lost only where a loss is in the budget; everywhere
        # else a world with nothing left to happen must also have told the client.
        told = budget["drop"] == 0
        if not maude_clean(definition, modules, told):
            found = maude_violation(definition, modules)
            require(found is None, f"bound '{label}': Maude reaches a state violating an invariant: {found}")
            found = maude_violation(definition, modules, TOLD if told else LIVE)
            raise CheckFailure(f"bound '{label}': Maude reaches a world with nothing left to "
                               f"happen that is not at rest, or has not told the client: {found}")
        distinct = list(dict.fromkeys(sits))
        verdicts = shen.evaluate(definition.files, [
            f"(inv.verdict ({name}.definition) {shen.term(sit)})" for sit in distinct], timeout=540)
        bad = [(sit, v) for sit, v in zip(distinct, verdicts) if v != ()]
        require(not bad, f"bound '{label}': a reachable situation violates {bad[:1]}")
        # inv.append-only on every edge, by what it reads: the rows before and after. Being
        # a prefix is transitive, so this is the invariant on every path.
        moves = dict.fromkeys((sits[n][1], sits[m][1]) for n, following in enumerate(edges)
                              for m in following if sits[n][1] != sits[m][1])
        blank = lambda rows: ["down", rows, [], [], []]
        require(all(v is True for v in shen.evaluate(definition.files, [
            f"(inv.append-only ({name}.definition) {two_steps(blank(a), blank(b))})"
            for a, b in moves], timeout=540)),
                f"bound '{label}': a transition changes the durable history")
        # inv.never-regresses is not a property of single edges: on every path.
        judged, regress = regressions(definition, sits, edges)
        require(not regress, f"bound '{label}': a finished workflow regresses: {regress[:1]}")
        # Liveness, exhaustively: the graph has no cycle, so every schedule ends, and it
        # can only end where nothing but a fault could still happen; such a world is at rest.
        longest = longest_path(edges)
        require(longest is not None, f"bound '{label}': some schedule never ends")
        resting = shen.evaluate(definition.files, [
            f"(inv.told-at-rest? {shen.term(sit)})" if told else f"(inv.resting? {shen.term(sit[0])})"
            for sit in dict.fromkeys(sit for sit, quiet in zip(sits, idle) if quiet)])
        require(resting and all(v is True for v in resting),
                f"bound '{label}': nothing is left to happen in a world that is not at rest"
                + (", or has not told the client" if told else ""))
        require(longest <= limit, f"bound '{label}': a schedule of {longest} steps exceeds "
                                  f"the declared {limit}")
        cuts = boundaries(records)
        require(budget["crash"] == 0 or all(all(counts) for counts in cuts.values()),
                f"bound '{label}': an effect boundary was never crashed at: {cuts}")
        # The verdicts above are not about unfinished workflows only: each bound shows
        # completed, compensated and stuck ones.
        ends = Counter(sit[0][3][0] for sit in distinct if sit[0] != "down"
                       and sit[0][0] == "live" and sit[0][3][0] in TERMINAL)
        require(all(ends[outcome] for outcome in TERMINAL),
                f"bound '{label}': finished workflows shown: {dict(ends)}")
        everything.setdefault(name, {}).update(pairs)
        report(f"bounded search '{label}' ({name}, budget {budget}, cancels {cancels}, outcomes "
               f"{outcomes}): Maude {states} states, simulator {len(sits)}; the same "
               f"{len(reached)} observations; no state violates a state invariant (Maude search "
               f"over the translated Shen, and shen-jit on the {len(distinct)} distinct "
               f"situations, of which {ends['completed']} show a completed workflow, "
               f"{ends['compensated']} a compensated and {ends['stuck']} a stuck one); "
               f"{len(moves)} distinct changes of the durable history only append; no "
               f"finished workflow regresses on any path ({judged} pairs of an earlier "
               f"finished node and a later node); no cycle, longest schedule {longest} steps "
               f"(declared {limit}), all {len(resting)} situations where nothing is left to "
               f"happen are at rest" + (" and have told the client the outcome" if told else "")
               + (f"; the node died at every boundary of all {len(cuts)} step shapes"
                  if budget["crash"] else ""))
    count = 0
    for name, pairs in everything.items():
        definition, native = built[name]
        count += check.refinement(definition.schema, native, pairs)
        check.maude_refinement(maude.step_module(definition.text), definition.schema, pairs)
    report(f"bounded search: native, shen-jit and the Maude translation agree on all {count} "
           f"(state, event) pairs the {len(BOUNDS)} searches executed")
    return everything


# ---- what the guarantees rest on

def reliance(order, order_native, small, small_native, report):
    # The workers' side of "one effect per activity", in two halves. With workers that
    # ignore the key, the same engine binary runs an activity twice, and the invariant
    # says so: it counts executions in the environment, not what the engine believes.
    careless = seeded(order_native, order, range(100), careless=True)
    doubled = sum("one-effect-per-activity" in found for found in judge(order, careless))
    require(doubled, "one-effect-per-activity holds even with workers that ignore the key")
    report(f"reliance on the workers, first half: with workers that run every request they "
           f"receive, the unedited engine violates one-effect-per-activity on {doubled} of "
           f"100 seeds")

    # With workers that never commit a key twice but keep no tombstone, a request still in
    # flight when its undo found nothing commits afterwards. No crash, loss, duplication
    # or cancel is needed for it: a timeout is enough. The search is exhaustive.
    prof = sim.profile(timed=False, observe_states=True, budget={"crash": 0, "drop": 0, "dup": 0})
    sits = graph(small_native, Workers(small.keys, FLAKY, 0, tombstones=False), prof)[3]
    distinct = list(dict.fromkeys(sits))
    verdicts = shen.evaluate(small.files, [
        f"(inv.verdict (small.definition) {shen.term(sit)})" for sit in distinct], timeout=540)
    late = [sit for sit, v in zip(distinct, verdicts) if v != ()]
    require(late and all(v in ((), ("compensation-matches",)) for v in verdicts),
            f"workers without tombstones: {Counter(verdicts).most_common(3)}")
    report(f"reliance on the workers, second half: with workers that never commit a key "
           f"twice but forget an undo that found nothing committed, the unedited engine "
           f"violates compensation-matches, and nothing else, in {len(late)} of the "
           f"{len(distinct)} situations of an exhaustive search of small with no fault and no "
           f"cancel in the budget ({len(sits)} states)")

    # The environment's rule that no completion crosses a crash. The engine does not lean
    # on it: it checks the number of an acknowledgement and of a read result itself, and a
    # timer by the Seq it was armed at. In a simulator that keeps completions across a
    # crash it is still safe, and an engine without the acknowledgement check is not,
    # although the standard simulator cannot tell the two apart.
    prof = sim.profile(timed=False, observe_states=True, budget={"crash": 1, "drop": 0, "dup": 0})
    outcomes = {"hold": (1,), "pay": (1,)}
    blind = small.mutant("ack-token-ignored", BLIND)
    blind_native = build(blind.schema, blind.directory / "build")
    found = {}
    for label, definition, native, world in (
            ("the engine, completions kept", small, small_native, worlds.KeepsCompletions),
            ("no token check, standard", blind, blind_native, sim.World),
            ("no token check, completions kept", blind, blind_native, worlds.KeepsCompletions)):
        sits = graph(native, Workers(small.keys, outcomes, 0), prof, world=world)[3]
        distinct = list(dict.fromkeys(sits))
        verdicts = shen.evaluate(definition.files, [
            f"(inv.verdict (small.definition) {shen.term(sit)})" for sit in distinct], timeout=540)
        found[label] = (len(sits), sum(v != () for v in verdicts),
                        sorted({name for v in verdicts if v != shen.TRAP for name in v}))
    require(not found["the engine, completions kept"][1] and not found["no token check, standard"][1]
            and "history-is-true" in found["no token check, completions kept"][2],
            f"stale completions: {found}")
    report(f"reliance on the environment (small, one crash, searched exhaustively): in a "
           f"simulator that delivers acknowledgements, read results and timers across a crash "
           f"the engine still satisfies every state invariant ({found['the engine, completions kept'][0]} "
           f"states); an engine that ignores the number of an acknowledgement is not caught by "
           f"the standard simulator ({found['no token check, standard'][0]} states) and violates "
           f"{found['no token check, completions kept'][2]} in the other")


# ---- 4. mutants

def one_step_removed(native, workload, trace):
    actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
    for n in range(len(actions)):
        try:
            yield sim.script(native, workload, trace["profile"], actions[:n] + actions[n + 1:])
        except sim.Divergence:
            continue


def first_failing_seed(binary, broken, fails, limit=2000, batch=50):
    for start in range(0, limit, batch):
        seeds = range(start, start + batch)
        for seed, bad in zip(seeds, fails(seeded(binary, broken, seeds))):
            if bad:
                return seed
    raise CheckFailure(f"{broken.name}: no seed below {limit} shows the violation")


def mutants(work, built, report, record):
    retained_path = HERE / "mutants.json"
    retained = {} if record else json.loads(retained_path.read_text())
    order, native = built["order"]
    # What the unedited engine does on the order definition, for the Maude step check.
    real_pairs = check.step_pairs(seeded(native, order, range(SEEDS)))
    for label, mutant in MUTANTS.items():
        wanted = mutant["invariant"]
        broken = order.mutant(label, mutant["edits"])
        binary = build(broken.schema, broken.directory / "build")
        fails = lambda traces: [wanted in found for found in judge(broken, traces)]
        if record:
            seed = first_failing_seed(binary, broken, fails)
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
        # The Maude step check must reject the translation of the mutant: it differs from
        # the unedited native code on the pairs of the unedited seeded runs.
        try:
            check.maude_refinement(maude.step_module(broken.text), order.schema, real_pairs)
        except CheckFailure:
            pass
        else:
            raise CheckFailure(f"{label}: the Maude step check accepted a translation of the mutant")
        # The same mutant of the engine on a small definition, searched exhaustively.
        where = "the invariant reads clock times, so it has no exhaustive counterpart"
        if mutant["bound"] is not None:
            tiny_name, budget, cancels, outcomes = BOUNDS[mutant["bound"]]
            tiny = built[tiny_name][0].mutant(label, mutant["edits"])
            prof = sim.profile(timed=False, observe_states=True, budget=budget)
            state = maude_violation(tiny, maude_model(tiny, prof, cancels, outcomes), wanted)
            require(state is not None, f"{label}: the bounded search '{mutant['bound']}' does not "
                                       f"reach a violation of {wanted}")
            if wanted in (LIVE, TOLD):
                verdict, = shen.evaluate(tiny.files, [
                    f"(inv.told-at-rest? {shen.term(state)})" if wanted == TOLD
                    else f"(inv.resting? {shen.term(state[0])})"])
                require(verdict is False, f"{label}: shen-jit calls Maude's state at rest"
                                          + (" and the client told" if wanted == TOLD else ""))
            else:
                verdict, = shen.evaluate(tiny.files, [
                    f"(inv.verdict ({tiny.name}.definition) {shen.term(state)})"])
                require(verdict == shen.TRAP or wanted in verdict,
                        f"{label}: shen-jit does not confirm Maude's violation: {verdict}")
            where = (f"in the bounded search '{mutant['bound']}' on the {tiny_name} definition "
                     f"Maude reaches a violating state and shen-jit confirms it")
        if mutant.get("regresses"):
            # Teeth of the path-sensitive regression check. A run is a graph that is one
            # path. On the runs of this mutant that regress across a crash, no single
            # transition regresses, and following the path finds it.
            runs = seeded(binary, broken, range(120))
            runs = [t for t, found in zip(runs, judge(broken, runs)) if "inv.never-regresses" in found]
            require(runs, f"{label}: no seed below 120 regresses")
            on_paths = on_edges = 0
            for run in runs:
                sits = [sim.freeze(sit) for _, _, sit in situations(run)]
                edges = [[n + 1] for n in range(len(sits) - 1)] + [[]]
                on_edges += bool(edge_regressions(broken, sits, edges))
                on_paths += bool(regressions(broken, sits, edges)[1])
            require(on_paths == len(runs) and not on_edges,
                    f"{label}: of {len(runs)} regressing runs, {on_paths} found along the path "
                    f"and {on_edges} on single transitions")
            where += (f"; of 120 seeds {len(runs)} regress across a crash: judged as graphs, "
                      f"inv.never-regresses fails along the path in all of them and on a "
                      f"single transition in none")
        report(f"mutant {label} ({mutant['what']}): {wanted}"
               f"{' and ' + mutant['also'] if 'also' in mutant else ''} fail at seed "
               f"{kept['seed']} ({kept['steps']} steps, sha256 {kept['sha256'][:16]}); minimal "
               f"trace {kept['minimal_steps']} steps replays, still fails and is 1-minimal; "
               f"refinement rejects the binary; {where}")
    if record:
        retained_path.write_text(json.dumps(retained, indent=2, sort_keys=True) + "\n")
        for stale in set(p.name for p in (HERE / "mutants").glob("*.trace.json")) - {
                f"{label}.trace.json" for label in MUTANTS}:
            (HERE / "mutants" / stale).unlink()
    covered = {m["invariant"] for m in MUTANTS.values()} | {
        m["also"] for m in MUTANTS.values() if "also" in m}
    declared = set(STATE + TRACE + [LIVE, TOLD])
    require(covered == declared, f"invariants without a retained mutant: {declared - covered}")
    require(set(retained) == set(MUTANTS), f"mutants.json and MUTANTS differ: "
                                           f"{set(retained) ^ set(MUTANTS)}")


def main(report=print, record=False, sections=SECTIONS):
    unknown = set(sections) - set(SECTIONS)
    require(not unknown, f"unknown sections {sorted(unknown)}; there are {SECTIONS}")
    require(not record or "mutants" in sections, "record regenerates the mutants section")
    clock = time.perf_counter()

    def timed(section):
        nonlocal clock
        now = time.perf_counter()
        print(f"[{section}: {now - clock:.0f} s]", file=sys.stderr, flush=True)
        clock = now

    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        built = specialization(work, report if "specialization" in sections else lambda _: None)
        timed("specialization")
        if "simulation" in sections:
            for name in ("order", "docs"):
                simulation(*built[name], report)
            timed("simulation")
        if "bounded" in sections:
            bounded(built, report)
            timed("bounded")
        if "reliance" in sections:
            reliance(*built["order"], *built["small"], report)
            timed("reliance")
        if "mutants" in sections:
            mutants(work, built, report, record)
            timed("mutants")
    report("workflow: ok" if set(sections) == set(SECTIONS)
           else f"workflow: ok ({', '.join(s for s in SECTIONS if s in sections)} only)")


if __name__ == "__main__":
    main(sections=tuple(sys.argv[1:]) or SECTIONS)
