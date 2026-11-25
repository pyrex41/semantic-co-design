"""Checkers over simulator traces: refinement, Shen invariants, Maude agreement, shrinking.

Everything here is application-independent. An example's own check.py chooses
profiles, workloads, witnesses and mutants and calls these functions.
"""
from pathlib import Path

from . import maude, shen, sim

HELPERS = Path(__file__).with_name("check.shen")


class CheckFailure(AssertionError):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def step_pairs(traces):
    """Every distinct (state, event) a node handled, with the native result the trace recorded."""
    pairs = {}
    for trace in traces:
        for step in trace["steps"]:
            if "pre" in step:
                key = sim.freeze((step["pre"], step["event"]))
                result = sim.freeze((step["post"], step["effects"]))
                require(pairs.setdefault(key, result) == result,
                        f"the native step gave two results for {key}")
    return pairs


def refinement(schema, native, pairs, *, nodes=1, source_file=None):
    """The native step and the native initial state equal the Shen source run in shen-jit.

    `pairs` maps (state, event) to the native result, as `step_pairs` collects them from
    traces and `sim.explore` from a bounded search. One shen-jit process evaluates all of
    them. Returns the number of pairs; raises CheckFailure with the first difference.
    """
    source_file = source_file or schema.directory / schema.manifest["spec"]
    calls = [f"({schema.init} {node} {nodes})" for node in range(nodes)]
    calls += [f"({schema.step} {shen.term(state)} {shen.term(event)})" for state, event in pairs]
    values = shen.evaluate([source_file], calls)
    for node in range(nodes):
        require(values[node] != shen.TRAP
                and schema.encode_state(values[node]) == native.init(node, nodes),
                f"refinement: init of node {node} differs from the Shen source")
    for (state, event), result, source in zip(pairs, pairs.values(), values[nodes:]):
        require(result == source, f"refinement: on state {state} and event {event}\n"
                                  f"  native {result}\n  shen   {source}")
    return len(pairs)


def shen_world(world):
    return (world["up"], ["down" if s is None else s for s in world["states"]], world["disks"])


def shen_trace(trace):
    """A trace as the Shen value the invariants see: [[Kind Node Event Effects World] ...].

    A step during which the node died is two steps here: what it did, with the world as its
    applied effects left it, and then `crash` with the world after memory and pending writes
    were lost. An invariant therefore never judges an effect against a disk that only the
    crash produced.
    """
    out = []
    for step in trace["steps"]:
        effects = (step.get("effects") or [])[:step.get("applied", 0)]
        node, event = step.get("node", -1), step.get("event") or []
        if "before_crash" in step:
            out.append((step["kind"], node, event, effects, shen_world(step["before_crash"])))
            out.append(("crash", node, [], [], shen_world(step["world"])))
        else:
            out.append((step["kind"], node, event, effects, shen_world(step["world"])))
    return out


def effects_applied(schema, traces):
    """What each step did to the world, read from the snapshots and not from `applied`.

    For every step in which a node ran, the writes waiting on its disk and the outputs in
    flight must have grown by exactly the first `applied` disk-write and output effects of
    the step, measured before any crash. Returns, per trace, the list of
    (step index, effects visible in the world) for the steps during which the node died.
    """
    out = []
    for trace in traces:
        before = {"pending": [[] for _ in range(trace["profile"]["nodes"])], "outputs": []}
        cuts = []
        for step in trace["steps"]:
            if "effects" in step:
                seen, node = step.get("before_crash", step["world"]), step["node"]
                applied = step["effects"][:step["applied"]]
                writes = [list(e[2:]) for e in applied if schema.effects[e[0]].role == "disk-write"]
                outputs = [e for e in applied if schema.effects[e[0]].role == "output"]
                new_writes = seen["pending"][node][len(before["pending"][node]):]
                require(seen["pending"][node][:len(before["pending"][node])] == before["pending"][node]
                        and new_writes == writes,
                        f"step {step['i']} of seed {trace['seed']}: the trace says {len(writes)} "
                        f"writes were issued, the disk queue grew by {new_writes}")
                grown = list(seen["outputs"])
                for old in before["outputs"]:
                    require(old in grown, f"step {step['i']}: an output in flight disappeared")
                    grown.remove(old)
                require(sorted(map(str, grown)) == sorted(map(str, outputs)),
                        f"step {step['i']} of seed {trace['seed']}: the trace says outputs "
                        f"{outputs} were emitted, the world gained {grown}")
                if "before_crash" in step:
                    cuts.append((step["i"], len(new_writes) + len(grown)))
            before = step["world"]
        out.append(cuts)
    return out


