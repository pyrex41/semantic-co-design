"""The outside world of AcceptedJob: clients that submit and retry.

A client action submits one request for one of the idempotency keys with one of
two payload digests. A retry is another submission. Whether a reply arrives
does not change what the client may do next, so every interleaving of
submissions, losses, crashes and retries is a schedule.

Three keys against a table of two receipts: two keys are needed before the
key comparison, the second receipt or the receipt number in a reply can matter
at all, and the third is what `full` is for.
"""
from ...sim import Workload

KEYS, DIGESTS = (7, 8, 9), (11, 22)


class Clients(Workload):
    def __init__(self, submissions, keys=KEYS, digests=DIGESTS):
        self.submissions, self.keys, self.digests = submissions, tuple(keys), tuple(digests)

    def initial(self, profile):
        # next request id, submissions left, (key, digest) of every request, newest first
        return (0, self.submissions, ())

    def actions(self, world):
        if not world.client[1]:
            return []
        return [("submit", key, digest) for key in self.keys for digest in self.digests]

    def apply(self, world, action, draw):
        request, remaining, history = world.client
        _, key, digest = action
        event = ("submit", request, key, digest)
        world.request(0, event, draw)
        world.client = (request + 1, remaining - 1, ((key, digest), *history))
        return {"node": 0, "event": event}


def maude(submissions, keys=KEYS, digests=DIGESTS):
    """The same clients as rewrite rules for tool/maude.py."""
    rules = ["  op client : Nat Nat Val -> Thing [ctor] ."]
    for key in keys:
        for digest in digests:
            rules.append(
                f"  rl [submit-{key}-{digest}] : {{ client(N, s(M:Nat), L) R }} => "
                f"{{ client(s(N), M:Nat, cons(cons(i({key}), cons(i({digest}), nil)), L)) "
                f"ev(0, cons(q('submit), cons(i(N), cons(i({key}), cons(i({digest}), nil))))) R }} .")
    return {"rules": "\n".join(rules), "init": f"client(0, {submissions}, nil)"}
