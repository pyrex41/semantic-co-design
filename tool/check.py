"""Checkers over simulator traces: refinement, Shen invariants, Maude agreement, shrinking.

Everything here is application-independent. An example's own check.py chooses
profiles, workloads, witnesses and mutants and calls these functions.
"""
from pathlib import Path

from . import maude, shen, sim
from .compile import initial_state

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
                pairs[key] = sim.freeze((step["post"], step["effects"]))
    return pairs


def refinement(schema, traces, inits, *, source_file=None):
    """At every simulated step the native result equals the Shen source run in shen-jit.

    One shen-jit process evaluates all distinct (state, event) pairs. Returns the number
    of pairs; raises CheckFailure with the first difference.
    """
    source_file = source_file or schema.directory / schema.manifest["spec"]
    pairs = step_pairs(traces)
    calls = [f"({schema.init} {node} {len(inits)})" for node in range(len(inits))]
    calls += [f"({schema.step} {shen.term(state)} {shen.term(event)})" for state, event in pairs]
    values = shen.evaluate([source_file], calls)
    for node, init in enumerate(inits):
        require(values[node] != shen.TRAP and schema.encode_state(values[node]) == tuple(init),
                f"refinement: init of node {node} differs from the Shen source")
    for (state, event), native, source in zip(pairs, pairs.values(), values[len(inits):]):
        require(native == source, f"refinement: on state {state} and event {event}\n"
                                  f"  native {native}\n  shen   {source}")
    return len(pairs)


def shen_trace(trace):
    """A trace as the Shen value the invariants see: [[Kind Node Event Effects World] ...]."""
    out = []
    for step in trace["steps"]:
        world = step["world"]
        states = ["down" if s is None else s for s in world["states"]]
        effects = step.get("effects") or []
        out.append((step["kind"], step.get("node", -1), step.get("event") or [],
                    effects[:step.get("applied", 0)], (world["up"], states, world["disks"])))
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


def maude_refinement(modules, schema, traces):
    """The translated Maude step reduces every simulated (state, event) to the same result."""
    pairs = step_pairs(traces)
    results = maude.reduce(modules, [
        f"f.{schema.step}({maude.term(state)}, {maude.term(event)})" for state, event in pairs])
    for (state, event), native, reduced in zip(pairs, pairs.values(), results):
        require(native == reduced, f"maude step: on state {state} and event {event}\n"
                                   f"  native {native}\n  maude  {reduced}")
    return len(pairs)


def maude_agreement(schema, native, workload, maude_workload, prof, inits, *, source=None):
    """Reachable observations of the Maude model and of the simulator, for one untimed profile.

    Returns (simulator set, Maude set, facts). Equality is the caller's assertion, so a
    mutant can be required to differ.
    """
    source = schema.source if source is None else source
    modules = maude.step_module(source) + maude.environment_module(schema, prof, maude_workload)
    reached, states, trap = maude.search(modules)
    explored, sim_states, edges = sim.explore(native, workload, prof, inits)
    return explored, reached, {"maude_states": states, "maude_observations": len(reached),
                               "sim_states": sim_states, "sim_edges": edges,
                               "sim_observations": len(explored), "maude_trap": trap}


def shrink(native, workload, inits, trace, still_fails):
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
                candidates.append((shorter, sim.script(native, workload, prof, inits, shorter)))
            except sim.Divergence:
                continue
        verdicts = still_fails([t for _, t in candidates]) if candidates else []
        kept = next((shorter for (shorter, _), bad in zip(candidates, verdicts) if bad), None)
        if kept is None:
            return sim.script(native, workload, prof, inits, actions)
        actions = kept


def inits_of(schema, nodes, source=None):
    return [initial_state(schema, node, nodes, source) for node in range(nodes)]
