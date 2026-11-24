"""Deterministic simulation of compiled nodes.

Single-threaded, no wall clock, no real I/O. The world owns a virtual clock, a
network, one disk per node and the schedule. Every nondeterministic decision is
either the choice of the next action or an integer drawn inside an action, and
both are recorded, so a run is a function of (profile, seed) and a trace can be
replayed without the seed.

    World.enabled()            every action that may happen next
    World.apply(action, draw)  perform one action; returns the trace record
    run(...)                   seeded, timed schedule
    replay(...)                re-execute a recorded trace and compare every record
    explore(...)               every schedule of an untimed profile, by state
"""
from collections import deque
import hashlib
import json
import random

from ..compile import Trap

FORMAT = "sim-trace/1"
STEP_KINDS = ("boot", "restart", "deliver", "local")


class SimError(Exception):
    """The specification failed under the environment: a trap or an invalid effect."""


class Divergence(Exception):
    """A replay did not reproduce the recorded trace."""


def freeze(value):
    """JSON arrays back to the nested tuples the simulator works with."""
    return tuple(freeze(v) for v in value) if isinstance(value, (list, tuple)) else value


def order(values):
    return sorted(values, key=lambda v: json.dumps(v))


DEFAULT_PROFILE = {
    "nodes": 1,
    "timed": True,                  # False: no delays, the clock stays at 0 (exploration)
    "observe_states": False,        # whether node states are part of an observation
    "budget": {"crash": 0, "drop": 0, "dup": 0, "partition": 0},
    "max_steps": 200,
    # Seeded schedules only.
    "fault_rate": 0.08,             # chance per step of spending a crash, drop or dup budget
    "crash_mid_rate": 0.05,         # chance that a step crashes between two of its effects
    "restart_rate": 0.3,            # chance per step that a down node restarts
    "client_rate": 0.2,             # chance per step that the workload acts
    "delay": {"net": [1, 20], "disk": [1, 5]},
}


def profile(**changes):
    out = json.loads(json.dumps(DEFAULT_PROFILE))
    for key, value in changes.items():
        if isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


class Workload:
    """The outside world's behavior. State must be plain data; subclass per application."""

    def initial(self, profile):
        return ()

    def actions(self, world):
        return []

    def apply(self, world, action, draw):
        return {}

    def observe(self, world):
        return ("client", *world.client)


class Draw:
    """Integers drawn inside an action. Subclasses decide where they come from."""

    def __init__(self):
        self.used = []

    def choose(self, label, n):
        raise NotImplementedError

    def delay(self, kind):
        raise NotImplementedError


class NoDraw(Draw):
    def choose(self, label, n):
        return 0

    def delay(self, kind):
        return 0


class SeededDraw(Draw):
    def __init__(self, rng, profile):
        super().__init__()
        self.rng, self.profile = rng, profile

    def choose(self, label, n):
        if label == "crash-after":      # 0 is "no crash"
            value = self.rng.randrange(1, n) if self.rng.random() < self.profile["crash_mid_rate"] else 0
        else:
            value = self.rng.randrange(n)
        self.used.append(value)
        return value

    def delay(self, kind):
        low, high = self.profile["delay"][kind]
        value = self.rng.randint(low, high)
        self.used.append(value)
        return value


class ScriptDraw(Draw):
    """Replays recorded integers; past the end of the script it answers 0 and notes the arity."""

    def __init__(self, script, timed):
        super().__init__()
        self.script, self.timed, self.arities = list(script), timed, []

    def next(self, n):
        value = self.script[len(self.used)] if len(self.used) < len(self.script) else 0
        if n is not None and not 0 <= value < n:
            raise Divergence(f"recorded draw {value} outside {n} choices")
        self.used.append(value)
        self.arities.append(n)
        return value

    def choose(self, label, n):
        return self.next(n)

    def delay(self, kind):
        return self.next(None) if self.timed else 0


