"""A recorded run of a cluster as one trace of the simulator, or the reason it is not one.

The record (tool/solo5/cluster.py) has two kinds of evidence that do not come from the
same place: what each node printed on its console, and what the harness, which is the
only path between the nodes, read from and wrote to each link, in the order it did so.

`alone(native, record)` takes each node by itself: every step on its console is the
compiled step applied to the state the console showed before it.

`reconstruct(native, record)` puts the nodes together. It first holds the two kinds of
evidence against each other, without the simulator:

* the frames the harness read from a node's link are, in order, exactly the sends and
  outputs that node's console says it performed: a send to node D is a frame to D's
  address, from the node's own address, carrying the `deliver` event with the node as
  the sender;
* every request or message a node handled is a frame the harness had written to that
  node's link, addressed to that node, later than the frames matched before it;
* a node skipped no frame for a reason other than its address or its ethertype, and no
  more frames for their address than the harness put on its link for another node.

Then it scripts tool.sim with one order of everything that happened. The order respects
every "before" the record establishes (a node's console order, the harness's own order,
a frame is sent before the harness reads it and written before the node handles it, a
boot's steps lie between its boot and its kill) and is otherwise by the clock, which the
harness and the nodes share. The simulator computes each step with its own library, its
own disks and its own network, and the record is accepted when

* every event a node handled is queued for that node in the simulator at that point: a
  message was sent to it by the node it names and not dropped, a read result is what
  the simulated disk holds, a timer was armed;
* the simulator's step gives the state and effects on the console, and the node
  performed as many of them as the simulated node did before it died;
* the simulator's clock, moved by its own timers and by the ticks on the consoles,
  agrees with every tick, so no timer fired early;
* after every kill the block image holds exactly the simulated disk's rows.

How the run is mapped onto simulator actions:

    a node handles a request or a message      deliver
    a node handles a completion or a timer     local
    a block write returned                     sync, straight after the step
    the harness read a reply                   output
    the harness sent a request                 client
    the harness dropped a frame, or cut it     drop
    the harness forwarded a frame twice        dup
    SIGKILL                                    crash; inside a step, the step with
                                               crash-after = effects performed
    the next boot                              restart

A cut is a drop here and not the simulator's `partition`: the harness decides when a
frame arrives, the simulator when the step sends, and a frame in flight when a cut starts
or ends would be judged differently by the two. As drops the trace is a schedule the
simulator has whenever the frame arrived. A frame the harness forwarded and the node
never handled (it was down, or was killed first) stays undelivered in the simulator.

A node booted with `hold=` stops inside a step and is killed there; that is exact. Any
other kill lands where it lands. A node killed inside a step has performed the effects it logged (`@did`), and possibly
one more whose log line it did not live to write. That one is counted when the evidence
shows it: one more frame on its link, or the row in its block image.
"""
import heapq
import json

from .. import sim
from ..compile import Trap
from .cluster import address, message, node_of
from .compare import Mismatch, PlanDraw, clocked, image_rows, need, parse, role
from .run import BROADCAST, unframe

SLACK = 2.0


class Feed(sim.Workload):
    """The client as the list of requests the harness sent, each to the node it chose."""

    def __init__(self, requests):
        self.requests = [(node, sim.freeze(event)) for node, event in requests]

    def initial(self, profile):
        return (0,)

    def actions(self, world):
        sent = world.client[0]
        return [("feed", sent)] if sent < len(self.requests) else []

    def apply(self, world, action, draw):
        node, event = self.requests[action[1]]
        world.request(node, event, draw)
        world.client = (action[1] + 1,)
        return {"node": node, "event": event}


