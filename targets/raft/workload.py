"""Clients of the key-value store.

A client holds a fixed list of operations, `("put", key, value)` or `("get", key)`.
Operation number i carries request number i + 1 on every send, so a retry is the same
request. At most `window` operations are open at once, and each may be sent `sends`
times, a retry no sooner than `patience` time units after the send before it (timed
profiles). An operation is complete when an `ok` reply for its request number has
reached the outside world; that is all the client learns.

Where to send: the client is not told who leads. With `aim="leaders"` it sends only to
servers that currently believe they lead, stale leaders included, which is what a client
following redirects converges to; with `aim="any"` it sends anywhere.

`calm_at` (timed profiles): once the clock passes it the client performs one action that
ends all faults: budgets go to zero and the network heals. From then on it sends one more
put, the `final` operation, to one server after another every `retry` time units until it
is answered. The liveness check measures how long that takes.
"""
from tool.sim import Workload

ROLE = 2            # position of Role in the state vector


class Clients(Workload):
    def __init__(self, ops, *, sends=2, window=2, patience=200, aim="leaders", calm_at=None,
                 final=None, retry=60):
        self.ops, self.sends, self.window, self.aim = tuple(ops), sends, window, aim
        self.patience, self.calm_at, self.final, self.retry = patience, calm_at, final, retry

    def initial(self, profile):
        # sends left per operation, the time faults stopped (-1: not yet), final puts sent,
        # the time each operation was last sent
        return (tuple(self.sends for _ in self.ops), -1, 0, tuple(-self.patience for _ in self.ops))

    def event(self, index):
        op = self.final if index == len(self.ops) else self.ops[index]
        return ("put", index + 1, op[1], op[2]) if op[0] == "put" else ("get", index + 1, op[1])

    def done(self, world):
        return {payload[1] for payload in world.got if payload[0] == "reply" and payload[2] == "ok"}

    def targets(self, world):
        if self.aim == "any":
            return list(range(world.n))
        leader = world.schema.code["leader"]
        return [node for node in range(world.n)
                if world.up[node] and world.state[node][ROLE] == leader]

    def actions(self, world):
        left, calmed, finals, sent = world.client
        done = self.done(world)
        out = []
        if self.calm_at is not None and calmed < 0:
            if world.clock >= self.calm_at:
                return [("calm",)]
        elif calmed >= 0:
            if (self.final is not None and len(self.ops) + 1 not in done
                    and world.clock >= calmed + finals * self.retry):
                out.append(("final", finals % world.n))
            return out
        open_ops = [n for n in range(len(self.ops)) if n + 1 not in done][:self.window]
        targets = self.targets(world)
        return [("send", n, node) for n in open_ops
                if left[n] and world.clock >= sent[n] + self.patience for node in targets]

    def apply(self, world, action, draw):
        left, calmed, finals, sent = world.client
        if action[0] == "calm":
            world.budget = {name: 0 for name in world.budget}
            world.blocked = set()
            world.client = (left, world.clock, finals, sent)
            return {}
        if action[0] == "final":
            node, index = action[1], len(self.ops)
            world.client = (left, calmed, finals + 1, sent)
        else:
            _, index, node = action
            world.client = (tuple(n - (i == index) for i, n in enumerate(left)), calmed, finals,
                            tuple(world.clock if i == index else t for i, t in enumerate(sent)))
        event = self.event(index)
        world.request(node, event, draw)
        return {"node": node, "event": event}

    def observe(self, world):
        return ("client", *world.client[0])


class Once(Workload):
    """For the bounded search: a fixed sequence of requests, each handed once to any server."""

    def __init__(self, requests):
        self.requests = tuple(requests)

    def initial(self, profile):
        return (0,)                                 # how many requests have been handed out

    def actions(self, world):
        sent = world.client[0]
        return [] if sent == len(self.requests) else [("send", node) for node in range(world.n)]

    def apply(self, world, action, draw):
        event = self.requests[world.client[0]]
        world.request(action[1], event, draw)
        world.client = (world.client[0] + 1,)
        return {"node": action[1], "event": event}


def maude_once(requests, nodes):
    """The same clients as rules of the Maude environment."""
    from tool.maude import term
    rules = ["  op client : Nat -> Thing [ctor] ."]
    for sent, event in enumerate(requests):
        for node in range(nodes):
            rules.append(f"  rl [send-{sent}-{node}] : {{ client({sent}) R }} => "
                         f"{{ client({sent + 1}) ev({node}, {term(event)}) R }} .")
    return {"rules": "\n".join(rules), "init": "client(0)"}