class World:
    def __init__(self, native, workload, profile, inits):
        self.native, self.schema, self.workload, self.profile = native, native.schema, workload, profile
        self.n = profile["nodes"]
        self.inits = [tuple(state) for state in inits]
        self.clock, self.ticks = 0, 0
        self.up = [True] * self.n
        self.state = list(self.inits)
        self.disk = [{} for _ in range(self.n)]
        self.writes = [[] for _ in range(self.n)]   # pending, oldest first: (at, seq, token, key, vals)
        self.net, self.local, self.outs = [], [], []  # (at, seq, node, event) / (at, seq, payload)
        self.got, self.dropped, self.lostw = set(), set(), set()
        self.budget = dict(profile["budget"])
        self.blocked = set()
        self.client = workload.initial(profile)
        self.timed = profile["timed"]
        if not self.timed and self.budget.get("partition"):
            raise ValueError("partitions are part of timed schedules only")

    def clone(self):
        other = object.__new__(World)
        other.__dict__.update(self.__dict__)
        other.up, other.state = list(self.up), list(self.state)
        other.disk = [dict(d) for d in self.disk]
        other.writes = [list(w) for w in self.writes]
        other.net, other.local, other.outs = list(self.net), list(self.local), list(self.outs)
        other.got, other.dropped, other.lostw = set(self.got), set(self.dropped), set(self.lostw)
        other.budget, other.blocked = dict(self.budget), set(self.blocked)
        return other

    # ---- views

    def rows(self, node):
        return tuple((key, *vals) for key, vals in sorted(self.disk[node].items()))

    def snapshot(self):
        return {"up": list(self.up),
                "states": [None if s is None else self.schema.decode_state(s) for s in self.state],
                "disks": [self.rows(node) for node in range(self.n)]}

    def key(self):
        """Identity of a state for exploration: contents only, no times or sequence numbers."""
        return (tuple(self.up), tuple(self.state), tuple(self.rows(n) for n in range(self.n)),
                tuple(tuple(w[2:] for w in ws) for ws in self.writes),
                tuple(order(i[2:] for i in self.net)), tuple(order(i[2:] for i in self.local)),
                tuple(order(o[2] for o in self.outs)), frozenset(self.got),
                frozenset(self.dropped), frozenset(self.lostw),
                tuple(sorted(self.budget.items())), freeze(self.client))

    def observe(self):
        """What an outside observer can know: durable rows, history sets and the workload."""
        things = {("rows", node, self.rows(node)) for node in range(self.n)}
        things |= {("got", v) for v in self.got} | {("dropped", v) for v in self.dropped}
        things |= {("lostw", v) for v in self.lostw}
        if self.profile["observe_states"]:
            things |= {("state", node, "down" if s is None else self.schema.decode_state(s))
                       for node, s in enumerate(self.state)}
        things.add(freeze(self.workload.observe(self)))
        return frozenset(things)

    # ---- actions

    def enabled(self):
        net = order({i[2:] for i in self.net})
        outs = order({o[2] for o in self.outs})
        acts = [("deliver", node, event) for node, event in net]
        acts += [("local", node, event) for node, event in order({i[2:] for i in self.local})]
        acts += [("sync", node) for node in range(self.n) if self.writes[node]]
        acts += [("output", payload) for payload in outs]
        for fault in ("drop", "dup"):
            if self.budget[fault] > 0:
                acts += [(fault, "net", node, event) for node, event in net]
                acts += [(fault, "out", payload) for payload in outs]
        if self.budget["crash"] > 0:
            acts += [("crash", node) for node in range(self.n) if self.up[node]]
        acts += [("restart", node) for node in range(self.n) if not self.up[node]]
        if self.timed and self.n > 1:
            if self.blocked:
                acts.append(("heal",))
            elif self.budget.get("partition", 0) > 0:
                acts += [("partition", mask) for mask in range(1, 2 ** (self.n - 1))]
        acts += [("client", *action) for action in self.workload.actions(self)]
        return acts

    def earliest(self):
        """The timed action that is due first, or None."""
        best = None
        for kind, items in (("deliver", self.net), ("local", self.local)):
            for item in items:
                if best is None or item[:2] < best[0]:
                    best = item[:2], (kind, item[2], item[3])
        for item in self.outs:
            if best is None or item[:2] < best[0]:
                best = item[:2], ("output", item[2])
        for node in range(self.n):
            if self.writes[node] and (best is None or self.writes[node][0][:2] < best[0]):
                best = self.writes[node][0][:2], ("sync", node)
        return best and best[1]

    def stamp(self, delay):
        self.ticks += 1
        return self.clock + (delay if self.timed else 0), self.ticks

    def take(self, items, content):
        """Remove the oldest queued item with this content and advance the clock to it."""
        found = min((i for i in items if i[2:] == content), default=None)
        if found is None:
            raise Divergence(f"nothing queued matches {content}")
        items.remove(found)
        self.clock = max(self.clock, found[0])
        return found

    def request(self, node, event, draw):
        """The workload hands a request to the network."""
        self.net.append((*self.stamp(draw.delay("net")), node, freeze(event)))

    def apply(self, action, draw):
        kind = action[0]
        rec = {"kind": kind}
        if kind in ("deliver", "local"):
            _, node, event = action
            self.take(self.net if kind == "deliver" else self.local, (node, event))
            rec.update(node=node, event=event)
            if not self.up[node]:
                rec["kind"] = "lost"
            else:
                self.step(node, event, draw, rec)
        elif kind == "sync":
            node = action[1]
            at, _, token, key, vals = self.writes[node].pop(0)
            self.clock = max(self.clock, at)
            self.disk[node][key] = vals
            rec.update(node=node, event=(key, *vals))
            if "disk-ack" in self.schema.roles:
                self.local.append((*self.stamp(0), node, (self.schema.roles["disk-ack"].tag, token)))
        elif kind == "output":
            self.take(self.outs, (action[1],))
            self.got.add(action[1])
            rec.update(event=action[1])
        elif kind in ("drop", "dup"):
            self.budget[kind] -= 1
            items, content = (self.net, action[2:]) if action[1] == "net" else (self.outs, action[2:])
            item = self.take(items, content)
            rec.update(event=content[-1])
            if kind == "drop":
                self.dropped.add(content[-1])
            else:
                items.append(item)
                items.append((*self.stamp(draw.delay("net")), *content))
        elif kind == "crash":
            self.budget["crash"] -= 1
            rec.update(node=action[1])
            self.crash(action[1], draw, rec)
        elif kind == "restart":
            node = action[1]
            self.up[node], self.state[node] = True, self.inits[node]
            rec.update(node=node)
            if "restart" in self.schema.roles:
                rec["event"] = (self.schema.roles["restart"].tag,)
                self.step(node, rec["event"], draw, rec)
        elif kind == "partition":
            self.budget["partition"] -= 1
            side = {node for node in range(self.n) if action[1] >> node & 1}
            self.blocked = {(a, b) for a in range(self.n) for b in range(self.n)
                            if (a in side) != (b in side)}
        elif kind == "heal":
            self.blocked = set()
        elif kind == "client":
            rec.update(self.workload.apply(self, action[1:], draw))
        else:
            raise Divergence(f"unknown action {action}")
        return rec

    def step(self, node, event, draw, rec):
        state = self.state[node]
        try:
            after, effects = self.native.step(state, event)
        except Trap as trap:
            raise SimError(f"node {node}: {trap}") from None
        rec.update(pre=self.schema.decode_state(state), post=self.schema.decode_state(after),
                   effects=effects)
        cut = None
        if self.budget["crash"] > 0 and effects:
            # 0: all effects happen. k: the node dies after the first k-1 effects.
            k = draw.choose("crash-after", len(effects) + 1)
            cut = k - 1 if k else None
        for effect in effects if cut is None else effects[:cut]:
            self.effect(node, effect, draw)
        rec["applied"] = len(effects) if cut is None else cut
        if cut is None:
            self.state[node] = after
        else:
            self.budget["crash"] -= 1
            self.crash(node, draw, rec)

    def effect(self, node, effect, draw):
        schema, (tag, *fields) = self.schema, effect
        role = schema.effects[tag].role
        if role == "disk-write":
            token, key, *vals = fields
            self.writes[node].append((*self.stamp(draw.delay("disk")), token, key, tuple(vals)))
        elif role == "disk-read":
            token, key = fields
            result = schema.roles["disk-read-result"]
            row = self.disk[node].get(key)
            vals = schema.default(result.fields[3:]) if row is None else row
            self.local.append((*self.stamp(draw.delay("disk")), node,
                               (result.tag, token, key, int(row is not None), *vals)))
        elif role == "output":
            self.outs.append((*self.stamp(draw.delay("net")), effect))
        elif role == "send":
            dest, *payload = fields
            if not (type(dest) is int and 0 <= dest < self.n):
                raise SimError(f"node {node} sends to {dest}, which is not a node")
            if (node, dest) not in self.blocked:
                self.net.append((*self.stamp(draw.delay("net")), dest,
                                 (schema.effects[tag].deliver, node, *payload)))
        elif role in ("timer-set", "timer-cancel"):
            timer = schema.roles["timer"].tag
            self.local = [i for i in self.local if i[2:] != (node, (timer, fields[0]))]
            if role == "timer-set":
                self.local.append((*self.stamp(fields[1]), node, (timer, fields[0])))
        elif role == "clock-read":
            self.local.append((*self.stamp(0), node,
                               (schema.roles["clock"].tag, fields[0], self.clock)))

    def crash(self, node, draw, rec):
        """Memory is lost. A prefix of the pending writes reaches the disk; the rest never do."""
        pending = self.writes[node]
        kept = draw.choose("persist", len(pending) + 1) if pending else 0
        for _, _, _, key, vals in pending[:kept]:
            self.disk[node][key] = vals
        for _, _, _, key, vals in pending[kept:]:
            self.lostw.add((key, *vals))
        self.writes[node] = []
        self.local = [i for i in self.local if i[2] != node]
        self.up[node], self.state[node] = False, None
        rec.update(crashed=True, persisted=kept, lost_writes=len(pending) - kept)

    def boot(self):
        """Every node handles `restart` once, with no faults, before the schedule starts."""
        records = []
        if "restart" in self.schema.roles:
            for node in range(self.n):
                event = (self.schema.roles["restart"].tag,)
                rec = {"kind": "boot", "node": node, "event": event}
                saved, self.budget = self.budget, {**self.budget, "crash": 0}
                self.step(node, event, NoDraw(), rec)
                self.budget = saved
                records.append(rec)
        return records


