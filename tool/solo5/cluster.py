"""Run several nodes as one cluster, with this process as the network between them.

    python3 -m tool.solo5.cluster <host|spt> <directory of images> <out.json> RUNS [image ...]

Every node is one process (a host process, or a unikernel under solo5-spt) with its own
block image and its own link: a directory of datagram sockets for a host process, a tap
interface for a unikernel. Nothing connects two links but this harness. It is the switch
and the client:

* A frame a node puts on its link is read here. A frame to the broadcast address is for
  the client. A frame to the address of a node (02:53:35:00:00:NN, see driver.c) is a
  message to that node, and what happens to it is decided here, from the scenario's
  seed: it is forwarded after a delay, dropped, forwarded twice, or cut because a
  partition separates the two nodes at the moment it arrives. With a small probability a
  forwarded frame is also put on the link of a node it is not addressed to, as a switch
  that does not know the address would; a correct driver ignores it.
* A node is killed with SIGKILL and booted again on the same block image.
* The client sends puts and gets to whichever node last said it leads.

The record has, in the order this process did them, every frame it read and what it
decided, every frame it wrote, every request, kill, boot, cut and heal, each with the
harness's clock (CLOCK_MONOTONIC, milliseconds). Per node and boot it has the console
lines and when each was read, the exit status, the argument vector and the rows of the
block image after the kill. tool/solo5/cluster_compare.py interprets it; nothing here does.

What the seed does not fix: when a node's timer fires and how long a process takes are
the machine's. Two runs of one scenario and seed differ. A run is recorded, and it is the
record that is checked and replayed, not the seed.
"""
import heapq
import json
import os
from pathlib import Path
import random
import select
import shutil
import socket
import struct
import sys
import tempfile

from ..schema import Schema
from . import emit
from .run import BLOCK, BROADCAST, Boot, ETHERTYPE, RunError, clock, frame, rows, sha, unframe

PREFIX = bytes([0x02, 0x53, 0x35, 0x00, 0x00])
DRAIN = 60.0               # ms a link is read for after its node was killed
PATIENCE = 10000.0         # ms: no wait in a scenario is longer


def address(node):
    return PREFIX + bytes([node])


def node_of(mac):
    """The node an address names, or None."""
    return mac[5] if mac[:5] == PREFIX else None


def message(schema, data):
    """A frame between nodes as (destination, claimed source by address, event), or None.
    The event is what the receiver's step is handed: the tag, the sender, the payload."""
    if len(data) < 30 or struct.unpack(">H", data[12:14])[0] != ETHERTYPE:
        return None
    count, code = struct.unpack("<2q", data[14:30])
    kind = schema.events.get(schema.symbol.get(code))
    if kind is None or kind.role != "recv" or count != 1 + kind.width or len(data) < 30 + 8 * kind.width:
        return None
    ints = struct.unpack(f"<{kind.width}q", data[30:30 + 8 * kind.width])
    try:
        event = [kind.tag, *schema.decode(iter(ints), kind.fields)]
    except Exception:
        return None
    return node_of(data[0:6]), node_of(data[6:12]), event


