"""Directed schedules for histories that the seeded simulator does not produce, or produces
too rarely to rely on.

Like the two in figure8.py, these are NOT found by a seed. Each is a program that picks, at
each point, the one enabled action it names, written by hand (most of them by the reviewers
of this target) to show the consequence of one mutant: what goes wrong for a client or for
a published property when the history that exposes the edit does occur. The result is an
ordinary scripted trace, which `sim.replay` verifies. On the specification every check
passes on every one of them.

    forgotten_vote        a durable, answered vote; a crash; a second vote in the same term
    unwritten_candidacy   a candidate elected before its own vote reached its disk, a crash
    stale_vote            a granted vote of term 1 reaches its candidate in term 2
    stale_leader          AppendEntries of a deposed leader reaches a follower a term later
    slow_disk             a follower's acknowledgement overtakes the leader's own disk
    lost_ack              a follower acknowledges an entry, crashes, and does not have it
    torn_append           a crash between the two writes of an append (leader or follower)
    torn_cut              a crash between the writes that replace a conflicting suffix
    stale_ack             an acknowledgement of term 1 reaches the same leader in term 3
"""
from tool import sim
from tool.check import require
from .figure8 import Director, PUT_A, PUT_B
from .workload import Once

PUT_C, PUT_D, PUT_E = ("put", 3, 1, 13), ("put", 4, 0, 14), ("put", 5, 1, 15)


class Stage(Director):
    """A Director for any number of servers and requests."""

    def __init__(self, native, nodes=3, requests=(PUT_A, PUT_B), crashes=3):
        self.native, self.workload = native, Once(requests)
        self.profile = sim.profile(nodes=nodes, timed=False, budget={"crash": crashes})
        self.world, self.trace = sim.start(native, self.workload, self.profile, None)
        self.actions = []
        for node in range(nodes):
            self.do("local", node, "loaded")

    def reload(self, node):
        """The node restarts and is handed every row it asks for."""
        self.do("restart", node)
        while self.maybe("local", node, "loaded"):
            pass

    def first(self, kind, *want):
        """Apply the first enabled action that `do` would accept, when several fit (the
        order is that of `World.enabled`). Returns whether there was one."""
        def fits(action):
            if action[0] != kind:
                return False
            fields = (action[1], *action[2]) if kind in ("deliver", "local") else action[1:]
            return fields[:len(want)] == want
        found = [action for action in self.world.enabled() if fits(action)]
        if found:
            draw = sim.ScriptDraw([], False)
            require(sim.attempt(self.world, self.trace, found[0], draw),
                    f"directed: the node failed at {kind} {want}")
            self.actions.append((found[0], list(draw.used)))
        return bool(found)

    def pump(self, leader, follower, term, rounds=30):
        """AppendEntries from the leader to the follower and the answers, until neither has
        one in flight: the leader repairs the follower's log."""
        for _ in range(rounds):
            moved = self.first("deliver", follower, "on-app-req", leader, term)
            if moved:
                self.flush(follower)
            moved = self.first("deliver", leader, "on-app-rsp", follower, term) or moved
            if not moved:
                return

    def finish(self):
        trace = sim.script(self.native, self.workload, self.profile, self.actions)
        sim.replay(self.native, self.workload, trace)
        return trace


def forgotten_vote(native):
    """Server 2 votes for server 0 in term 1, durably, and server 0 leads. Server 2 crashes
    and restarts. Server 1 asks in the same term. A server that restores its vote refuses."""
    d = Stage(native)
    d.stand(0, 1)
    d.vote(2, 0, 1)                             # durable, answered: server 0 leads term 1
    d.do("crash", 2)
    d.reload(2)                                 # row 0 says: term 1, voted for 0
    d.stand(1, 1)
    d.vote(2, 1, 1)
    return d.finish()


def unwritten_candidacy(native):
    """Server 0 times out twice. The disk acknowledges the first write (term 1) while the
    second (term 2, its vote for itself) is still pending. A candidate that asks now is
    elected in term 2 by server 1, crashes, and its disk says term 1: it then gives server 2
    its vote in term 2."""
    d = Stage(native)
    d.do("local", 0, "alarm", 0)                # term 1
    d.do("local", 0, "alarm", 0)                # term 2: two writes pending
    d.do("sync", 0)
    d.do("local", 0, "written", 1)              # only term 1 is durable
    if d.maybe("deliver", 1, "on-vote-req", 0, 2):   # sent only by a node that asks early
        d.flush(1)
        d.do("deliver", 0, "on-vote-rsp", 1, 2)
    d.do("crash", 0)                            # the term-2 write is lost
    d.reload(0)
    d.stand(2, 1)
    d.stand(2, 2)
    d.vote(0, 2, 2)                             # server 0 knows nothing of its own candidacy
    return d.finish()


