"""Checks of the simulator itself. Run: nix develop -c python3 -m tool check sim

The node is tool/sim/probe: one request makes two disk writes and two outputs in one
step, so the rules an application relies on are observable here even when the
application never has two effects in a step or two writes pending:

* a node that dies during a step performed exactly the effects before the cut,
* syncs happen in order and a crash keeps a prefix of the pending writes,
* no completion crosses a crash,
* a dropped or duplicated message takes no time,
* a trace replays only as the trace it claims to be, to its end, errors included.

Every rule has a simulator in tool/sim/mutants.py (or an edited Maude model) that breaks
it and that the corresponding check must reject.
"""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory

from . import check, maude, sim
from .check import CheckFailure, require
from .compile import build
from .schema import Schema
from .sim import Workload, mutants

HERE = Path(__file__).parent / "sim" / "probe"
SEEDS = 200
VALUES = (5, 6, 7)
SEEDED = sim.profile(budget={"crash": 2, "drop": 1, "dup": 1}, max_steps=200, crash_mid_rate=0.3)
# One request and one crash: what the crash left behind can be read off an observation.
ONE = sim.profile(timed=False, observe_states=True, budget={"crash": 1, "drop": 0, "dup": 0})
# The request may also be duplicated or lost: up to four writes wait on the disk at once.
TWO = sim.profile(timed=False, observe_states=True, budget={"crash": 1, "drop": 1, "dup": 1})
VALUE = 5
# A probe that has no clause for an acknowledgement: the first one is a Shen error.
TRAPPING = "  [Acks Loaded] [acked Token] -> [[(+ Acks 1) Loaded] [[say 3 Token]]])"


class Goes(Workload):
    """Hands a fixed sequence of `go` requests to node 0."""

    def __init__(self, values):
        self.values = tuple(values)

    def initial(self, profile):
        return (0,)

    def actions(self, world):
        sent = world.client[0]
        return [("go", self.values[sent])] if sent < len(self.values) else []

    def apply(self, world, action, draw):
        world.request(0, ("go", action[1]), draw)
        world.client = (world.client[0] + 1,)
        return {"node": 0, "event": ("go", action[1])}


def maude_goes(values):
    rules = ["  op client : Nat -> Thing [ctor] ."]
    for sent, value in enumerate(values):
        rules.append(f"  rl [go-{sent}] : {{ client({sent}) R }} => {{ client({sent + 1}) "
                     f"ev(0, cons(q('go), cons(i({value}), nil))) R }} .")
    return {"rules": "\n".join(rules), "init": "client(0)"}


# ---- what one observation of the single-request bound says

def seen(observation):
    rows = {row[0] for row in next(t[2] for t in observation if t[0] == "rows")}
    lost = {t[1][0] for t in observation if t[0] == "lostw"}
    state = next(t[2] for t in observation if t[0] == "state")
    return rows, lost, state


def in_order(observation):
    """Key 2 is written after key 1 in the same step: durable 2 implies durable 1."""
    rows, _, _ = seen(observation)
    return 2 not in rows or 1 in rows


def died_between_writes(observation):
    """The node is down, the first write was issued and the second never was."""
    rows, lost, state = seen(observation)
    return state == "down" and 1 in rows | lost and 2 not in rows | lost


def stale_completion(observation):
    """A restart read the durable row back and an acknowledgement still arrived afterwards.
    The single request was consumed before that restart, so no new write can explain it."""
    _, _, state = seen(observation)
    return state != "down" and state[0] >= 1 and state[1] == VALUE


# ---- seeded runs: determinism, replay, and what steps did to the world

def cut_sizes(schema, traces):
    """How many effects of a `go` step were visible in the world when the node died in it."""
    sizes = {}
    for trace, cuts in zip(traces, check.effects_applied(schema, traces)):
        for index, visible in cuts:
            if trace["steps"][index]["event"][0] == "go":
                sizes[visible] = sizes.get(visible, 0) + 1
    return dict(sorted(sizes.items()))


def fault_time(traces):
    """(drop and dup steps, how many of them moved the virtual clock)."""
    faults = moved = 0
    for trace in traces:
        for before, step in zip(trace["steps"], trace["steps"][1:]):
            if step["kind"] in ("drop", "dup"):
                faults += 1
                moved += step["t"] != before["t"]
    return faults, moved


def rejected(native, workload, trace, world=sim.World):
    try:
        sim.replay(native, workload, trace, world=world)
    except sim.Divergence:
        return True
    return False


