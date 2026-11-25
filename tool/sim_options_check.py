"""Checks of the simulator's optional profile keys. Part of: python3 -m tool check sim

    delay["timer"], "timer_rate"   a timer runs for its delay scaled by a drawn skew and by
                                   the rate of the node's clock
    budget["oneway"]               an asymmetric cut: everything into a node, or out of it
    budget["timer"]                a bound on timer events, so that an untimed search of a
                                   node that re-arms its timers is finite
    "slow", delay["net-slow"],     a drawn share of messages, or of disk operations, takes a
    delay["disk-slow"]             delay from a second range: late messages, a stalled disk

The node is tool/examples/gossip, which sends and sets timers; the stalled disk is shown on
tool/sim/probe, which writes. Each key has a simulator in tool/sim/mutants.py (or an edited
Maude model) that ignores it and that the check rejects.
"""
import collections
from pathlib import Path
from tempfile import TemporaryDirectory

from . import check, sim
from .check import require
from .compile import build
from .examples.gossip.workload import Setters, maude as maude_setters
from .schema import Schema
from .sim import mutants

HERE = Path(__file__).parent / "examples" / "gossip"


def alarms(trace):
    return [step["t"] for step in trace["steps"] if step.get("event", [None])[0] == "alarm"]


def skew(native, report):
    """One node, no faults: it arms a timer of 10 at boot and twice more when it fires."""
    quiet = Setters(())
    plain = sim.profile(nodes=1, budget={"crash": 0, "drop": 0, "dup": 0})
    cases = {"exact": ({}, [10, 20, 30]),
             "every timer runs double": ({"delay": {"timer": [200, 200]}}, [10, 30, 50]),
             "a clock at half rate": ({"timer_rate": [50]}, [5, 10, 15]),
             "both": ({"delay": {"timer": [200, 200]}, "timer_rate": [50]}, [5, 15, 25])}
    for label, (keys, expected) in cases.items():
        prof = sim.profile(**{**plain, **keys, "delay": {**plain["delay"], **keys.get("delay", {})}})
        trace = sim.run(native, quiet, prof, 0)
        require(alarms(trace) == expected, f"timer skew, {label}: alarms at {alarms(trace)}")
        sim.replay(native, quiet, trace)
        if label != "exact":
            ignored = sim.run(native, quiet, prof, 0, world=mutants.ExactTimers)
            require(alarms(ignored) == [10, 20, 30], "the exact-timer simulator is not exact")
    # A drawn skew: every re-armed timer runs for 10 scaled by the percentage its step drew.
    jitter = sim.profile(**{**plain, "delay": {**plain["delay"], "timer": [50, 150]}})
    spans = set()
    for seed in range(50):
        trace = sim.run(native, quiet, jitter, seed)
        sim.replay(native, quiet, trace)
        fired = [s for s in trace["steps"] if s.get("event", [None])[0] == "alarm"]
        for before, after in zip(fired, fired[1:]):
            percent = before["draws"][-1]
            require(50 <= percent <= 150 and after["t"] - before["t"] == 10 * percent // 100,
                    f"timer skew: seed {seed} drew {percent}% and waited {after['t'] - before['t']}")
            spans.add(after["t"] - before["t"])
    require(len(spans) > 5, f"timer skew: the drawn delays hardly vary: {sorted(spans)}")
    report(f"timer skew: a timer of 10 fires after 10, after 20 at 200%, after 5 on a clock at "
           f"half rate; over 50 seeds a drawn skew of 50-150% gives delays {min(spans)} to "
           f"{max(spans)}, each the recorded draw; a simulator with exact timers is told apart")


def settle(native, cut, start, world=sim.World):
    """Three nodes, one cut, the value 7 handed to node `start`; the values they end with."""
    prof = sim.profile(nodes=3, budget={"crash": 0, "drop": 0, "dup": 0, "partition": 1, "oneway": 1})
    world = world(native, Setters((7,)), prof)
    world.boot()
    require(sim.freeze(cut) in world.enabled(), f"{cut} is not enabled")
    world.apply(cut, sim.NoDraw())
    world.apply(("client", "set", start, 7), sim.NoDraw())
    for _ in range(200):
        action = world.earliest()
        if action is None:
            break
        world.apply(action, sim.NoDraw())
    return [native.schema.decode_state(state)[2] for state in world.state]


