"""A recorded run of a driver as a trace of the simulator, or the reason it is not one.

`reconstruct(native, record)` reads the record tool/solo5/run.py made of a driver and
scripts tool.sim with the same sequence of events: the events the driver handled, in the
order it handled them, and a crash where the process was killed. The simulator computes
everything else with its own copy of the compiled step and its own model of the disk
and the network. The record is accepted only if

* every event the driver handled is one the simulator has queued at that point (so a
  read result is what the simulated disk holds, an acknowledgement belongs to a write
  that was issued, a timer was armed and is due);
* the simulator's step gives the state and the effects the driver logged, and the
  driver performed as many of them as the simulated node did before it died;
* the frames the client received are exactly the outputs performed, in order;
* the rows in the block image after every kill are the simulated disk's rows;
* for a node with a clock, the simulator's clock, moved only by its own timers and by
  the ticks the driver logged, agrees with every logged tick.

The first three are read from the driver's console, the fourth from the image file and
the frames from the client's socket, so a driver that misreports is caught by the other
two. The result is an ordinary `sim-trace/2` scripted trace: it replays, and the Shen
invariants of the node can be evaluated on it.

How a real run is mapped onto simulator actions:

    request handled        client (inject), deliver
    completion handled     local
    block write returned   sync, straight after the step (the acknowledgement is queued)
    packet received        output
    killed mid-step        the step with crash-after = effects performed, every write
                           issued before the cut persisted
    killed while idle      crash
    next boot              restart
"""
import json

from .. import sim
from ..check import CheckFailure
from ..schema import SchemaError


class Mismatch(CheckFailure):
    """The run is not a run of the simulator."""


def need(condition, message):
    if not condition:
        raise Mismatch(message)


class Feed(sim.Workload):
    """The outside world as a fixed list of requests, handed over one at a time."""

    def __init__(self, requests):
        self.requests = [sim.freeze(r) for r in requests]

    def initial(self, profile):
        return (0,)

    def actions(self, world):
        sent = world.client[0]
        return [("feed", sent)] if sent < len(self.requests) else []

    def apply(self, world, action, draw):
        event = self.requests[action[1]]
        world.request(0, event, draw)
        world.client = (action[1] + 1,)
        return {"node": 0, "event": event}


class PlanDraw(sim.Draw):
    """No delays; the node dies after `cut` effects of this step, if a cut is given; a
    crash keeps every pending write, because the driver's writes had all returned."""

    def __init__(self, cut=None):
        super().__init__()
        self.cut = cut

    def choose(self, label, n):
        value = {"crash-after": 0 if self.cut is None else self.cut + 1, "persist": n - 1}.get(label, 0)
        self.used.append(value)
        return value

    def delay(self, kind):
        self.used.append(0)
        return 0


def clocked(times, origin):
    """The simulator with its clock fed from a recorded run.

    `times` has one entry per scripted action: the tick the driver logged for it, or
    None. An action happens no earlier than its tick. The simulator still owns the clock:
    its own timers move it too, and the comparison requires both to agree.
    """
    class Clocked(sim.World):
        def __init__(self, *args):
            super().__init__(*args)
            self.clock, self.position = origin, 0

        def apply(self, action, draw):
            when = times[self.position] if self.position < len(times) else None
            self.position += 1
            if when is not None:
                self.clock = max(self.clock, when)
            return super().apply(action, draw)

    return Clocked


def parse(schema, boot):
    """The console lines of one boot as steps."""
    out = {"init": None, "steps": [], "hold": None, "skips": [], "fail": None}
    steps = out["steps"]
    try:
        for line in boot["lines"]:
            head, *words = line.split()
            if head == "@boot":
                need(words[0] == schema.name, f"the image is {words[0]}, not {schema.name}")
            elif head == "@init":
                out["init"] = tuple(map(int, words))
            elif head == "@event":
                number, tick, code, *fields = map(int, words)
                kind = schema.events.get(schema.symbol.get(code))
                need(kind is not None, f"event code {code} is not a declared event")
                rest = iter(fields)
                event = (kind.tag, *schema.decode(rest, kind.fields))
                need(next(rest, None) is None and number == len(steps) + 1, f"malformed {line}")
                steps.append({"n": number, "t": tick, "event": event, "role": kind.role,
                              "post": None, "effects": (), "applied": 0, "trap": None})
            elif head == "@step":
                state, effects = schema.decode_output(list(map(int, words)))
                steps[-1].update(post=schema.decode_state(state), effects=effects)
            elif head == "@trap":
                steps[-1]["trap"] = int(words[0])
            elif head == "@did":
                need(int(words[0]) == steps[-1]["applied"], f"effects performed out of order: {line}")
                steps[-1]["applied"] += 1
            elif head == "@hold":
                out["hold"] = (int(words[0]), int(words[1]))
            elif head == "@skip":
                out["skips"].append(words[0])
            elif head == "@fail":
                out["fail"] = words[0]
            else:
                need(head == "@idle", f"unknown console line {line}")
    except (SchemaError, StopIteration, ValueError, IndexError) as error:
        raise Mismatch(f"the console does not decode: {error}") from None
    return out


def role(schema, effect):
    return schema.effects[effect[0]].role


def image_rows(schema, rows):
    """Rows read from a block image, with their values decoded like a write's fields."""
    kind = next((k for k in schema.effects.values() if k.role == "disk-write"), None)
    return tuple((row[0], *schema.decode(iter(row[1:]), kind.fields[2:])) for row in rows)