def seeded(work, schema, native, report):
    workload = Goes(VALUES)
    traces = [sim.run(native, workload, SEEDED, seed) for seed in range(SEEDS)]
    require({t["end"] for t in traces} == {"quiescent"}, "probe: a seeded run did not quiesce")
    require([sim.dumps(sim.run(native, workload, SEEDED, s)) for s in range(SEEDS)]
            == [sim.dumps(t) for t in traces], "determinism: a seed changed its trace")
    for trace in traces:
        sim.replay(native, workload, trace)
    steps = sum(len(t["steps"]) for t in traces)
    report(f"seeded: {SEEDS} seeds, {steps} steps, all quiescent, byte-identical on rerun, "
           f"every trace replays")

    # A step did to the world exactly what its record says, read from the snapshots.
    sizes = cut_sizes(schema, traces)
    require(set(sizes) == {0, 1, 2, 3}, f"a crash never landed at some cut of a step: {sizes}")
    most = max(len(pending) for t in traces for s in t["steps"] for pending in s["world"]["pending"])
    require(most >= 2, "two writes were never pending at once")
    report(f"effects: in every step the disk queue and the outputs in flight grew by exactly "
           f"the effects recorded as applied; nodes died with 0, 1, 2 and 3 of a step's four "
           f"effects done {sizes}; up to {most} writes pending at once")
    for world in (mutants.AllEffectsOnCut, mutants.LateCut):
        try:
            cut_sizes(schema, [sim.run(native, workload, SEEDED, seed, world=world)
                               for seed in range(SEEDS)])
        except CheckFailure:
            continue
        raise CheckFailure(f"effects: {world.__name__} was not noticed")
    report("effects teeth: a simulator that performs every effect of a cut step, and one that "
           "cuts one effect late, are both rejected by the world snapshots")

    faults, moved = fault_time(traces)
    require(faults and not moved, f"{moved} of {faults} drop/dup steps moved the clock")
    slow = fault_time([sim.run(native, workload, SEEDED, seed, world=mutants.FaultsTakeTime)
                       for seed in range(SEEDS)])
    require(slow[1], "clock: a simulator whose faults take time was not noticed")
    report(f"clock: none of {faults} drop/dup steps moves virtual time; in a simulator where "
           f"faults advance the clock {slow[1]} of {slow[0]} do")
    return traces


def replay_teeth(work, schema, native, traces, report):
    workload = Goes(VALUES)
    victim = max(traces, key=lambda t: len(t["steps"]))
    require(len(victim["steps"]) > 12, "replay: no trace long enough to cut")

    def variant(change):
        edited = copy.deepcopy(victim)
        change(edited)
        return edited

    sync = next(n for n, s in enumerate(victim["steps"]) if s["kind"] == "sync")
    broken = {
        "one recorded value changed": lambda t: t["steps"][sync]["world"]["disks"][0][0].__setitem__(1, 99),
        "a middle step removed": lambda t: t["steps"].pop(sync),
        "the last step removed, end unchanged": lambda t: t["steps"].pop(),
        "the last 8 steps removed, end unchanged": lambda t: t["steps"].__delitem__(slice(-8, None)),
        "end rewritten to max_steps": lambda t: t.__setitem__("end", "max_steps"),
        "end rewritten to an error": lambda t: t.__setitem__("end", "error: node 0: bogus"),
        "end removed": lambda t: t.__setitem__("end", None),
        "seed changed": lambda t: t.__setitem__("seed", t["seed"] + 1),
        "fault rate changed": lambda t: t["profile"].__setitem__("fault_rate", 0.9),
        "step bound lowered below the length": lambda t: t["profile"].__setitem__("max_steps", 3),
    }
    for label, change in broken.items():
        require(rejected(native, workload, variant(change)),
                f"replay accepted a trace with {label}")

    # A scripted trace (no seed) is authenticated by its own recomputed end.
    actions = [(s["action"], s["draws"]) for s in victim["steps"] if s["action"] != ["boot"]]
    whole = sim.script(native, workload, SEEDED, actions)
    part = sim.script(native, workload, SEEDED, actions[:-8])
    require(whole["end"] == "quiescent" and part["end"] == "open", "script: wrong end")
    sim.replay(native, workload, whole)
    sim.replay(native, workload, part)
    cut = copy.deepcopy(whole)
    del cut["steps"][-8:]
    require(rejected(native, workload, cut), "replay accepted a cut script that claims to be quiescent")

    # A run that ends in a specification error replays to that error and nowhere else.
    source = schema.source
    require(source.count(TRAPPING) == 1, "probe: mutation site")
    trapping = build(schema, work / "trapping", source.replace(TRAPPING, ")"))
    failed = next(t for t in (sim.run(trapping, workload, SEEDED, seed) for seed in range(50))
                  if t["end"].startswith("error"))
    last = failed["steps"][-1]
    require(last["kind"] == "error" and last["action"][0] == "local", "the failing action is not recorded")
    sim.replay(trapping, workload, failed)
    require(rejected(native, workload, failed), "replay accepted an error trace on a binary that does not fail")
    for label, change in (("end rewritten to quiescent", lambda t: t.__setitem__("end", "quiescent")),
                          ("the failing step removed", lambda t: t["steps"].pop())):
        edited = copy.deepcopy(failed)
        change(edited)
        require(rejected(trapping, workload, edited), f"replay accepted an error trace with {label}")
    report(f"replay teeth: {len(broken)} edits of a {len(victim['steps'])}-step trace are each "
           f"rejected (cut tails, rewritten end, seed, rates, step bound); a cut script that "
           f"claims quiescence is rejected; a run ending in a specification error records the "
           f"failing action, replays to the same error on its binary, and is rejected on the "
           f"correct binary and with its end or failing step edited")


