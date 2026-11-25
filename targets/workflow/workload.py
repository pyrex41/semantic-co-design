"""The outside world of a workflow: activity workers, and a client that may cancel and is
told the outcome.

The workers are part of the simulated environment. They see every delivery of a `run` or
`revert` request, a duplicate as well, and keep a ledger of what they really did:

    (commit, name, key, result)   the activity ran and its effect is committed
    (fault, name, key, attempt)   the activity ran and failed; nothing is committed
    (reverse, name, key)          a committed effect was undone
    (void, name, key)             an undo arrived for a key that never committed

What a worker does with a key: a `run` for a key that has committed answers with the
recorded result and does not run again; a `run` for a key that was undone is refused
(result 0); otherwise the activity really runs and the environment chooses its outcome.
A request with a key the definition does not declare has no idempotency protection: it
runs every time. A `revert` reverses a committed key, voids one that never committed,
and is otherwise repeated without effect. Every answer travels back over the network,
where it can be delayed, lost or duplicated like any other request.

The client keeps what it was told: the outcome of every `report` that reached it, each
outcome once, in the order they first arrived.

The invariants read the ledger and the reports; the engine never does.

What these workers give the engine, by construction, is the idempotency contract in full:
a declared key commits at most once, and a key that was undone never commits afterwards
(the undo leaves a tombstone, `void` or `reverse`, that refuses a late request). No engine
can make a declared key commit twice here. The two halves are switched off one at a time,
by `careless` and by `tombstones=False`, to show what the engine leans on.
"""
from tool.sim import Workload


def last(ledger, key):
    """The newest commit, reverse or void entry of a key (the ledger is newest first)."""
    for entry in ledger:
        if entry[0] != "fault" and entry[2] == key:
            return entry
    return None


class Workers(Workload):
    def __init__(self, keys, outcomes, cancels=1, *, careless=False, tombstones=True):
        """`outcomes[name]` lists the results an execution of that activity may have; 0 is
        a failure. A seeded run draws one uniformly, so repeating a result weights it.
        Careless workers ignore the idempotency key of a `run`: they run it every time.
        Workers without tombstones still never commit a key twice, but a `run` for a key
        whose undo found nothing committed is executed like a first request."""
        self.keys, self.outcomes, self.cancels = tuple(keys), dict(outcomes), cancels
        self.careless, self.tombstones = careless, tombstones

    def initial(self, profile):
        # ledger, cancels the client may still send, outcomes the client was told
        return ((), self.cancels, ())

    def actions(self, world):
        return [("cancel",)] if world.client[1] else []

    def apply(self, world, action, draw):
        ledger, left, told = world.client
        world.request(0, ("cancel",), draw)
        world.client = (ledger, left - 1, told)
        return {"node": 0, "event": ("cancel",)}

    def deliver(self, world, payload, draw):
        ledger, left, told = world.client
        tag = payload[0]
        if tag == "run":
            _, name, key, attempt = payload
            known = last(ledger, key) if key in self.keys and not self.careless else None
            if known is not None and known[0] == "void" and not self.tombstones:
                known = None
            if known is None:
                results = self.outcomes[name]
                result = results[draw.choose("outcome", len(results))]
                ledger = ((("commit", name, key, result) if result
                           else ("fault", name, key, attempt)), *ledger)
            else:
                result = known[3] if known[0] == "commit" else 0
            world.request(0, ("done", key, attempt, result), draw)
        elif tag == "revert":
            _, name, key, _ = payload
            known = last(ledger, key)
            if known is None:
                ledger = (("void", name, key), *ledger)
            elif known[0] == "commit":
                ledger = (("reverse", name, key), *ledger)
            world.request(0, ("reverted", key), draw)
        elif tag == "report":
            if payload[1] not in told:
                told = (*told, payload[1])
            world.client = (ledger, left, told)
            return {"told": told}
        else:
            return {}
        world.client = (ledger, left, told)
        return {"ledger": ledger}


