"""python3 -m tool.host.conform [options] <node.json> <node 0 data> <node 1 data> ...

    --source FILE        judge against this Shen source, not the manifest's
    --wasm FILE          every boot record must name this module
    --received N=FILE    the outputs one client of node N received: a JSON list, or an
                         object holding the list as "got"

Check a run of real processes from what they left on disk. Per node that is
events.log (every event delivered, with the step's result) and disk.log (the
durable rows). Three questions, all answered from those files:

  refinement   each segment starts from init; at every record the Shen source,
               run in shen-jit on the state before and the event, gives the
               recorded state and effects (tool/check.py does the comparison).
  causality    every event that is not a request or a restart names the effect
               that caused it, and that effect exists, came first, and says the
               same thing; every durable row is a write the log holds.
  invariants   the logs and the durable files give one trace in the simulator's
               form, and the manifest's Shen invariants are evaluated on it.

Given what a client received, each of those outputs must be an output effect
in that node's log, and no output may have been received more often than the
log emitted it (a client holds one connection at a time, and an effect goes to
a connection once). The host makes a record durable before it acts on the
effects, so an output that reached a client and is in no record means a record
did not survive what the host called durable.

A process kill ends a segment: the next record is a boot record, the state is
init again, and whatever the dead process still held in memory (unacknowledged
writes, timers, reads in flight) may not cause an event afterwards. A torn
final record (tool/host/wal.py) is an event the node stepped on but whose
effects never left, in a process whose memory is gone: it is dropped and
counted. The end of a log is a kill. A node whose log holds no complete record
has no run to judge, and that is a failure, not an empty success.

The clock is the machine's monotonic clock, which starts again when the machine
does: readings may not go back within a segment, and across a boot record
nothing is required of them.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from .. import check, sim
from ..check import CheckFailure, require
from ..schema import Schema
from . import wal

LOCAL = {"disk-ack": "disk-write", "disk-read-result": "disk-read", "timer": "timer-set",
         "clock": "clock-read"}


def load(schema, node, nodes, directory, digests):
    """One node's records, checked for form, and its durable rows."""
    try:
        records, torn = wal.read(Path(directory) / "events.log")
        rows, torn_rows = wal.read(Path(directory) / "disk.log")
    except wal.Corrupt as error:
        raise CheckFailure(f"log: node {node}: {error}") from None
    require(records, f"log: node {node} has no log in {directory}")
    whole = lambda place: isinstance(place, list) and all(type(n) is int for n in place)
    for n, row in enumerate(rows):
        require(isinstance(row, list) and len(row) > 2 and whole(row[:2]), f"log: node {node} durable row {n} is malformed")
    for n, record in enumerate(records):
        where = f"log: node {node} record {n}"
        try:
            require(n or "boot" in record, f"log: node {node} does not begin with a boot")
            require(record["seq"] == n, f"{where} carries sequence number {record['seq']}")
            if "boot" in record:
                boot = record["boot"]
                require((boot["node"], boot["nodes"]) == (node, nodes), f"{where}: another node's boot")
                for name, digest in digests.items():
                    require(boot[name] == digest, f"{where} booted another {name}")
                require(whole(boot["init"]) and len(boot["init"]) == schema.state_width,
                        f"{where}: the initial state is malformed")
                schema.decode_state(boot["init"])
                continue
            require("trap" not in record, f"{where}: the step had no result for {record['event']}")
            require(record.get("cause") is None or whole(record["cause"]),
                    f"{where}: the cause is malformed")
            schema.encode_state(sim.freeze(record["post"]))
            schema.encode_input((), record["event"])
            require(len(record["effects"]) <= schema.max_effects, f"{where}: too many effects")
            for tag, *fields in record["effects"]:
                schema.encode(fields, schema.effects[tag].fields)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise CheckFailure(f"{where} is malformed: {error!r}") from None
    return {"records": records, "rows": rows, "torn": torn, "torn_rows": torn_rows}


def replay(schema, logs, inits, source_file):
    """(a) Each node's event sequence through the Shen source. Returns distinct pairs."""
    steps, seen = [], {}
    for node, log in enumerate(logs):
        for record in log["records"]:
            if "boot" in record:
                require(tuple(record["boot"]["init"]) == tuple(inits[node]),
                        f"refinement: node {node} booted from another initial state")
                state, first = schema.decode_state(inits[node]), "restart" in schema.roles
                continue
            require(not first or record["event"] == [schema.roles["restart"].tag],
                    f"refinement: node {node} record {record['seq']}: a segment must begin with restart")
            step = {"pre": state, "event": record["event"], "post": record["post"],
                    "effects": record["effects"]}
            # check.refinement compares one result per distinct (state, event).
            key, result = sim.freeze((state, step["event"])), sim.freeze((step["post"], step["effects"]))
            require(seen.setdefault(key, result) == result,
                    f"refinement: node {node} record {record['seq']}: the same state and event "
                    f"gave two results")
            steps.append(step)
            state, first = record["post"], False
    return check.refinement(schema, [{"steps": steps}], inits, source_file=source_file)


