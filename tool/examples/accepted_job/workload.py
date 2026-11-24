"""The outside world of AcceptedJob: clients that submit and retry.

A client action submits one request for the single idempotency key with one of
two payload digests. A retry is another submission. Whether a reply arrives
does not change what the client may do next, so every interleaving of
submissions, losses, crashes and retries is a schedule.
"""
from ...sim import Workload

KEY, DIGESTS = 7, (11, 22)


class Clients(Workload):
    def __init__(self, submissions):
        self.submissions = submissions

    def initial(self, profile):
        return (0, self.submissions, ())            # next request id, remaining, digests so far

    def actions(self, world):
        return [("submit", digest) for digest in DIGESTS] if world.client[1] else []

    def apply(self, world, action, draw):
        request, remaining, history = world.client
        event = ("submit", request, KEY, action[1])
        world.request(0, event, draw)
        world.client = (request + 1, remaining - 1, (action[1], *history))
        return {"node": 0, "event": event}


def maude(submissions):
    """The same clients as rewrite rules for tool/maude.py."""
    rules = ["  op client : Nat Nat Val -> Thing [ctor] ."]
    for digest in DIGESTS:
        rules.append(
            f"  rl [submit-{digest}] : {{ client(N, s(M:Nat), L) R }} => "
            f"{{ client(s(N), M:Nat, cons(i({digest}), L)) "
            f"ev(0, cons(q('submit), cons(i(N), cons(i({KEY}), cons(i({digest}), nil))))) R }} .")
    return {"rules": "\n".join(rules), "init": f"client(0, {submissions}, nil)"}