def maude(keys, outcomes, cancels=1):
    """The same workers and client as rewrite rules for tool/maude.py."""
    declared = " or ".join(f"KK:Int == {key}" for key in keys) or "false"
    request = lambda tag, name: (f"cons(q('{tag}), cons({name}, cons(i(KK:Int), "
                                 f"cons(i(AA:Int), nil))))")
    entry = lambda kind, name, *rest: (f"cons(q('{kind}), cons({name}, cons(i(KK:Int), "
                                       + "".join(f"cons({item}, " for item in rest) + "nil"
                                       + ")" * len(rest) + ")))")
    done = lambda result: (f"ev(0, cons(q('done), cons(i(KK:Int), cons(i(AA:Int), "
                           f"cons(i({result}), nil)))))")
    reverted = "ev(0, cons(q('reverted), cons(i(KK:Int), nil)))"
    world = lambda payload, ledger: f"{{ out({payload}) client({ledger}, CC:Nat, TD:Val) R }}"
    after = lambda payload, ledger, event: (f"{{ got({payload}) client({ledger}, CC:Nat, TD:Val) "
                                            f"{event} R }}")
    undone = "cons(q(QQ:Qid), cons(NM2:Val, cons(i(KK:Int), nil))) := wf.last(KK:Int, LG:Val)"
    committed = ("cons(q('commit), cons(NM2:Val, cons(i(KK:Int), cons(i(RR:Int), nil)))) "
                 ":= wf.last(KK:Int, LG:Val)")
    rules = [
        "  op wf.last : Int Val -> Val .",
        "  eq wf.last(KK:Int, nil) = nil .",
        "  eq wf.last(KK:Int, cons(cons(q(QQ:Qid), cons(NM:Val, cons(i(KK2:Int), Vs)))"
        ", LG:Val))",
        "   = if KK:Int == KK2:Int and QQ:Qid =/= 'fault",
        "     then cons(q(QQ:Qid), cons(NM:Val, cons(i(KK2:Int), Vs))) else wf.last(KK:Int, LG:Val) fi .",
        "  op wf.fresh : Int Val -> Bool .",
        f"  eq wf.fresh(KK:Int, LG:Val) = not ({declared}) or wf.last(KK:Int, LG:Val) == nil .",
    ]
    for name, results in outcomes.items():
        asked = request("run", f"q('{name})")
        for result in dict.fromkeys(results):
            made = (entry("commit", f"q('{name})", f"i({result})") if result
                    else entry("fault", f"q('{name})", "i(AA:Int)"))
            rules.append(f"  crl [run-{name}-{result}] : {world(asked, 'LG:Val')}\n"
                         f"   => {after(asked, f'cons({made}, LG:Val)', done(result))}\n"
                         f"   if wf.fresh(KK:Int, LG:Val) .")
    asked = request("run", "NM:Val")
    rules += [
        f"  crl [run-again] : {world(asked, 'LG:Val')}\n"
        f"   => {after(asked, 'LG:Val', done('RR:Int'))}\n"
        f"   if not wf.fresh(KK:Int, LG:Val) /\\ {committed} .",
        f"  crl [run-refused] : {world(asked, 'LG:Val')}\n"
        f"   => {after(asked, 'LG:Val', done(0))}\n"
        f"   if not wf.fresh(KK:Int, LG:Val) /\\ {undone} ."]
    asked = request("revert", "NM:Val")
    rules += [
        f"  crl [revert-void] : {world(asked, 'LG:Val')}\n"
        f"   => {after(asked, 'cons(' + entry('void', 'NM:Val') + ', LG:Val)', reverted)}\n"
        f"   if wf.last(KK:Int, LG:Val) == nil .",
        f"  crl [revert-reverse] : {world(asked, 'LG:Val')}\n"
        f"   => {after(asked, 'cons(' + entry('reverse', 'NM:Val') + ', LG:Val)', reverted)}\n"
        f"   if {committed} .",
        f"  crl [revert-again] : {world(asked, 'LG:Val')}\n"
        f"   => {after(asked, 'LG:Val', reverted)}\n"
        f"   if {undone} .",
        "  op wf.told : Val Val -> Val .",
        "  eq wf.told(V, nil) = cons(V, nil) .",
        "  eq wf.told(V, cons(W, Vs)) = if V == W then cons(W, Vs) else cons(W, wf.told(V, Vs)) fi .",
        "  rl [report] : { out(cons(q('report), cons(V, nil))) client(LG:Val, CC:Nat, TD:Val) R }\n"
        "   => { got(cons(q('report), cons(V, nil))) client(LG:Val, CC:Nat, wf.told(V, TD:Val)) R } ."]
    client = ["  op client : Val Nat Val -> Thing [ctor] .",
              "  rl [cancel] : { client(LG:Val, s(CC:Nat), TD:Val) R }\n"
              "   => { client(LG:Val, CC:Nat, TD:Val) ev(0, cons(q('cancel), nil)) R } ."]
    return {"rules": "\n".join(client), "output": "\n".join(rules),
            "init": f"client(nil, {cancels}, nil)"}
