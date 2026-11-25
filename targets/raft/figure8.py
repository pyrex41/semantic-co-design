"""Two directed schedules: Figure 8 of the Raft paper, and a vote lost in a crash.

These are NOT schedules the seeded simulator found. Each is written by hand, as a program
that picks, at each point, the one enabled action it names, so that the retained checks
can be shown to reject a mutant when the history that exposes it does arise. The result
is an ordinary scripted trace: `sim.replay` verifies it.

Three servers, an untimed profile (any enabled action may be next), up to three crashes.

`schedule`, after Figure 8:

    term 1   server 0 leads, takes put a at index 1; it is durable on server 0 only.
             Server 0 crashes.
    term 2   server 2 leads (voted for by server 1), takes put b at index 1; it is
             durable on server 2 only. Server 2 crashes.
    term 3   server 0 restarts and leads again. It replicates a (term 1) to server 1.
             a is now on a majority, but it is not of term 3: it must not be committed.
             A node that commits by counting replicas commits it here and answers the
             client. Server 0 crashes.
    term 4   server 2 restarts and is elected by server 1, because its last entry is of
             term 2 and server 1's is of term 1. It overwrites a with b on server 1.

`lost_vote`: server 2 grants its vote to server 0 in term 1 and crashes before the vote
reaches its disk. It restarts with no memory of the vote and grants server 1 its vote in
the same term. A node that answers a vote request only when the vote is durable never
sent the first answer, so server 0 is not elected. A node that answers at once has
elected two leaders in term 1.
"""
from tool import sim
from tool.check import require
from .workload import Once

PUT_A, PUT_B = ("put", 1, 0, 11), ("put", 2, 0, 12)
PROFILE = sim.profile(nodes=3, timed=False, budget={"crash": 3})


class Director:
    def __init__(self, native):
        self.native, self.workload = native, Once((PUT_A, PUT_B))
        self.world, self.trace = sim.start(native, self.workload, PROFILE, None)
        self.actions = []

    def do(self, kind, *want):
        """Apply the one enabled action of `kind` whose fields begin with `want`; for a
        delivery, `want` is the node and then the first fields of the event."""
        def fits(action):
            if action[0] != kind:
                return False
            fields = (action[1], *action[2]) if kind in ("deliver", "local") else action[1:]
            return fields[:len(want)] == want
        found = [action for action in self.world.enabled() if fits(action)]
        require(len(found) == 1, f"figure 8: {len(found)} enabled actions match {kind} {want}")
        draw = sim.ScriptDraw([], False)
        require(sim.attempt(self.world, self.trace, found[0], draw),
                f"figure 8: the node failed at {kind} {want}")
        self.actions.append((found[0], list(draw.used)))

    def maybe(self, kind, *want):
        """`do`, if such an action is enabled. Returns whether it was."""
        before = len(self.actions)
        try:
            self.do(kind, *want)
        except AssertionError:
            pass
        return len(self.actions) > before

    def flush(self, node):
        """Every pending write of the node becomes durable and is acknowledged, in order."""
        while self.world.writes[node]:
            self.do("sync", node)
        while any(i[2] == node and i[3][0] == "written" for i in self.world.local):
            token = min(i[3][1] for i in self.world.local if i[2] == node and i[3][0] == "written")
            self.do("local", node, "written", token)

    def stand(self, node, term):
        """The node's election timer fires and its vote for itself becomes durable."""
        self.do("local", node, "alarm", 0)
        self.flush(node)

    def vote(self, voter, candidate, term):
        self.do("deliver", voter, "on-vote-req", candidate, term)
        self.flush(voter)
        self.do("deliver", candidate, "on-vote-rsp", voter, term)

    def repair(self, leader, follower, term):
        """The new leader's heartbeat fails on the follower, it backs up one entry, sends
        the entry, and hears that it is stored."""
        self.do("deliver", follower, "on-app-req", leader, term)
        self.do("deliver", leader, "on-app-rsp", follower, term)
        self.do("deliver", follower, "on-app-req", leader, term)
        self.flush(follower)
        self.do("deliver", leader, "on-app-rsp", follower, term)

    def recover(self, node):
        self.do("restart", node)
        self.do("local", node, "loaded", 0, 0)
        self.do("local", node, "loaded", 1, 1)

    def put(self, node):
        self.do("client", "send", node)
        self.do("deliver", node, "put")
        self.flush(node)


def schedule(native):
    """Run the schedule on `native`. Returns the scripted trace."""
    d = Director(native)
    for node in range(3):
        d.do("local", node, "loaded")
    d.stand(0, 1)                   # term 1: server 0, elected by both others
    d.vote(1, 0, 1)
    d.vote(2, 0, 1)
    d.put(0)                        # a, on server 0 only
    d.do("crash", 0)
    d.stand(2, 2)                   # term 2: server 2, elected by server 1
    d.vote(1, 2, 2)
    d.put(2)                        # b, on server 2 only
    d.do("crash", 2)
    d.recover(0)
    d.stand(0, 2)                   # a term in which server 1 has already voted
    d.stand(0, 3)                   # term 3: server 0 again
    d.vote(1, 0, 3)
    d.repair(0, 1, 3)               # a reaches server 1: stored on a majority, in term 3
    d.do("crash", 0)
    d.recover(2)
    d.stand(2, 3)
    d.stand(2, 4)                   # term 4: server 2, whose last entry is newer than a
    d.vote(1, 2, 4)
    d.repair(2, 1, 4)               # b replaces a on server 1
    trace = sim.script(native, d.workload, PROFILE, d.actions)
    sim.replay(native, d.workload, trace)
    return trace


def lost_vote(native):
    """Run the lost-vote schedule on `native`. Returns the scripted trace."""
    d = Director(native)
    for node in range(3):
        d.do("local", node, "loaded")
    d.stand(0, 1)                               # server 0 asks for votes in term 1
    d.do("deliver", 2, "on-vote-req", 0, 1)     # server 2 grants; the vote is not on disk yet
    d.do("crash", 2)                            # and now it never will be
    d.maybe("deliver", 0, "on-vote-rsp", 2, 1)  # only a node that answered early sent this
    d.do("restart", 2)
    d.do("local", 2, "loaded", 0, 0)
    d.stand(1, 1)                               # server 1 asks in the same term
    d.vote(2, 1, 1)                             # server 2 remembers nothing and grants again
    trace = sim.script(native, d.workload, PROFILE, d.actions)
    sim.replay(native, d.workload, trace)
    return trace