# ------------------------------------------------------------------------------ traces

def record(world, index, action, draws, rec):
    return {"i": index, "t": world.clock, "action": action, "draws": draws, **rec,
            "world": world.snapshot()}


def dumps(trace):
    """The canonical bytes of a trace."""
    return json.dumps(trace, sort_keys=True, separators=(",", ":")) + "\n"


def digest(trace):
    return hashlib.sha256(dumps(trace).encode()).hexdigest()


def start(native, workload, prof, inits, seed):
    world = World(native, workload, prof, inits)
    steps = [record(world, n, ["boot"], [], rec) for n, rec in enumerate(world.boot())]
    trace = {"format": FORMAT, "schema": native.schema.name, "seed": seed, "profile": prof,
             "steps": steps, "end": None}
    return world, trace


def pick(world, actions, rng):
    """The seeded schedule: mostly whatever is due first, sometimes a fault or the workload."""
    prof = world.profile
    for kinds, rate in ((("crash", "drop", "dup", "partition", "heal"), prof["fault_rate"]),
                        (("restart",), prof["restart_rate"]), (("client",), prof["client_rate"])):
        some = [a for a in actions if a[0] in kinds]
        if some and rng.random() < rate:
            return rng.choice(some)
    due = world.earliest()
    if due is not None:
        return due
    for kind in ("client", "restart"):
        some = [a for a in actions if a[0] == kind]
        if some:
            return rng.choice(some)
    return None