def stale_vote(native):
    """Server 1 votes for server 0 in term 1; the answer is slow. Both server 0 and server 2
    go on to term 2, where server 1 votes for server 2. Then the term-1 answer arrives."""
    d = Stage(native)
    d.stand(0, 1)
    d.do("deliver", 1, "on-vote-req", 0, 1)
    d.flush(1)                                  # granted, durable, in flight
    d.stand(0, 2)
    d.stand(2, 1)
    d.stand(2, 2)
    d.vote(1, 2, 2)                             # server 2 leads term 2
    d.do("deliver", 0, "on-vote-rsp", 1, 1)
    return d.finish()


def stale_leader(native):
    """Server 0 leads term 1 and appends put 1, which reaches nobody. Server 2 leads term 2,
    commits put 2 at the same index with server 1 and answers the client. Then the term-1
    AppendEntries reaches server 1, and server 0 stands again."""
    d = Stage(native)
    d.stand(0, 1)
    d.vote(1, 0, 1)
    d.put(0)                                    # put 1 at index 1, on server 0 only
    d.stand(2, 1)
    d.stand(2, 2)
    d.vote(1, 2, 2)
    d.put(2)                                    # put 2 at index 1, term 2
    d.do("deliver", 1, "on-app-req", 2, 2, 0, 0, True)
    d.flush(1)
    d.do("deliver", 2, "on-app-rsp", 1, 2, True, 1)   # committed; the client is answered
    d.do("local", 2, "alarm", 1)
    d.do("deliver", 1, "on-app-req", 2, 2, 1, 2, False)   # server 1 applies put 2
    d.do("deliver", 1, "on-app-req", 0, 1, 0, 0, True)    # the term-1 entry, late
    d.flush(1)
    d.do("deliver", 0, "on-app-rsp", 1, 2)      # server 0 learns of term 2
    d.flush(0)
    d.stand(0, 3)
    d.vote(1, 0, 3)
    return d.finish()


def slow_disk(native):
    """Server 0 leads and appends put 1; its own two writes stay pending. Server 1 stores
    the entry and acknowledges. Server 0 crashes, the writes are lost, and server 2 is
    elected by server 0 with an empty log."""
    d = Stage(native)
    d.stand(0, 1)
    d.vote(1, 0, 1)
    d.do("client", "send", 0)
    d.do("deliver", 0, "put")                   # appended; not on server 0's disk
    d.do("deliver", 1, "on-app-req", 0, 1, 0, 0, True)
    d.flush(1)
    d.do("deliver", 0, "on-app-rsp", 1, 1, True, 1)
    d.do("crash", 0)
    d.reload(0)
    d.stand(2, 1)
    d.stand(2, 2)
    d.vote(0, 2, 2)
    return d.finish()


def lost_ack(native):
    """Server 0 leads and appends put 1, durably. Server 1 is handed the entry and crashes
    before either of its two writes reaches its disk. A follower that had already answered
    lets server 0 commit and answer the client on the strength of a copy that is gone;
    server 2 is then elected by server 1 with an empty log."""
    d = Stage(native)
    d.stand(0, 1)
    d.vote(1, 0, 1)
    d.put(0)
    d.do("deliver", 1, "on-app-req", 0, 1, 0, 0, True)
    d.maybe("deliver", 0, "on-app-rsp", 1, 1, True, 1)   # sent only by a node that answers early
    d.do("crash", 1)
    d.reload(1)
    d.stand(2, 1)
    d.stand(2, 2)
    d.vote(1, 2, 2)
    return d.finish()


def torn_append(native, leader=False):
    """Server 0 leads and appends put 1. One of the two writes of that append reaches the
    disk of the leader (or of follower 1) and the server crashes. It restarts."""
    d = Stage(native)
    d.stand(0, 1)
    d.vote(1, 0, 1)
    d.vote(2, 0, 1)
    if leader:
        d.do("client", "send", 0)
        d.do("deliver", 0, "put")
        node = 0
    else:
        d.put(0)
        d.do("deliver", 1, "on-app-req", 0, 1, 0, 0, True)
        node = 1
    d.do("sync", node)                          # the first write of the two
    d.do("crash", node)                         # the second never reaches the disk
    d.reload(node)
    return d.finish()