def explain(schema, node, logs):
    """(b) One node's records as simulator steps, each event traced to its cause.

    Returns items: a step without its world, plus what it changes (`state`, `rows`) and,
    for a received message, the (node, record) that must come first.
    """
    log, restart = logs[node], "restart" in schema.roles
    role_of = lambda effect: effect and schema.effects[effect[0]].role
    effects = [{(r["seq"], k): e for r in other["records"] for k, e in enumerate(r.get("effects", []))}
               for other in logs]
    durable = {(row[0], row[1]): row[2:] for row in log["rows"]}
    require([*durable] == sorted(durable) and len(durable) == len(log["rows"]),
            f"causality: node {node}: durable rows are out of order")
    for at, row in durable.items():
        require(role_of(effects[node].get(at)) == "disk-write" and effects[node][at][2:] == row,
                f"causality: node {node}: durable row {row} is not a write the log holds at {at}")

    items, used, armed, unsynced, issued, segment, clock = [], set(), {}, {}, [], None, 0

    def kill():
        # The disk serves in order and syncs each write before it starts the next, so what a
        # segment made durable is a prefix of what it issued.
        kept = [at in durable for at in issued]
        require(kept == sorted(kept, reverse=True),
                f"causality: node {node}: a write of the segment at record {segment} is not "
                f"durable and a later write of that segment is")
        issued.clear()
        kept = [unsynced[at] for at in sorted(unsynced) if at in durable]
        items.append({"kind": "crash", "node": node, "crashed": True, "state": None, "rows": kept,
                      "persisted": len(kept), "lost_writes": len(unsynced) - len(kept)})
        unsynced.clear()
        armed.clear()

    for record in log["records"]:
        seq, where = record["seq"], f"causality: node {node} record {record['seq']}"
        if "boot" in record:
            if segment is not None:
                kill()
            clock = 0                       # the machine may have started again too
            if not restart:
                items.append({"kind": "restart" if segment is not None else "boot", "node": node,
                              "state": schema.decode_state(record["boot"]["init"])})
            segment = seq
            continue
        event, cause = record["event"], record.get("cause")
        role = schema.events[event[0]].role
        once = (role, *cause) if isinstance(cause, list) else None
        require(once is None or once not in used, f"{where}: a second event from one cause")
        used.add(once)
        item = {"kind": "local", "node": node, "event": event, "effects": record["effects"],
                "applied": len(record["effects"]), "state": record["post"], "seq": seq}
        if role in ("request", "restart"):
            require(cause is None, f"{where}: a {role} has no cause")
            item["kind"] = "deliver" if role == "request" else "boot" if segment == 0 else "restart"
            if role == "request":
                items.append({"kind": "client", "node": node, "event": event})
        elif role == "recv":
            require(isinstance(cause, list) and len(cause) == 3 and cause[0] in range(len(logs)),
                    f"{where}: a message needs the send that caused it")
            src, at = cause[0], tuple(cause[1:])
            sent = effects[src].get(at)
            require(role_of(sent) == "send" and schema.effects[sent[0]].deliver == event[0]
                    and sent[1] == node and event[1:] == [src, *sent[2:]],
                    f"{where}: {event} is not what node {src} sent at {at}")
            item.update(kind="deliver", needs=(src, at[0]))
        else:
            require(isinstance(cause, list) and len(cause) == 2, f"{where}: {event} has no cause")
            at = tuple(cause)
            made = effects[node].get(at)
            require(segment <= at[0] < seq and role_of(made) == LOCAL[role],
                    f"{where}: {event} is not caused by a {LOCAL[role]} of this segment")
            if role == "disk-ack":
                require(made[1] == event[1] and at in durable,
                        f"{where}: acknowledges a write that is not durable")
                items.append({"kind": "sync", "node": node, "event": unsynced[at],
                              "rows": [unsynced.pop(at)]})
            elif role == "disk-read-result":
                # The disk serves in order, so a read sees every durable write issued before it.
                found = [row for place, row in durable.items() if place < at and row[0] == made[2]]
                vals = found[-1][1:] if found else list(schema.default(schema.events[event[0]].fields[3:]))
                require(event[1:] == [*made[1:], int(bool(found)), *vals],
                        f"{where}: {event} is not what the durable file held")
            elif role == "timer":
                require(armed.pop(event[1], None) == at, f"{where}: timer {event[1]} was not armed")
            elif role == "clock":
                require(made[1] == event[1] and event[2] >= clock, f"{where}: the clock went back")
                clock = event[2]
        for k, (tag, *fields) in enumerate(record["effects"]):
            if role_of([tag]) == "disk-write":
                unsynced[seq, k] = fields[1:]
                issued.append((seq, k))
            elif role_of([tag]) == "timer-set":
                armed[fields[0]] = (seq, k)
            elif role_of([tag]) == "timer-cancel":
                armed.pop(fields[0], None)
        items.append(item)
    if segment is not None:
        kill()
    return items