def run(native, workload, prof, inits, seed):
    """One seeded schedule. The trace is a function of the arguments and nothing else."""
    rng = random.Random(seed)
    world, trace = start(native, workload, prof, inits, seed)
    try:
        while len(trace["steps"]) < prof["max_steps"]:
            action = pick(world, world.enabled(), rng)
            if action is None:
                trace["end"] = "quiescent"
                break
            draw = SeededDraw(rng, prof)
            rec = world.apply(action, draw)
            trace["steps"].append(record(world, len(trace["steps"]), action, draw.used, rec))
        else:
            trace["end"] = "max_steps"
    except SimError as error:
        trace["end"] = f"error: {error}"
    return json.loads(dumps(trace))


def script(native, workload, prof, inits, actions, *, seed=None):
    """Run a given list of (action, draws). Raises Divergence if one is not enabled."""
    world, trace = start(native, workload, prof, inits, seed)
    try:
        for action, draws in actions:
            action = freeze(action)
            if action not in world.enabled():
                raise Divergence(f"step {len(trace['steps'])}: {action} is not enabled")
            draw = ScriptDraw(draws, prof["timed"])
            rec = world.apply(action, draw)
            if len(draw.used) != len(draws):
                raise Divergence(f"step {len(trace['steps'])}: draws {draws} became {draw.used}")
            trace["steps"].append(record(world, len(trace["steps"]), action, draw.used, rec))
    except SimError as error:
        trace["end"] = f"error: {error}"
    return json.loads(dumps(trace))


