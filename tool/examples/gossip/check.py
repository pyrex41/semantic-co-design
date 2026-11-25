"""Three nodes on a ring: exercises send, receive, timers, partitions and several instances
of one compiled step. Run: nix develop -c python3 -m tool check gossip"""
from pathlib import Path
from tempfile import TemporaryDirectory

from ... import check, maude, sim
from ...check import require
from ...compile import build
from ...schema import Schema
from .workload import Setters, maude as maude_setters

HERE = Path(__file__).parent
SEEDS = 200
VALUES = (3, 5, 4)
SEEDED = sim.profile(nodes=3, budget={"crash": 1, "drop": 2, "dup": 2, "partition": 2},
                     max_steps=300)
BOUND = sim.profile(nodes=2, timed=False, observe_states=True,
                    budget={"crash": 0, "drop": 1, "dup": 0})
BOUND_VALUES = (2,)
# Two more small bounds, so that the crash rules and the duplicate rules of the Maude
# environment are compared with the simulator on a node that sends and sets timers.
CRASH_BOUND = sim.profile(nodes=2, timed=False, observe_states=True,
                          budget={"crash": 1, "drop": 0, "dup": 0})
DUP_BOUND = sim.profile(nodes=2, timed=False, observe_states=True,
                        budget={"crash": 0, "drop": 0, "dup": 1})
SMALL_VALUES = (1,)
# A node that passes on a value it invented; its state space is unbounded, so it is only simulated.
MUTANT = ("[[tell (ring.next Id Nodes) New]]] where",
          "[[tell (ring.next Id Nodes) (+ New 1)]]] where")
# A node that always tells node 0: finite, and visible to the bounded search.
MISROUTED = ("(if (= (+ Id 1) Nodes) 0 (+ Id 1))", "(if (= (+ Id 1) Nodes) 0 0)")


def main(report=print, record=False):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        native = build(schema, Path(tmp) / "spec")
        workload = Setters(VALUES)
        traces = [sim.run(native, workload, SEEDED, seed) for seed in range(SEEDS)]
        require({t["end"] for t in traces} == {"quiescent"}, "simulation did not quiesce")
        require([sim.dumps(sim.run(native, workload, SEEDED, s)) for s in range(SEEDS)]
                == [sim.dumps(t) for t in traces], "determinism: a seed changed its trace")
        for trace in traces:
            sim.replay(native, workload, trace)
        check.effects_applied(schema, traces)
        kinds = {}
        for trace in traces:
            for step in trace["steps"]:
                kinds[step["kind"]] = kinds.get(step["kind"], 0) + 1
        require(all(kinds.get(k) for k in ("partition", "heal", "drop", "dup", "crash", "lost")),
                f"simulation never exercised a fault kind: {kinds}")
        report(f"simulation: 3 nodes, {SEEDS} seeds, {sum(kinds.values())} steps, byte-identical "
               f"on rerun, every trace replays; {dict(sorted(kinds.items()))}")
        simulated = check.step_pairs(traces)
        check.refinement(schema, native, simulated, nodes=3)
        require(not any(check.invariants(schema, traces)), "an invariant failed")
        found = check.predicates(HERE / "invariants.shen", ["wit.converged-after-partition"], traces)
        converged = sum(row[0] is True for row in found)
        require(converged, "no seed converged after a partition")
        report(f"invariants: inv.only-requested-values holds on all traces; {converged} seeds "
               f"converge to the largest value after a partition")

        # Two nodes, exhaustively, with node states in the observation.
        searched = {}
        for label, values, bound in (("drop", BOUND_VALUES, BOUND), ("crash", SMALL_VALUES, CRASH_BOUND),
                                     ("duplicate", SMALL_VALUES, DUP_BOUND)):
            mine, reached, facts = check.maude_agreement(
                schema, native, Setters(values), maude_setters(values, 2), bound)
            require(facts["maude_trap"] is None and mine == reached,
                    f"maude and simulator disagree on the {label} bound: "
                    f"{len(reached - mine)} only in Maude, {len(mine - reached)} only simulated")
            searched.update(facts["pairs"])
            if bound is BOUND:
                explored = mine
            report(f"bounded search (2 nodes, values {values}, budget {bound['budget']}): Maude "
                   f"{facts['maude_states']} states, simulator {facts['sim_states']}; the same "
                   f"{len(reached)} observations")
        check.refinement(schema, native, searched, nodes=2)
        pairs = {**simulated, **searched}
        check.maude_refinement(maude.step_module(schema.source), schema, pairs)
        report(f"refinement: native equals shen-jit and the Maude translation on {len(pairs)} "
               f"distinct (state, event) pairs: {len(simulated)} from the seeded runs and "
               f"{len(searched)} executed by the searches, and on the initial state of every node")
        # Teeth: a node that passes on a value it invented.
        source = schema.source
        require(source.count(MUTANT[0]) == 1, "mutation site")
        source = source.replace(*MUTANT)
        broken = build(schema, Path(tmp) / "mutant", source)
        runs = [sim.run(broken, workload, SEEDED, seed) for seed in range(20)]
        failing = [n for n, bad in enumerate(check.invariants(schema, runs)) if bad]
        require(failing, "the invariant accepted a node that invents values")
        require(source.count(MISROUTED[0]) == 1, "mutation site")
        source = schema.source.replace(*MISROUTED)
        misrouted = build(schema, Path(tmp) / "misrouted", source)
        mine, other, _ = check.maude_agreement(schema, misrouted, Setters(BOUND_VALUES),
                                               maude_setters(BOUND_VALUES, 2), BOUND,
                                               source=source)
        require(mine == other and other != explored,
                "bounded search does not distinguish a misrouting node")
        report(f"mutants: inv.only-requested-values fails on {len(failing)} of 20 seeds of a node "
               f"that invents values (first seed {failing[0]}); for a node that misroutes, Maude "
               f"and the simulator agree with each other and differ from the specification")
    report("gossip: ok")