def oneway(native, report):
    """Node 0 hears nothing ("in") or is heard by nobody ("out"); the ring is 0 -> 1 -> 2 -> 0."""
    expected = {(("oneway", 0, "out"), 0): [7, 0, 0],      # node 0 cannot pass the value on
                (("oneway", 0, "out"), 1): [7, 7, 7],      # but it still hears it
                (("oneway", 0, "in"), 0): [7, 7, 7],       # node 0 can still speak
                (("oneway", 0, "in"), 1): [0, 7, 7],       # but never hears
                (("partition", 1), 0): [7, 0, 0],          # the symmetric cut does both
                (("partition", 1), 1): [0, 7, 7]}
    for (cut, start), values in expected.items():
        require(settle(native, cut, start) == values,
                f"{cut}, value handed to node {start}: {settle(native, cut, start)}")
    both = [settle(native, cut, start, mutants.OnewayCutsBoth) for cut, start in expected]
    require(both != list(expected.values()), "a simulator whose one-way cut is symmetric passes")
    prof = sim.profile(nodes=3, budget={"crash": 1, "drop": 1, "dup": 1, "partition": 1, "oneway": 2},
                       max_steps=300)
    workload, cuts = Setters((3, 5, 4)), 0
    for seed in range(50):
        trace = sim.run(native, workload, prof, seed)
        sim.replay(native, workload, trace)
        cuts += sum(step["kind"] == "oneway" for step in trace["steps"])
    require(cuts, "no seeded run made a one-way cut")
    report(f"one-way cuts: a node that only hears, and one that only speaks, end as expected in "
           f"{len(expected)} scenarios; 50 seeded runs with {cuts} one-way cuts replay; a "
           f"simulator whose one-way cut is symmetric is told apart")


def timer_budget(schema, native, report):
    """Two nodes, untimed, one value: how far the search goes with 0, 1 and unbounded timers."""
    sizes = {}
    for timers in (0, 1, None):
        budget = {"crash": 0, "drop": 0, "dup": 0, **({} if timers is None else {"timer": timers})}
        prof = sim.profile(nodes=2, timed=False, observe_states=True, budget=budget)
        mine, reached, facts = check.maude_agreement(schema, native, Setters((1,)),
                                                     maude_setters((1,), 2), prof)
        require(facts["maude_trap"] is None and mine == reached,
                f"timer budget {timers}: Maude and the simulator disagree")
        sizes[timers] = mine
    require(sizes[0] < sizes[1] < sizes[None], "the timer budget does not bound the search")
    one = sim.profile(nodes=2, timed=False, observe_states=True,
                      budget={"crash": 0, "drop": 0, "dup": 0, "timer": 1})
    mine, _, _ = check.maude_agreement(schema, native, Setters((1,)), maude_setters((1,), 2), one,
                                       world=mutants.IgnoresTimerBudget)
    require(mine != sizes[1], "a simulator that ignores the timer budget passes")
    edit = lambda text: text.replace("budget('timer, 1)", "budget('timer, 9)")
    _, reached, _ = check.maude_agreement(schema, native, Setters((1,)), maude_setters((1,), 2),
                                          one, edit=edit)
    require(reached != sizes[1], "a Maude model with a larger timer budget passes")
    report(f"timer budget: with 0, 1 and unbounded timer events Maude and the simulator reach "
           f"the same {len(sizes[0])}, {len(sizes[1])} and {len(sizes[None])} observations, each "
           f"set inside the next; a simulator and a Maude model that ignore the budget are told apart")


def flights(trace, share=None):
    """Every message handed to the network in a fault-free seeded run, as
    (destination, event, time sent, the slow draw or None, the delay drawn), and the
    Counter of (node, event, time) over the deliveries. `share`: whether the simulator
    that made the trace draws the slow choice (by default, what the profile says)."""
    share = trace["profile"].get("slow", {}).get("net", 0) if share is None else share
    sent, delivered = [], collections.Counter()
    for step in trace["steps"]:
        draws = list(step["draws"])
        take = lambda: (draws.pop(0) if share else None, draws.pop(0))
        if step["kind"] == "client":
            sent.append((step["node"], tuple(step["event"]), step["t"], *take()))
        elif step["kind"] in ("deliver", "local"):
            if step["kind"] == "deliver":
                delivered[(step["node"], tuple(step["event"]), step["t"])] += 1
            for effect in step["effects"]:
                if effect[0] == "tell":
                    sent.append((effect[1], ("heard", step["node"], effect[2]), step["t"], *take()))
    return sent, delivered