class UnixLink:
    """One host process: it is bound to DIR/node and sends to DIR/client."""

    def __init__(self, directory):
        self.directory = str(directory)
        os.makedirs(self.directory, exist_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.socket.bind(self.directory + "/client")
        self.socket.setblocking(False)

    def options(self):
        return [f"--net:{emit.NET}={self.directory}"]

    def send(self, data):
        """Whether the frame was handed to the node's socket. It is not when no process
        is bound there, or when its queue is full: a link without carrier, or a full one."""
        try:
            self.socket.sendto(data, self.directory + "/node")
            return True
        except OSError:
            return False

    def take(self):
        try:
            return self.socket.recv(4096)
        except BlockingIOError:
            return None

    def close(self):
        self.socket.close()


class TapLink:
    """One unikernel under solo5-spt: its device is a tap interface, and this is a raw
    socket on that interface."""

    def __init__(self, interface):
        self.interface = interface
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.socket.bind((interface, ETHERTYPE))
        self.socket.setblocking(False)

    def options(self):
        return [f"--net:{emit.NET}={self.interface}"]

    def send(self, data):
        try:
            self.socket.send(data)
            return True
        except OSError:
            return False

    def take(self):
        while True:
            try:
                data, where = self.socket.recvfrom(4096)
            except BlockingIOError:
                return None
            if where[2] != 4:                   # PACKET_OUTGOING: what this process sent
                return data

    def close(self):
        self.socket.close()


class Cluster:
    def __init__(self, schema, nodes, command, links, images, seed, blocks=16):
        self.schema, self.n, self.command, self.links, self.images = schema, nodes, command, links, images
        self.rng = random.Random(seed)
        self.blocks = blocks
        self.log = []
        self.boots = [[] for _ in range(nodes)]         # finished boots, per node
        self.live = [None] * nodes                      # the running Boot, per node
        self.argv = [None] * nodes
        self.seen = [0] * nodes                         # console lines already looked at
        self.belief = [None] * nodes                    # (role, term) of the last @step
        self.killed_at = [-1e18] * nodes
        self.pending, self.sequence, self.frames = [], 0, 0
        self.blocked = set()
        self.faults = {"drop": 0.0, "dup": 0.0, "flood": 0.0, "delay": [0.0, 0.0]}
        self.replies = {}                               # request number -> reply effect
        self.requests = 0
        self.led, self.mark = -1, -1                    # the highest term a node said it led
        # Where a node's console shows what it believes: the first symbol of the state is
        # its role and the integer after it its term. True of targets/raft; the harness
        # uses it only to choose whom the client talks to and whom to kill or cut off.
        self.role = schema.manifest["state"].index("sym")
        for image in images:
            Path(image).write_bytes(bytes(BLOCK * blocks))

    # ---- the record

    def note(self, what, **more):
        self.log.append({"at": clock(), "what": what, **more})
        return self.log[-1]

    # ---- nodes

    def boot(self, node):
        if self.live[node] is not None:
            raise RunError(f"node {node} is already running")
        self.pump(self.killed_at[node] + DRAIN)         # what the dead boot sent is read first
        options = [f"--block:{emit.DISK}={self.images[node]}"] + self.links[node].options()
        self.argv[node] = self.command(options, [f"node={node}", f"nodes={self.n}"])
        self.note("boot", node=node, boot=len(self.boots[node]))
        self.live[node] = Boot(self.argv[node])
        self.seen[node], self.belief[node] = 0, None

    def kill(self, node):
        boot = self.live[node]
        if boot is None:
            raise RunError(f"node {node} is not running")
        status, cpu = boot.kill()
        # Noted once the process is dead: nothing the node did is later than this entry.
        self.note("kill", node=node, boot=len(self.boots[node]))
        self.killed_at[node] = clock()
        self.live[node], self.belief[node] = None, None
        self.boots[node].append({"lines": list(boot.lines), "at": list(boot.at),
                                 "other": boot.other[:60], "argv": list(self.argv[node]),
                                 "status": status, "cpu": cpu, "blocks": self.blocks,
                                 "rows": rows(self.schema, self.images[node])})

    def look(self):
        """What each running node last said of itself: its role and term."""
        for node, boot in enumerate(self.live):
            if boot is None:
                continue
            with boot.changed:
                lines = boot.lines[self.seen[node]:]
                self.seen[node] += len(lines)
            for line in lines:
                if line.startswith("@step "):
                    words = line.split()
                    self.belief[node] = (self.schema.symbol.get(int(words[1 + self.role])),
                                         int(words[2 + self.role]))
                    if self.belief[node][0] == "leader":
                        self.led = max(self.led, self.belief[node][1])
                elif line.split()[0] in ("@fail", "@trap"):
                    raise RunError(f"node {node} stopped itself: {line}")

    def leader(self):
        """The running node that says it leads the highest term, or None."""
        self.look()
        leaders = [(belief[1], node) for node, belief in enumerate(self.belief)
                   if belief and belief[0] == "leader" and self.live[node] is not None]
        return max(leaders)[1] if leaders else None

    def pick(self, who):
        if who in ("leader", "follower"):
            self.wait(lambda: self.leader() is not None, "a leader")
            lead = self.leader()
            return lead if who == "leader" else next(
                node for node in range(self.n) if node != lead and self.live[node])
        if who == "down":
            return next(node for node in range(self.n) if self.live[node] is None)
        return who

    # ---- the network

    def arrive(self, node, data):
        """A frame read from the link of `node`."""
        self.frames += 1
        entry = self.note("in", node=node, boot=len(self.boots[node]), id=self.frames,
                          frame=data.hex())
        if data[0:6] == BROADCAST:
            effect = unframe(self.schema, data)
            entry["decision"] = "client"
            if effect is not None and effect[0] == "reply" and effect[2] == "ok":
                self.replies.setdefault(effect[1], effect)
            return
        parsed = message(self.schema, data)
        to = parsed and parsed[0]
        if to is None or not 0 <= to < self.n:
            entry["decision"] = "unknown"               # not a frame this switch forwards
            return
        entry["to"] = to
        if (node, to) in self.blocked:
            entry["decision"] = "cut"
            return
        draw = self.rng.random()
        low, high = self.faults["delay"]
        if draw < self.faults["drop"]:
            entry["decision"] = "drop"
            return
        copies = 2 if draw < self.faults["drop"] + self.faults["dup"] else 1
        entry["decision"] = "dup" if copies == 2 else "forward"
        entry["delays"] = [self.rng.uniform(low, high) for _ in range(copies)]
        for delay in entry["delays"]:
            self.queue(entry["at"] + delay, to, entry["id"], data, False)
        others = [other for other in range(self.n) if other not in (node, to)]
        if others and self.rng.random() < self.faults["flood"]:
            entry["flood"] = self.rng.choice(others)
            self.queue(entry["at"] + self.rng.uniform(low, high), entry["flood"], entry["id"], data, True)

    def queue(self, due, node, number, data, flood):
        self.sequence += 1
        heapq.heappush(self.pending, (due, self.sequence, node, number, data, flood))

    def pump(self, until, done=None):
        """Be the network until the clock reaches `until` or `done()` holds. Returns
        whether `done()` held."""
        sockets = {link.socket: node for node, link in enumerate(self.links)}
        while True:
            now = clock()
            while self.pending and self.pending[0][0] <= now:
                _, _, node, number, data, flood = heapq.heappop(self.pending)
                # Noted before the frame is written: the node cannot handle it earlier.
                entry = self.note("out", node=node, id=number, flood=flood, frame=data.hex())
                entry["sent"] = self.links[node].send(data)
            if done is not None and done():
                return True
            if now >= until:
                return False
            wait = min(until, self.pending[0][0] if self.pending else until, now + 3.0) - now
            ready, _, _ = select.select(list(sockets), [], [], max(wait, 0.0) / 1000.0)
            for sock in ready:
                while (data := self.links[sockets[sock]].take()) is not None:
                    self.arrive(sockets[sock], data)

    def wait(self, done, what, patience=PATIENCE):
        if not self.pump(clock() + patience, done):
            raise RunError(f"waiting {patience:.0f} ms for {what}: "
                           f"the nodes say {self.belief}")

    # ---- the client

    def operation(self, op, sends=4, patience=600.0):
        """One put or get, under the next request number, sent to whoever leads; sent
        again, under the same number, while there is no `ok` for it. Returns the reply."""
        self.requests += 1
        number = self.requests
        event = ["put", number, op[1], op[2]] if op[0] == "put" else ["get", number, op[1]]
        for _ in range(sends):
            self.wait(lambda: self.leader() is not None or number in self.replies, "a leader")
            if number in self.replies:
                break
            node = self.leader()
            data = frame(self.schema, event)
            entry = self.note("request", node=node, event=event, frame=data.hex())
            entry["sent"] = self.links[node].send(data)
            if self.pump(clock() + patience, lambda: number in self.replies):
                break
        return self.replies.get(number)

    def play(self, moves):
        for node in range(self.n):
            self.boot(node)
        for move in moves:
            kind = move[0]
            if kind == "faults":
                self.faults = {**self.faults, **move[1]}
                self.note("faults", **self.faults)
            elif kind == "leader":
                self.wait(lambda: self.leader() is not None, "a leader")
            elif kind == "op":
                self.operation(move[1], **(move[2] if len(move) > 2 else {}))
            elif kind == "kill":
                node = self.pick(move[1])
                self.mark = self.led
                self.kill(node)
            elif kind == "succession":
                self.wait(lambda: self.leader() is not None
                          and self.belief[self.leader()][1] > self.mark, "a leader of a later term")
            elif kind == "boot":
                self.boot(self.pick(move[1]))
            elif kind == "isolate":
                alone = self.pick(move[1])
                self.mark = self.led
                self.blocked = {(a, b) for a in range(self.n) for b in range(self.n)
                                if (a == alone) != (b == alone)}
                self.note("cut", alone=alone)
            elif kind == "heal":
                self.blocked = set()
                self.note("heal")
            elif kind == "run":
                self.pump(clock() + move[1])
            else:
                raise RunError(f"unknown move {move}")

    def stop(self):
        """Kill what runs and read what it had still sent."""
        for node in range(self.n):
            if self.live[node] is not None:
                try:
                    self.kill(node)
                except Exception as failure:            # the record keeps what there is
                    self.note("error", error=f"kill of node {node}: {failure}")
        self.pump(clock() + DRAIN)


def play(schema, nodes, command, links, images, scenario, seed):
    cluster = Cluster(schema, nodes, command, links, images, seed)
    error = None
    try:
        cluster.play(scenario["moves"])
    except (RunError, OSError, StopIteration) as failure:
        error = f"{type(failure).__name__}: {failure}"
    finally:
        cluster.note("end")                      # the kills after this one end the run
        cluster.stop()
    record = {"schema": schema.name, "nodes": nodes, "seed": seed, "log": cluster.log,
              "boots": cluster.boots, "replies": {str(k): v for k, v in cluster.replies.items()}}
    if error is not None:
        record["error"] = error
    return record


def session(kind, directory, work, nodes, tender="solo5-spt", taps="tap"):
    """(links, command builder for an image name, the tender's path or None)."""
    directory = Path(directory)
    if kind == "host":
        links = [UnixLink(Path(work) / f"n{node}") for node in range(nodes)]
        return links, lambda name: (lambda options, arguments:
                                    [str(directory / f"{name}.host"), *options, "--", *arguments]), None
    if kind == "spt":
        path = shutil.which(tender)
        if path is None:
            raise RunError(f"no {tender} on PATH")
        links = [TapLink(f"{taps}{node}") for node in range(nodes)]
        return links, lambda name: (lambda options, arguments:
                                    [path, "--mem=32", *options, str(directory / f"{name}.spt"),
                                     *arguments]), path
    raise RunError(f"unknown driver kind {kind}")


def plan(runs, images=None):
    """(image, scenario, seed) of every run: each scenario `runs` times on the unedited
    image, and each mutant image once on each of its scenarios."""
    from .cluster_scenarios import MUTANT_RUNS, SCENARIOS
    out = [("raft", name, seed) for name in SCENARIOS for seed in range(runs)]
    out += [(image, name, 0) for image, names in MUTANT_RUNS.items() for name in names]
    return [run for run in out if not images or run[0] in images]


def run_all(kind, directory, runs, images=None, report=None, **where):
    """Every run of `plan`. Returns {image: {scenario: [record per seed]}}."""
    from .cluster_scenarios import NODE, NODES, SCENARIOS
    out = {}
    schema = Schema(NODE)
    base = tempfile.gettempdir() if len(tempfile.gettempdir()) < 60 else "/tmp"
    with tempfile.TemporaryDirectory(prefix="s5c", dir=base) as work:
        links, builder, tender = session(kind, directory, work, NODES, **where)
        executed = {}
        try:
            for image, name, seed in plan(runs, images):
                file = Path(directory) / f"{image}.{kind}"
                for path in (file, tender):
                    if path and str(path) not in executed:
                        executed[str(path)] = sha(path)
                disks = [os.path.join(work, f"disk{node}.img") for node in range(NODES)]
                record = play(schema, NODES, builder(image), links, disks, SCENARIOS[name], seed)
                record.update(driver=kind, scenario=name,
                              image={"file": file.name, "sha256": executed[str(file)]},
                              tender=tender and {"path": tender, "sha256": executed[tender]})
                out.setdefault(image, {}).setdefault(name, []).append(record)
                if report:
                    report(f"{kind}: {image}: {name} seed {seed}: {len(record['log'])} log entries"
                           + (f", ERROR {record['error']}" if "error" in record else ""))
        finally:
            for link in links:
                link.close()
    return out


def main(argv):
    if len(argv) < 4 or argv[0] not in ("host", "spt"):
        print(__doc__)
        return 2
    where = {}
    for key in ("taps", "tender"):
        if os.environ.get("SOLO5_" + key.upper()):
            where[key] = os.environ["SOLO5_" + key.upper()]
    result = run_all(argv[0], argv[1], int(argv[3]), argv[4:],
                     report=lambda text: print(text, file=sys.stderr, flush=True), **where)
    Path(argv[2]).write_text(json.dumps(result, sort_keys=True) + "\n")
    print(f"{sum(len(r) for per in result.values() for r in per.values())} runs of "
          f"{len(result)} images recorded in {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