def replay(native, workload, inits, trace):
    """Re-execute a trace from its recorded actions and draws; every record must reappear."""
    if trace.get("format") != FORMAT:
        raise Divergence("not a simulator trace")
    actions = [(s["action"], s["draws"]) for s in trace["steps"] if s["action"] != ["boot"]]
    again = script(native, workload, trace["profile"], inits, actions, seed=trace["seed"])
    if again["end"] is None:
        again["end"] = trace["end"]
    for old, new in zip(trace["steps"], again["steps"]):
        if old != new:
            raise Divergence(f"step {old['i']} differs on replay")
    if dumps(again) != dumps(trace):
        raise Divergence("the replayed trace differs from the recorded one")
    return again


# -------------------------------------------------------------------------- exploration

def successors(world):
    """Every (action, draws, next world, record) of one state, over all draws."""
    for action in world.enabled():
        prefixes = [[]]
        while prefixes:
            prefix = prefixes.pop()
            after, draw = world.clone(), ScriptDraw(prefix, world.timed)
            rec = after.apply(action, draw)
            yield action, draw.used, after, rec
            for position in range(len(prefix), len(draw.used)):
                prefixes += [draw.used[:position] + [alt]
                             for alt in range(1, draw.arities[position])]


def explore(native, workload, prof, inits, *, max_states=200000):
    """Breadth-first over every schedule of an untimed profile.

    Returns (observations, states, edges). SimError propagates: a reachable trap is a
    failure. Raises OverflowError past max_states so a bound is never silently partial.
    """
    if prof["timed"]:
        raise ValueError("exploration needs an untimed profile")
    world = World(native, workload, prof, inits)
    world.boot()
    seen, queue, observations, edges = {world.key()}, deque([world]), set(), 0
    while queue:
        world = queue.popleft()
        observations.add(world.observe())
        for _, _, after, _ in successors(world):
            edges += 1
            key = after.key()
            if key not in seen:
                if len(seen) >= max_states:
                    raise OverflowError(f"more than {max_states} states")
                seen.add(key)
                queue.append(after)
    return observations, len(seen), edges