def torn_cut(native):
    """Five servers. Server 0 holds [a b e] of term 1, of which b and e reached server 1
    only. Server 2 leads term 2 with [a] and sends c for index 2. Server 0 must replace b
    and e by c: three writes, of which one reaches the disk before it crashes. Server 1 then
    leads term 3 with [a b e] and brings servers 0 and 3 up to date."""
    d = Stage(native, nodes=5, requests=(PUT_A, PUT_B, PUT_C, PUT_D, PUT_E))
    d.stand(0, 1)
    d.vote(1, 0, 1)
    d.vote(2, 0, 1)
    d.put(0)                                    # a at index 1, to everyone
    for node in (1, 2, 3, 4):
        d.do("deliver", node, "on-app-req", 0, 1, 0, 0, True)
        d.flush(node)
    d.do("deliver", 0, "on-app-rsp", 1, 1, True, 1)
    d.put(0)                                    # b at index 2, to server 1 only
    d.do("deliver", 1, "on-app-req", 0, 1, 1, 1, True)
    d.flush(1)
    d.do("deliver", 0, "on-app-rsp", 1, 1, True, 2)
    d.put(0)                                    # e at index 3, to server 1 only
    d.do("deliver", 1, "on-app-req", 0, 1, 2, 1, True)
    d.flush(1)
    d.stand(2, 2)
    d.vote(3, 2, 2)
    d.vote(4, 2, 2)                             # server 2 leads term 2 with [a]
    d.put(2)                                    # c at index 2, term 2
    d.do("deliver", 0, "on-app-req", 2, 2, 1, 1, True)
    d.do("sync", 0)                             # one of the three writes
    d.do("crash", 0)
    d.reload(0)
    d.stand(1, 2)
    d.stand(1, 3)
    d.vote(0, 1, 3)
    d.vote(3, 1, 3)                             # server 1 leads term 3 with [a b e]
    d.put(1)                                    # f at index 4, term 3
    for _ in range(6):
        d.pump(1, 0, 3)
        d.pump(1, 3, 3)
        d.do("local", 1, "alarm", 1)            # the heartbeat carries the commit index
    d.pump(1, 0, 3)
    d.pump(1, 3, 3)
    return d.finish()


def stale_ack(native):
    """Five servers. Server 0 leads term 1 with [a b e]; server 1 stores all three and its
    acknowledgement of index 3 is slow. Server 2 leads term 2 with [a], elected by 3 and 4,
    and replaces b and e by c on server 0. Server 0 leads again in term 3, elected by 3 and
    4, and appends d at index 3. Now the term-1 acknowledgement arrives, and server 3 stores
    d: with the stale acknowledgement that looks like three of five. Server 2 then leads
    term 4, elected by servers 1 and 4, which never had d."""
    d = Stage(native, nodes=5, requests=(PUT_A, PUT_B, PUT_C, PUT_D, PUT_E))
    d.stand(0, 1)
    d.vote(1, 0, 1)
    d.vote(2, 0, 1)
    d.put(0)                                    # a at index 1, to everyone
    for node in (1, 2, 3, 4):
        d.do("deliver", node, "on-app-req", 0, 1, 0, 0, True)
        d.flush(node)
    d.do("deliver", 0, "on-app-rsp", 1, 1, True, 1)
    d.put(0)                                    # b at index 2, to server 1 only
    d.do("deliver", 1, "on-app-req", 0, 1, 1, 1, True)
    d.flush(1)
    d.do("deliver", 0, "on-app-rsp", 1, 1, True, 2)
    d.put(0)                                    # e at index 3, to server 1 only
    d.do("deliver", 1, "on-app-req", 0, 1, 2, 1, True)
    d.flush(1)                                  # its answer (term 1, index 3) stays in flight
    d.stand(2, 2)
    d.vote(3, 2, 2)
    d.vote(4, 2, 2)                             # server 2 leads term 2 with [a]
    d.put(2)                                    # c at index 2, term 2
    d.do("deliver", 0, "on-app-req", 2, 2, 1, 1, True)
    d.flush(0)                                  # server 0 now holds [a c]
    d.stand(0, 3)
    d.vote(3, 0, 3)
    d.vote(4, 0, 3)                             # server 0 leads term 3 with [a c]
    d.put(0)                                    # d at index 3, term 3
    d.do("deliver", 0, "on-app-rsp", 1, 1, True, 3)   # the acknowledgement of term 1
    d.pump(0, 3, 3)                             # server 3 is repaired and stores d
    d.do("deliver", 2, "on-app-req", 0, 3, 2, 2, False)   # a heartbeat deposes server 2
    d.flush(2)
    d.stand(2, 4)
    d.vote(1, 2, 4)
    d.vote(4, 2, 4)                             # server 2 leads term 4 with [a c]
    return d.finish()