def reconstruct(native, record, *, stops=False):
    """The simulator trace of a recorded run. Raises Mismatch when there is none.

    `stops` allows the run to end with the node stopping itself because a step had no
    result; the simulated run must then end in the same error.
    """
    schema = native.schema
    need(record["schema"] == schema.name, "the record is of another node")
    for number, raw in enumerate(record["boots"]):
        need("error" not in raw, f"boot {number} could not be played: {raw.get('error')}")
    boots = [parse(schema, raw) for raw in record["boots"]]
    timed = bool({"timer", "clock"} & set(schema.roles))
    requests = [step["event"] for boot in boots for step in boot["steps"] if step["role"] == "request"]
    prof = sim.profile(timed=True, budget={"crash": len(boots)}, max_steps=10 ** 6)
    times = []
    origin = boots[0]["steps"][0]["t"] if timed and boots[0]["steps"] else 0
    World = clocked(times, origin)
    workload = Feed(requests)
    world, trace = sim.start(native, workload, prof, None, World)
    actions, fed = [], 0
    restart = (schema.roles["restart"].tag,) if "restart" in schema.roles else None

    def act(where, action, when=None, cut=None):
        need(trace["end"] is None, f"{where}: the simulated run has ended ({trace['end']})")
        action = sim.freeze(action)
        need(action in world.enabled(), f"{where}: the simulator cannot do {action} here")
        times.append(when)
        draw = PlanDraw(cut)
        sim.attempt(world, trace, action, draw)
        actions.append((action, list(draw.used)))
        return trace["steps"][-1]

    def same(where, step, rec):
        if step["trap"] is not None:
            need(rec["kind"] == "error", f"{where}: the driver's step had no result (code "
                                         f"{step['trap']}); the simulator's had one")
            return
        need(rec["kind"] != "error", f"{where}: the simulator's step has no result: {rec.get('error')}")
        for name in ("event", "post", "effects", "applied"):
            need(sim.freeze(rec[name]) == sim.freeze(step[name]),
                 f"{where}: {name} differs\n  driver    {step[name]}\n  simulator {rec[name]}")
        need(not timed or world.clock == step["t"],
             f"{where}: the driver handled this at tick {step['t']}, the simulator's clock "
             f"is at {world.clock}")

    for number, (boot, raw) in enumerate(zip(boots, record["boots"])):
        last, steps = number == len(boots) - 1, boot["steps"]
        need(boot["fail"] is None, f"boot {number}: the driver stopped: {boot['fail']}")
        need(boot["init"] == tuple(native.init(0, 1)), f"boot {number}: the initial state differs")
        need(restart is None or (steps and steps[0]["event"] == restart),
             f"boot {number}: the node did not handle restart first")
        handled = [step["event"] for step in steps if step["role"] == "request"]
        need(sim.freeze(handled) == sim.freeze(raw["sent"]),
             f"boot {number}: requests handled {handled} are not the requests sent {raw['sent']}")
        performed = [e for step in steps for e in step["effects"][:step["applied"]]
                     if role(schema, e) == "output"]
        need(sim.freeze(performed) == sim.freeze(raw["received"]),
             f"boot {number}: the client received {raw['received']}, the driver reports the "
             f"outputs {performed}")
        hold = boot["hold"]
        need((hold is None) == (raw["hold"] is None) and (hold is None or (
            hold[0] == raw["hold"][0] == steps[-1]["n"] and hold[1] == steps[-1]["applied"])),
            f"boot {number}: asked to hold at {raw['hold']}, the driver reports {hold}")
        dead = False
        for index, step in enumerate(steps):
            where = f"boot {number} event {step['n']} {step['event']}"
            when = step["t"] if timed else None
            cut = None
            if hold and step is steps[-1] and step["applied"] < len(step["effects"]):
                cut = step["applied"]
            if index == 0 and restart is not None:
                need(number or cut is None, f"{where}: a kill inside the first restart step "
                                            "cannot be scripted in the simulator")
                rec = act(where, ("restart", 0), when, cut) if number else trace["steps"][0]
            elif step["role"] == "request":
                act(where, ("client", "feed", fed), when)
                fed += 1
                rec = act(where, ("deliver", 0, step["event"]), when, cut)
            else:
                rec = act(where, ("local", 0, step["event"]), when, cut)
            same(where, step, rec)
            if step["trap"] is not None:
                need(stops and last and step is steps[-1],
                     f"{where}: the node stopped itself (code {step['trap']})")
                break
            done = step["effects"][:step["applied"]]
            dead = cut is not None
            for effect in done:
                if role(schema, effect) == "disk-write" and not dead:
                    act(where, ("sync", 0))
            for effect in done:
                if role(schema, effect) == "output":
                    act(where, ("output", effect))
        if not last and not dead:
            act(f"boot {number}: the kill", ("crash", 0))
        if any(k.role == "disk-write" for k in schema.effects.values()):
            need(sim.freeze(world.rows(0)) == image_rows(schema, raw["rows"]),
                 f"boot {number}: after the kill the block image holds {raw['rows']}, the "
                 f"simulated disk {list(world.rows(0))}")
    if trace["end"] is None:
        trace["end"] = "quiescent" if sim.idle(world) else "open"
    trace = json.loads(sim.dumps(trace))
    # The trace is a function of the actions: scripting them again gives the same bytes.
    try:
        again = sim.script(native, workload, prof, actions, world=World)
        need(sim.dumps(again) == sim.dumps(trace), "the reconstructed trace is not reproducible")
        sim.replay(native, workload, trace, world=World)
    except sim.Divergence as error:
        raise Mismatch(f"the reconstructed trace does not replay: {error}") from None
    return trace, {"boots": boots, "requests": requests, "workload": workload, "world": World}