def alone(native, record):
    """Each node's console by itself against the compiled step. Returns the number of
    steps compared."""
    schema, count = native.schema, 0
    for node, boots in enumerate(record["boots"]):
        for number, raw in enumerate(boots):
            where, state, event = f"node {node} boot {number}", None, None
            for line in raw["lines"]:
                head, *words = line.split()
                try:
                    if head == "@init":
                        state = tuple(map(int, words))
                        need(state == tuple(native.init(node, record["nodes"])),
                             f"{where}: the initial state is not node_init's")
                    elif head == "@event":
                        code, *fields = map(int, words[2:])
                        kind = schema.events[schema.symbol[code]]
                        event = (kind.tag, *schema.decode(iter(fields), kind.fields))
                    elif head == "@step":
                        need(state is not None and event is not None, f"{where}: a step without "
                                                                      "a state or an event")
                        mine = native.raw(schema.encode_input(state, event))
                        need(mine is not None and list(mine) == list(map(int, words)),
                             f"{where}: the step on {event} is not the compiled step's")
                        state, event, count = tuple(mine[:schema.state_width]), None, count + 1
                except (KeyError, ValueError, Trap) as error:
                    raise Mismatch(f"{where}: the console does not decode: {error!r}") from None
    return count


def describe(schema, data):
    data = bytes.fromhex(data)
    parsed = message(schema, data)
    if parsed is not None:
        return f"{parsed[2]} to node {parsed[0]} from the address of node {parsed[1]}"
    if data[0:6] == BROADCAST and unframe(schema, data) is not None:
        return f"{unframe(schema, data)} to the client"
    return f"{len(data)} bytes that are no frame of this node"


