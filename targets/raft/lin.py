"""Linearizability of the client-visible put/get history, by search (Wing and Gong).

This checker knows nothing of Raft and shares no code with the node. It reads only what
a client can see in a trace: the step at which a request was first handed to the network
and the step at which an `ok` reply for it first reached the outside world.

    put(key, value) -> ok          get(key) -> value          every key starts at 0

A history is linearizable when its operations can be put in one order that (a) respects
real time: an operation that returned before another was invoked comes first, and (b) is
a legal sequential run of a key-value store. An operation with no reply is open: a put
may have taken effect at any point after its invocation, or never; a get with no reply
constrains nothing. Requests carry a number and a retry is the same request, so a put
takes effect at most once however often it was sent.
"""


def history(trace):
    """Operations of a trace as dicts: req, op, key, value, invoked, returned, result."""
    ops, problems = {}, []
    for step in trace["steps"]:
        event = step.get("event")
        if step["kind"] == "client" and event and event[0] in ("put", "get"):
            request = event[1]
            op = {"req": request, "op": event[0], "key": event[2],
                  "value": event[3] if event[0] == "put" else None,
                  "invoked": step["i"], "returned": None, "result": None}
            seen = ops.setdefault(request, op)
            if (seen["op"], seen["key"], seen["value"]) != (op["op"], op["key"], op["value"]):
                problems.append(f"request {request} was sent as two different operations")
        elif step["kind"] == "output" and event and event[0] == "reply" and event[2] == "ok":
            request = event[1]
            if request not in ops:
                problems.append(f"step {step['i']}: a reply to request {request}, never sent")
            elif ops[request]["returned"] is None:
                ops[request].update(returned=step["i"], result=event[3])
    return list(ops.values()), problems


def linearizable(ops, keys=2):
    """Search for a legal order. Returns (True, order) or (False, None)."""
    ops = [op for op in ops if not (op["op"] == "get" and op["returned"] is None)]
    closed = frozenset(n for n, op in enumerate(ops) if op["returned"] is not None)
    failed = set()

    def search(done, state, order):
        if closed <= done:
            return order
        if (done, state) in failed:
            return None
        # An operation may come next if no unfinished operation returned before it began.
        horizon = min((ops[n]["returned"] for n in closed - done), default=None)
        for n, op in enumerate(ops):
            if n in done or (horizon is not None and op["invoked"] > horizon):
                continue
            if op["op"] == "put":
                after = state[:op["key"]] + (op["value"],) + state[op["key"] + 1:]
            elif op["result"] == state[op["key"]]:
                after = state
            else:
                continue
            found = search(done | {n}, after, order + [op["req"]])
            if found is not None:
                return found
        failed.add((done, state))
        return None

    order = search(frozenset(), (0,) * keys, [])
    return (order is not None), order


def check(trace):
    """None when the trace's history is linearizable, else a description of the failure."""
    ops, problems = history(trace)
    if problems:
        return problems[0]
    good, _ = linearizable(ops)
    if good:
        return None
    return "not linearizable: " + "; ".join(
        f"{op['op']}#{op['req']}(k{op['key']}{'' if op['value'] is None else '=' + str(op['value'])})"
        f"@{op['invoked']}..{op['returned']}" + ("" if op["result"] is None or op["op"] == "put"
                                                 else f"->{op['result']}")
        for op in sorted(ops, key=lambda op: op["invoked"]))


def self_test():
    """Hand-written histories with known answers: the checker's own teeth."""
    def op(req, kind, key, value, invoked, returned, result=None):
        return {"req": req, "op": kind, "key": key, "value": value, "invoked": invoked,
                "returned": returned, "result": 0 if kind == "put" and returned else result}

    cases = {
        "sequential": ([op(1, "put", 0, 5, 0, 1), op(2, "get", 0, None, 2, 3, 5)], True),
        "read of initial value": ([op(1, "get", 0, None, 0, 1, 0)], True),
        "concurrent get may see either": ([op(1, "put", 0, 5, 0, 3), op(2, "get", 0, None, 1, 2, 0)],
                                          True),
        "open put seen by a get": ([op(1, "put", 0, 5, 0, None), op(2, "get", 0, None, 1, 2, 5)],
                                   True),
        "open put never applied": ([op(1, "put", 0, 5, 0, None), op(2, "get", 0, None, 1, 2, 0)],
                                   True),
        "other key is independent": ([op(1, "put", 1, 9, 0, 1), op(2, "get", 0, None, 2, 3, 0)],
                                     True),
        "stale read": ([op(1, "put", 0, 5, 0, 1), op(2, "put", 0, 6, 2, 3),
                        op(3, "get", 0, None, 4, 5, 5)], False),
        "lost update": ([op(1, "put", 0, 5, 0, 1), op(2, "get", 0, None, 2, 3, 0)], False),
        "value never written": ([op(1, "get", 0, None, 0, 1, 7)], False),
        "read before the write began": ([op(1, "get", 0, None, 0, 1, 5), op(2, "put", 0, 5, 2, 3)],
                                        False),
        "put applied twice": ([op(1, "put", 0, 5, 0, 1), op(2, "put", 0, 6, 2, 3),
                               op(3, "get", 0, None, 4, 5, 6), op(4, "get", 0, None, 6, 7, 5)],
                              False),
        "two reads go backwards": ([op(1, "put", 0, 5, 0, 9), op(2, "get", 0, None, 1, 2, 5),
                                    op(3, "get", 0, None, 3, 4, 0)], False),
    }
    wrong = [name for name, (ops, expected) in cases.items() if linearizable(ops)[0] != expected]
    return len(cases), wrong