# ---- bounded search against the Maude environment

def bounded(schema, native, traces, report):
    pairs = check.step_pairs(traces)
    one, two = Goes((VALUE,)), maude_goes((VALUE,))
    explored, reached, facts = check.maude_agreement(schema, native, one, two, ONE)
    require(facts["maude_trap"] is None and explored == reached,
            f"probe: Maude and the simulator disagree: {len(reached - explored)} observations "
            f"only in Maude, {len(explored - reached)} only in the simulator")
    pairs.update(facts["pairs"])
    require(all(map(in_order, reached)), "sync order: key 2 is durable without key 1")
    between = sum(map(died_between_writes, reached))
    require(between, "no observation shows a node that died between its two writes")
    require(not any(map(stale_completion, reached)), "a completion crossed a crash")
    report(f"bounded (one request, {ONE['budget']}): Maude {facts['maude_states']} states, "
           f"simulator {facts['sim_states']}; the same {len(reached)} observations; durable "
           f"writes are always an in-order prefix; {between} observations show a node that "
           f"died between its two writes; no acknowledgement arrives after a restart")

    # Each broken rule is visible both as a property and against the Maude model.
    expect = {
        mutants.AllEffectsOnCut: lambda obs: not any(map(died_between_writes, obs)),
        mutants.NoMidStepCrash: lambda obs: not any(map(died_between_writes, obs)),
        mutants.SyncsNewest: lambda obs: not all(map(in_order, obs)),
        mutants.PersistsSuffix: lambda obs: not all(map(in_order, obs)),
        mutants.KeepsCompletions: lambda obs: any(map(stale_completion, obs)),
    }
    for world, broken in expect.items():
        mine, _ = sim.explore(native, one, ONE, world=world)
        require(broken(mine), f"{world.__name__}: its property still holds")
        require(mine != reached, f"{world.__name__}: Maude agreement does not notice it")
    edits = {
        "no crash between effects": ("px(Pre) PS := prefixes(Es)", "px(Pre) PS := px(nil)"),
        "a crash persists every pending write": ("px(Pre2) PS2 := allPrefixes(PW)", "Pre2 := PW"),
        "completions survive a crash": ("lostAll(after(Pre, PW)) purge(I, R)", "lostAll(after(Pre, PW)) R"),
    }
    for label, (old, new) in edits.items():
        def edit(modules, old=old, new=new):
            require(old in modules, f"Maude mutation site missing: {old}")
            return modules.replace(old, new)
        _, other, _ = check.maude_agreement(schema, native, one, two, ONE, edit=edit)
        require(other != explored, f"a Maude model in which {label} equals the simulator")
    report(f"bounded teeth: {len(expect)} broken simulators ({', '.join(w.__name__ for w in expect)}) "
           f"each lose their property and differ from Maude; {len(edits)} edited Maude models "
           f"({'; '.join(edits)}) each differ from the simulator")

    explored, reached, facts = check.maude_agreement(schema, native, one, two, TWO)
    require(facts["maude_trap"] is None and explored == reached,
            "probe: Maude and the simulator disagree with a duplicate and a drop")
    require(facts["max_pending"] == 4, f"expected four pending writes, saw {facts['max_pending']}")
    require(all(map(in_order, reached)), "sync order fails with four pending writes")
    pairs.update(facts["pairs"])
    report(f"bounded (one request, {TWO['budget']}): Maude {facts['maude_states']} states, "
           f"simulator {facts['sim_states']}; the same {len(reached)} observations with up to "
           f"{facts['max_pending']} writes pending")
    count = check.refinement(schema, native, pairs)
    check.maude_refinement(maude.step_module(schema.source), schema, pairs)
    report(f"refinement: native, shen-jit and the Maude translation agree on all {count} "
           f"(state, event) pairs the seeded runs and both searches executed")


def main(report=print, record=False):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        native = build(schema, work / "probe")
        traces = seeded(work, schema, native, report)
        replay_teeth(work, schema, native, traces, report)
        bounded(schema, native, traces, report)
    from .sim_options_check import main as options
    options(report)
    report("sim: ok")


if __name__ == "__main__":
    main()