def reconstruct(native, record):
    """The simulator trace of a recorded cluster run. Raises Mismatch when there is none."""
    schema, n, log = native.schema, record["nodes"], record["log"]
    need(record["schema"] == schema.name, "the record is of another node")
    need(len(record["boots"]) == n and all(record["boots"]), "a node was never booted")
    need([e["at"] for e in log] == sorted(e["at"] for e in log), "the harness's clock goes back")
    restart = schema.roles["restart"].tag

    # ---- the consoles
    boots = []
    for node, raws in enumerate(record["boots"]):
        boots.append([])
        for number, raw in enumerate(raws):
            where = f"node {node} boot {number}"
            need(len(raw["at"]) == len(raw["lines"]), f"{where}: console lines without a time")
            boot = parse(schema, raw)
            need(boot["boot"] == (node, n, 512 * raw["blocks"], 512),
                 f"{where}: booted as {boot['boot']}: not node {node} of {n} on its block file")
            need(boot["init"] is None or boot["init"] == tuple(native.init(node, n)),
                 f"{where}: the initial state differs")
            need(boot["fail"] is None and not any(step["trap"] is not None for step in boot["steps"]),
                 f"{where}: the node stopped itself ({boot['fail'] or 'a step without a result'})")
            need(raw["status"] == -9, f"{where}: exit status {raw['status']}; a killed node has -9")
            if boot["steps"] and boot["steps"][-1]["post"] is None:
                boot["steps"].pop()         # killed before the step returned: nothing happened
            need(not boot["steps"] or boot["steps"][0]["event"] == (restart,),
                 f"{where}: the node did not handle restart first")
            for step in boot["steps"]:
                need(raw["at"][step["line"]] >= step["t"] - SLACK,
                     f"{where}: event {step['n']} carries tick {step['t']}, and the harness read "
                     f"it at {raw['at'][step['line']]:.0f} ms of the same clock")
            bad = [why for why in boot["skips"] if why not in ("address", "ethertype")]
            need(not bad, f"{where}: frames skipped as {bad}")
            boots[-1].append(boot)

    # ---- the harness's log, per node and boot
    spans, ins = {}, {}
    for index, entry in enumerate(log):
        if entry["what"] in ("boot", "kill"):
            key = (entry["node"], entry["boot"])
            need(0 <= entry["node"] < n and entry["boot"] < len(boots[entry["node"]]),
                 f"the log names a boot that has no console: {key}")
            span = spans.setdefault(key, {})
            need(entry["what"] not in span and (entry["what"] == "boot") != ("boot" in span),
                 f"node {key[0]} boot {key[1]}: booted or killed twice, or killed before boot")
            span[entry["what"]] = index
        elif entry["what"] == "in":
            ins.setdefault((entry["node"], entry["boot"]), []).append(index)
    for node in range(n):
        for number in range(len(boots[node])):
            need(set(spans.get((node, number), ())) == {"boot", "kill"},
                 f"node {node} boot {number}: the log has no boot and kill for it")
            need(number == 0 or spans[node, number - 1]["kill"] < spans[node, number]["boot"],
                 f"node {node}: boot {number} started before boot {number - 1} was killed")

    # ---- frames on a node's link against the effects on its console
    emitted = {}                                 # (node, boot) -> [(step, effect)] in order
    for node in range(n):
        for number, (boot, raw) in enumerate(zip(boots[node], record["boots"][node])):
            where, steps = f"node {node} boot {number}", boot["steps"]
            seen = ins.get((node, number), [])
            wire = lambda step: [e for e in step["effects"][:step["applied"]]
                                 if role(schema, e) in ("send", "output")]
            if steps and steps[-1]["applied"] < len(steps[-1]["effects"]):
                last = steps[-1]
                following = last["effects"][last["applied"]]
                kind = role(schema, following)
                if kind in ("send", "output"):
                    if len(seen) == sum(len(wire(step)) for step in steps) + 1:
                        last["applied"], last["inferred"] = last["applied"] + 1, True
                elif kind == "disk-write":
                    _, _, key, *values = following
                    if (key, *values) in image_rows(schema, raw["rows"]):
                        last["applied"], last["inferred"] = last["applied"] + 1, True
            sent = [(index, effect) for index, step in enumerate(steps) for effect in wire(step)]
            need(len(seen) == len(sent),
                 f"{where}: the node put {len(seen)} frames on its link; its steps performed "
                 f"{len(sent)} sends and outputs")
            for position, (at, (_, effect)) in enumerate(zip(seen, sent)):
                data = bytes.fromhex(log[at]["frame"])
                if role(schema, effect) == "output":
                    good = data[0:6] == BROADCAST and unframe(schema, data) == list(effect)
                else:
                    tag, to, *payload = effect
                    good = (data[0:6] == address(to) and data[6:12] == address(node)
                            and (message(schema, data) or [None] * 3)[2]
                            == [schema.effects[tag].deliver, node, *payload])
                need(good, f"{where}: frame {position} on the node's link is "
                           f"{describe(schema, log[at]['frame'])}; the effect its step performed "
                           f"is {list(effect)}" + ("" if role(schema, effect) == "output" else
                                                   f", a message to node {effect[1]} from node {node}"))
            emitted[node, number] = sent

    # ---- what a node handled against what the harness wrote to its link
    fed = {}                                     # (node, boot, step index) -> log index
    for node in range(n):
        for number, boot in enumerate(boots[node]):
            where, span = f"node {node} boot {number}", spans[node, number]
            given = [index for index in range(span["boot"], span["kill"])
                     if log[index]["what"] in ("out", "request") and log[index]["node"] == node
                     and log[index].get("sent")]

            def content(index):
                entry = log[index]
                if entry["what"] == "request":
                    return sim.freeze(entry["event"]), True
                parsed = message(schema, bytes.fromhex(entry["frame"]))
                return sim.freeze(parsed[2]), parsed[0] == node and not entry["flood"]

            handled = [(position, step) for position, step in enumerate(boot["steps"])
                       if step["role"] in ("request", "recv")]
            floods = sum(not content(index)[1] for index in given)

            def embed(mine_only):
                """The handled events as a subsequence of the frames written to the link,
                each matched to the earliest frame after the one before it: {step: log
                index}, and the first step that has none."""
                at, found = 0, {}
                for position, step in handled:
                    while at < len(given) and not (content(given[at])[0] == step["event"]
                                                   and (content(given[at])[1] or not mine_only)):
                        at += 1
                    if at == len(given):
                        return found, step
                    found[position] = given[at]
                    at += 1
                return found, None

            found, missing = embed(True)
            if missing is not None:
                # Not among the frames for this node. Among all the frames on its link?
                anything, never = embed(False)
                stray = [(position, index) for position, index in anything.items()
                         if not content(index)[1]]
                need(never is not None or not stray,
                     f"{where}: the node handled {boot['steps'][stray[0][0]]['event']} (event "
                     f"{boot['steps'][stray[0][0]]['n']}), a frame addressed to node "
                     f"{message(schema, bytes.fromhex(log[stray[0][1]]['frame']))[0]}"
                     if stray else "")
                need(False, f"{where}: the node handled {missing['event']} (event {missing['n']}), "
                            "which the harness had not written to its link")
            for position, index in found.items():
                fed[node, number, position] = index
            skipped = boot["skips"].count("address")
            need(skipped <= floods, f"{where}: the node skipped {skipped} frames for their "
                                    f"address; the harness wrote {floods} such frames to its link")
    need("error" not in record, f"the run could not be played to its end: {record.get('error')}")

    # ---- one order of everything
    items, after, waits = [], {}, {}             # item -> its successors, its unmet predecessors

    def item(key, priority):
        items.append(key)
        after[key], waits[key] = [], 0
        return priority

    def edge(a, b):
        after[a].append(b)
        waits[b] += 1

    priority = {}
    for index, entry in enumerate(log):
        priority["h", index] = item(("h", index), int(entry["at"]))
        if index:
            edge(("h", index - 1), ("h", index))
    for node in range(n):
        previous = None
        for number, boot in enumerate(boots[node]):
            edges_in = iter(ins.get((node, number), []))
            for position, step in enumerate(boot["steps"]):
                key = ("s", node, number, position)
                priority[key] = item(key, step["t"])
                if previous:
                    edge(previous, key)
                previous = key
                if position == 0:
                    edge(("h", spans[node, number]["boot"]), key)
                if (node, number, position) in fed:
                    edge(("h", fed[node, number, position]), key)
            for position, _ in emitted[node, number]:
                edge(("s", node, number, position), ("h", next(edges_in)))
            if boot["steps"]:
                edge(previous, ("h", spans[node, number]["kill"]))
    ready = [(priority[key], n_, key) for n_, key in enumerate(items) if not waits[key]]
    heapq.heapify(ready)
    order, serial = [], len(items)
    while ready:
        _, _, key = heapq.heappop(ready)
        order.append(key)
        for other in after[key]:
            waits[other] -= 1
            if not waits[other]:
                serial += 1
                heapq.heappush(ready, (priority[other], serial, other))
    need(len(order) == len(items), "the record has no order: something was handled before it "
                                   "was sent, or sent before the step that sent it")

    # ---- the simulator, scripted in that order
    requests = [(e["node"], e["event"]) for e in log if e["what"] == "request"]
    decisions = [e.get("decision") for e in log if e["what"] == "in"]
    prof = sim.profile(nodes=n, timed=True, max_steps=10 ** 7,
                       budget={"crash": sum(map(len, boots)) + 1, "dup": decisions.count("dup"),
                               "drop": decisions.count("drop") + decisions.count("cut")})
    times = []
    origin = min(boot[0]["steps"][0]["t"] for boot in boots if boot[0]["steps"])
    World = clocked(times, origin)
    workload = Feed(requests)
    world, trace = sim.start(native, workload, prof, None, World)
    actions = []
    counts = {"steps": 0, "cuts": 0, "crashes": 0, "restarts": 0, "delivered": 0,
              "inferred": 0, "floods": sum(e["what"] == "out" and e["flood"] for e in log),
              "skipped": sum(boot["skips"].count("address") for per in boots for boot in per),
              **{name: decisions.count(name) for name in ("forward", "drop", "dup", "cut", "client")}}

    def act(where, action, when=None, cut=None):
        need(trace["end"] is None, f"{where}: the simulated run has ended ({trace['end']})")
        action = sim.freeze(action)
        need(action in world.enabled(), f"{where}: the simulator cannot do {action} here")
        times.append(when)
        draw = PlanDraw(cut)
        sim.attempt(world, trace, action, draw)
        actions.append((action, list(draw.used)))
        return trace["steps"][-1]

    fed_requests = 0
    for key in order:
        if key[0] == "h":
            entry = log[key[1]]
            what = entry["what"]
            if what == "request":
                act(f"request {entry['event']}", ("client", "feed", fed_requests))
                fed_requests += 1
            elif what == "in":
                data = bytes.fromhex(entry["frame"])
                where = f"frame {entry['id']} from node {entry['node']}"
                if entry["decision"] == "client":
                    act(where, ("output", unframe(schema, data)))
                elif entry["decision"] in ("drop", "cut", "dup"):
                    to, _, event = message(schema, data)
                    act(where, ("dup" if entry["decision"] == "dup" else "drop", "net", to, event))
            elif what == "kill":
                node, number = entry["node"], entry["boot"]
                where = f"node {node} boot {number}: the kill"
                if world.up[node]:
                    act(where, ("crash", node))
                    counts["crashes"] += 1
                need(sim.freeze(world.rows(node))
                     == image_rows(schema, record["boots"][node][number]["rows"]),
                     f"{where}: the block image holds {record['boots'][node][number]['rows']}, "
                     f"the simulated disk {list(world.rows(node))}")
            continue
        _, node, number, position = key
        boot = boots[node][number]
        step = boot["steps"][position]
        where = f"node {node} boot {number} event {step['n']} {step['event']}"
        cut = None
        if position == len(boot["steps"]) - 1 and step["applied"] < len(step["effects"]):
            cut = step["applied"]
        if position == 0:
            need(number or cut is None, f"{where}: a death inside the first restart step "
                                        "cannot be scripted in the simulator")
            need(number == 0 or not world.up[node], f"{where}: the simulated node is still up")
            rec = act(where, ("restart", node), step["t"], cut) if number else trace["steps"][node]
            counts["restarts"] += bool(number)
        elif step["role"] in ("request", "recv"):
            rec = act(where, ("deliver", node, step["event"]), step["t"], cut)
            counts["delivered"] += step["role"] == "recv"
        else:
            rec = act(where, ("local", node, step["event"]), step["t"], cut)
        need(rec["kind"] != "error", f"{where}: the simulator's step has no result: {rec.get('error')}")
        for name in ("event", "post", "effects", "applied"):
            need(sim.freeze(rec[name]) == sim.freeze(step[name]),
                 f"{where}: {name} differs\n  driver    {step[name]}\n  simulator {rec[name]}")
        need(not (number or position) or world.clock == step["t"],
             f"{where}: the driver handled this at tick {step['t']}, the simulator's clock "
             f"is at {world.clock}")
        counts["steps"] += 1
        counts["cuts"] += cut is not None
        counts["inferred"] += bool(step.get("inferred"))
        if cut is None:
            for effect in step["effects"]:
                if role(schema, effect) == "disk-write":
                    act(where, ("sync", node))
    if trace["end"] is None:
        trace["end"] = "quiescent" if sim.idle(world) else "open"
    trace = json.loads(sim.dumps(trace))
    try:
        again = sim.script(native, workload, prof, actions, world=World)
        need(sim.dumps(again) == sim.dumps(trace), "the reconstructed trace is not reproducible")
        sim.replay(native, workload, trace, world=World)
    except sim.Divergence as error:
        raise Mismatch(f"the reconstructed trace does not replay: {error}") from None
    return trace, {"boots": boots, "counts": counts, "workload": workload, "world": World}