def interleave(items):
    """One order of all nodes' steps in which every message is sent before it is received."""
    count = len(items)
    at, done = [0] * count, [-1] * count
    up, states, disks, steps = [False] * count, [None] * count, [{} for _ in range(count)], []
    while any(at[n] < len(items[n]) for n in range(count)):
        ready = [n for n in range(count) if at[n] < len(items[n])
                 and ("needs" not in items[n][at[n]]
                      or done[items[n][at[n]]["needs"][0]] >= items[n][at[n]]["needs"][1])]
        require(ready, "causality: messages are received before they are sent")
        node = ready[0]
        item = dict(items[node][at[node]])
        at[node] += 1
        done[node] = item.pop("seq", done[node])
        item.pop("needs", None)
        if "state" in item:
            states[node] = item.pop("state")
            up[node] = states[node] is not None
        for key, *vals in item.pop("rows", []):
            disks[node][key] = vals
        item["world"] = {"up": list(up), "states": list(states),
                         "disks": [[[key, *vals] for key, vals in sorted(d.items())] for d in disks]}
        steps.append(item)
    return {"steps": steps}


def witnessed(schema, node, log, outputs):
    """Everything one client of this node received is an output effect its log holds,
    as often as the client received it."""
    logged = Counter(sim.freeze(e) for r in log["records"] for e in r.get("effects", [])
                     if schema.effects[e[0]].role == "output")
    try:
        got = Counter(sim.freeze(output) for output in outputs)
    except TypeError:
        raise CheckFailure(f"causality: what a client of node {node} received is malformed") from None
    for output, times in got.items():
        require(output in logged,
                f"causality: a client received {output}, which the log of node {node} does not hold")
        require(times <= logged[output],
                f"causality: a client received {output} {times} times and the log of node "
                f"{node} holds it {logged[output]} times")
    return len(outputs)


def conform(schema, directories, *, source_file=None, module=None, received=None):
    """Raise CheckFailure, or return the facts of the run, invariant violations included."""
    source_file = Path(source_file or schema.directory / schema.manifest["spec"])
    digests = {"wasm": hashlib.sha256(Path(module).read_bytes()).hexdigest()} if module else {}
    nodes = len(directories)
    logs = [load(schema, node, nodes, d, digests) for node, d in enumerate(directories)]
    inits = check.inits_of(schema, nodes, source_file.read_text())
    trace = interleave([explain(schema, node, logs) for node in range(nodes)])
    seen = sum(witnessed(schema, node, logs[node], outputs)
               for node, outputs in (received or {}).items())
    pairs = replay(schema, logs, inits, source_file)
    events = sum("event" in r for log in logs for r in log["records"])
    return {"nodes": nodes, "events": events, "pairs": pairs,
            "segments": sum("boot" in r for log in logs for r in log["records"]),
            "torn": sum(bool(log["torn"]) for log in logs),
            "torn_rows": sum(bool(log["torn_rows"]) for log in logs),
            "rows": sum(len(log["rows"]) for log in logs), "received": seen,
            "lost_writes": sum(s.get("lost_writes", 0) for s in trace["steps"]),
            "trace": trace,
            "violations": check.invariants(schema, [trace])[0] if schema.invariants else []}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--wasm")
    parser.add_argument("--received", action="append", default=[], metavar="N=FILE")
    parser.add_argument("manifest")
    parser.add_argument("data", nargs="+")
    args = parser.parse_args()
    received = {}
    for node, _, file in (item.partition("=") for item in args.received):
        outputs = json.loads(Path(file).read_text())
        received[int(node)] = outputs["got"] if isinstance(outputs, dict) else outputs
    try:
        facts = conform(Schema(args.manifest), args.data, source_file=args.source,
                        module=args.wasm, received=received)
    except CheckFailure as failure:
        sys.exit(str(failure))
    facts.pop("trace")
    print(json.dumps(facts))
    sys.exit(1 if facts["violations"] else 0)