def invariants(schema, traces, *, file=None, state=None, trace=None):
    """Evaluate the manifest's Shen invariants over traces, in one shen-jit process.

    A state invariant takes a World and a trace invariant takes the list of steps; both
    hold when they return true. Returns, per trace, a list of (invariant, detail).
    """
    declared = schema.invariants
    file = file or schema.directory / declared["file"]
    state = declared.get("state", []) if state is None else state
    trace = declared.get("trace", []) if trace is None else trace
    calls = []
    for item in traces:
        calls.append(f"(do (set tool.trace {shen.term(shen_trace(item))}) ok)")
        calls += [f"(tool.first-bad (/. W ({name} W)) (value tool.trace) 0)" for name in state]
        calls += [f"({name} (value tool.trace))" for name in trace]
    values = iter(shen.evaluate([HELPERS, file], calls))
    out = []
    for _ in traces:
        require(next(values) == "ok", "invariants: a trace could not be read by shen-jit")
        bad = [(name, f"step {v}") for name in state if (v := next(values)) != -1]
        bad += [(name, v) for name in trace if (v := next(values)) is not True]
        out.append(bad)
    return out


def predicates(file, names, traces):
    """Evaluate Shen predicates over whole traces (witness searches). Returns rows of values."""
    calls = []
    for item in traces:
        calls.append(f"(do (set tool.trace {shen.term(shen_trace(item))}) ok)")
        calls += [f"({name} (value tool.trace))" for name in names]
    values = shen.evaluate([HELPERS, file], calls)
    width = len(names) + 1
    return [values[n * width + 1:(n + 1) * width] for n in range(len(traces))]


def maude_refinement(modules, schema, pairs):
    """The translated Maude step reduces every (state, event) to the native result."""
    results = maude.reduce(modules, [
        f"f.{schema.step}({maude.term(state)}, {maude.term(event)})" for state, event in pairs])
    for (state, event), native, reduced in zip(pairs, pairs.values(), results):
        require(native == reduced, f"maude step: on state {state} and event {event}\n"
                                   f"  native {native}\n  maude  {reduced}")
    return len(pairs)


def maude_agreement(schema, native, workload, maude_workload, prof, *, source=None,
                    max_states=200000, timeout=300, world=sim.World, edit=None):
    """Reachable observations of the Maude model and of the simulator, for one untimed profile.

    Returns (simulator set, Maude set, facts). Equality is the caller's assertion, so a
    mutant can be required to differ: `world` is the simulator to run (tool/sim/mutants.py)
    and `edit` rewrites the Maude text before the search. `facts["pairs"]` are the native
    steps the exploration executed, for the refinement checks.
    """
    source = schema.source if source is None else source
    modules = maude.step_module(source) + maude.environment_module(schema, prof, maude_workload)
    if edit:
        modules = edit(modules)
    explored, found = sim.explore(native, workload, prof, max_states=max_states, world=world)
    reached, states, trap = maude.search(modules, timeout=timeout)
    return explored, reached, {"maude_states": states, "maude_observations": len(reached),
                               "sim_states": found["states"], "sim_edges": found["edges"],
                               "sim_observations": len(explored), "maude_trap": trap,
                               "pairs": found["pairs"], "max_pending": found["max_pending"]}


def shrink(native, workload, trace, still_fails):
    """Remove steps one at a time while the failure remains.

    `still_fails(traces)` returns one boolean per candidate trace. The result is 1-minimal:
    removing any single remaining step loses the failure or is not a valid schedule.
    """
    actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
    prof = trace["profile"]
    while True:
        candidates = []
        for n in reversed(range(len(actions))):
            shorter = actions[:n] + actions[n + 1:]
            try:
                candidates.append((shorter, sim.script(native, workload, prof, shorter)))
            except sim.Divergence:
                continue
        verdicts = still_fails([t for _, t in candidates]) if candidates else []
        kept = next((shorter for (shorter, _), bad in zip(candidates, verdicts) if bad), None)
        if kept is None:
            return sim.script(native, workload, prof, actions)
        actions = kept
