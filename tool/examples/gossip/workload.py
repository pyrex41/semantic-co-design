"""Clients that hand a fixed sequence of values to any node."""
from ...sim import Workload


class Setters(Workload):
    def __init__(self, values):
        self.values = tuple(values)

    def initial(self, profile):
        return (0,)                                 # how many values have been handed out

    def actions(self, world):
        sent = world.client[0]
        if sent == len(self.values):
            return []
        return [("set", node, self.values[sent]) for node in range(world.n)]

    def apply(self, world, action, draw):
        _, node, value = action
        world.request(node, ("set", value), draw)
        world.client = (world.client[0] + 1,)
        return {"node": node, "event": ("set", value)}


def maude(values, nodes):
    rules = ["  op client : Nat -> Thing [ctor] ."]
    for sent, value in enumerate(values):
        for node in range(nodes):
            rules.append(f"  rl [set-{sent}-{node}] : {{ client({sent}) R }} => {{ client({sent + 1}) "
                         f"ev({node}, cons(q('set), cons(i({value}), nil))) R }} .")
    return {"rules": "\n".join(rules), "init": "client(0)"}