def slow(native, report):
    """Late messages on the gossip ring and a stalled disk on the probe node."""
    quiet = {"crash": 0, "drop": 0, "dup": 0}
    late = sim.profile(nodes=3, budget=quiet, max_steps=120, slow={"net": 30},
                       delay={"net-slow": [200, 400]})
    workload, kinds, messages = Setters((3, 5, 4)), collections.Counter(), 0
    for seed in range(50):
        trace = sim.run(native, workload, late, seed)
        sim.replay(native, workload, trace)
        sent, delivered = flights(trace)
        due = collections.Counter()
        for node, event, at, choice, delay in sent:
            is_slow = choice < 30
            require(200 <= delay <= 400 if is_slow else 1 <= delay <= 20,
                    f"slow messages: seed {seed} drew {choice} of 100 and then a delay of {delay}")
            kinds[is_slow] += 1
            due[(node, event, at + delay)] += 1
        require(delivered and not delivered - due,
                f"slow messages: seed {seed} delivers a message at a time no draw gave it")
        messages += sum(delivered.values())
        plain, _ = flights(sim.run(native, workload, late, seed, world=mutants.NeverSlow), 0)
        require(all(delay <= 20 for *_, delay in plain), "the never-slow simulator was slow")
    share = kinds[True] / (kinds[True] + kinds[False])
    require(0.2 < share < 0.4, f"slow messages: {share:.0%} of messages were slow, 30% asked")
    always = sim.profile(nodes=3, budget=quiet, max_steps=60, slow={"net": 100},
                         delay={"net-slow": [300, 300]})
    sent, _ = flights(sim.run(native, workload, always, 0))
    require(sent and all(delay == 300 for *_, delay in sent), "a share of 100% is not every message")
    report(f"slow messages: with a share of 30% and delays of 200 to 400, over 50 seeds "
           f"{kinds[True]} of {kinds[True] + kinds[False]} messages were slow ({share:.0%}), each "
           f"delay the recorded draw and each of {messages} deliveries at the time its draw "
           f"gave; with 100% every message takes the slow delay; a simulator that is never slow "
           f"is told apart")


def stalled_disk(report):
    """One probe node: a request issues two writes; with a stalled disk both syncs wait."""
    from .sim_check import Goes, HERE as PROBE
    schema = Schema(PROBE / "node.json")
    quiet = {"crash": 0, "drop": 0, "dup": 0}
    stalled = sim.profile(budget=quiet, max_steps=40, slow={"disk": 100},
                          delay={"disk-slow": [50, 50], "net": [1, 1]})
    with TemporaryDirectory() as tmp:
        native = build(schema, Path(tmp) / "probe", executable=False)
        waits = {}
        for label, world in (("stalled", sim.World), ("ignored", mutants.NeverSlow)):
            trace = sim.run(native, Goes((5,)), stalled, 0, world=world)
            if world is sim.World:
                sim.replay(native, Goes((5,)), trace)
            issued = next(s["t"] for s in trace["steps"] if s.get("event", [None])[0] == "go"
                          and s["kind"] == "deliver")
            waits[label] = [s["t"] - issued for s in trace["steps"] if s["kind"] == "sync"]
    require(waits["stalled"] == [50, 50], f"stalled disk: the writes synced after {waits['stalled']}")
    require(waits["ignored"] and max(waits["ignored"]) <= 5,
            f"the never-slow simulator stalled: {waits['ignored']}")
    report("stalled disk: with a share of 100% and a delay of 50 the two writes of a step sync "
           "50 after they were issued, in order; a simulator that is never slow syncs them "
           f"after {waits['ignored']}")


def main(report=print):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        native = build(schema, Path(tmp) / "gossip", executable=False)
        skew(native, report)
        oneway(native, report)
        timer_budget(schema, native, report)
        slow(native, report)
    stalled_disk(report)
