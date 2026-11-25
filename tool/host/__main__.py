"""python3 -m tool.host <host.json>     run one node as an OS process
python3 -m tool.host <packed directory> <node> <data directory> <client host:port> <peer host:port> ...

The host knows the manifest's roles and nothing else about the application.
See tool/README.md, section 3, for the configuration, the wire and the log.
The second form runs a node from what tool/host/pack.py wrote; the peers are
every node's peer address in node order, this node's being where it listens.
"""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import selectors
import signal
import socket
import sys
import threading
import time

from ..compile import Trap
from ..schema import Schema, SchemaError
from ..wasm import Wasm
from . import wal


def die():
    os.kill(os.getpid(), signal.SIGKILL)


class Host:
    def __init__(self, config):
        self.schema = Schema(config["manifest"])
        self.wasm = Wasm(self.schema, config["wasm"])
        self.node, self.peers = config["node"], [tuple(p) for p in config["peers"]]
        self.init = tuple(config["init"])
        self.data = Path(config["data"])
        self.data.mkdir(parents=True, exist_ok=True)
        # HOST_CRASH=point:n makes the n-th pass through a named point a kill -9 of this process.
        point, _, count = os.environ.get("HOST_CRASH", "").partition(":")
        self.crash_point, self.crash_left = point, int(count or 0)
        self.ready = deque()                # (event, cause) waiting for the node
        self.timers = {}                    # id -> (deadline, cause)
        self.clients, self.links = [], {}
        self.jobs, self.done = queue.SimpleQueue(), queue.SimpleQueue()
        self.selector = selectors.DefaultSelector()

        # A torn tail is a record nothing acted on (the log) or a write nobody was told
        # about (the disk): cut both before appending.
        self.seq = len(wal.repair(self.data / "events.log"))
        wal.repair(self.data / "disk.log")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        self.log_fd = os.open(self.data / "events.log", flags, 0o644)
        self.disk_fd = os.open(self.data / "disk.log", flags, 0o644)
        directory = os.open(self.data, os.O_RDONLY)
        wal.sync(directory)
        os.close(directory)

        self.wake, wake = os.pipe()
        self.selector.register(self.wake, selectors.EVENT_READ, self.completions)
        threading.Thread(target=self.disk, args=(wake,), daemon=True).start()
        # The boot record is durable before the node can be reached.
        self.append({"boot": {"node": self.node, "nodes": len(self.peers), "init": self.init,
                              "wasm": hashlib.sha256(Path(self.wasm.library).read_bytes()).hexdigest()}})
        self.state = self.init
        self.listen(tuple(config["client"]), "request", self.clients)
        self.listen(self.peers[self.node], "recv", None)

    # ---- the one path into the node

    def run(self):
        if "restart" in self.schema.roles:
            self.deliver((self.schema.roles["restart"].tag,), None)
        while True:
            due = min(self.timers, key=lambda ident: (self.timers[ident][0], ident), default=None)
            wait = None if due is None else max(0, self.timers[due][0] - time.monotonic())
            for key, _ in self.selector.select(wait):
                key.data(key.fileobj)
            # A due timer is delivered at once, never queued, so a cancel always wins.
            if due is not None and self.timers[due][0] <= time.monotonic():
                self.deliver((self.schema.roles["timer"].tag, due), self.timers.pop(due)[1])
            while self.ready:
                self.deliver(*self.ready.popleft())

    def deliver(self, event, cause):
        """Step, make the record durable, and only then let the effects out."""
        try:
            after, effects = self.wasm.step(self.state, event)
        except Trap:
            self.append({"event": event, "cause": cause, "trap": True})
            sys.exit(f"node {self.node}: the step has no result for {event}")
        seq = self.append({"event": event, "cause": cause, "effects": effects,
                           "post": self.schema.decode_state(after)})
        if self.crash("logged"):
            die()
        for k, effect in enumerate(effects):
            if k and self.crash("effect"):
                die()
            self.act([seq, k], effect)
        self.state = after

    def append(self, record):
        line = wal.frame({"seq": self.seq, **record})
        torn = self.crash("log-torn")
        os.write(self.log_fd, line[:len(line) // 2] if torn else line)
        wal.sync(self.log_fd)
        if torn:
            die()
        self.seq += 1
        return self.seq - 1

    def crash(self, point):
        """True on the n-th pass through the configured crash point."""
        if point != self.crash_point:
            return False
        self.crash_left -= 1
        return self.crash_left == 0

    # ---- effects by role

    def act(self, cause, effect):
        schema, (tag, *fields) = self.schema, effect
        role = schema.effects[tag].role
        if role == "output":
            self.broadcast(wal.line(effect))
        elif role == "send":
            dest, *payload = fields
            if not (type(dest) is int and 0 <= dest < len(self.peers)):
                sys.exit(f"node {self.node} sends to {dest}, which is not a node")
            self.send(dest, wal.line({"cause": [self.node, *cause], "event":
                                      [schema.effects[tag].deliver, self.node, *payload]}))
        elif role in ("disk-write", "disk-read"):
            self.jobs.put((role, cause, fields))
        elif role == "timer-set":
            self.timers[fields[0]] = (time.monotonic() + fields[1] / 1000, cause)
        elif role == "timer-cancel":
            self.timers.pop(fields[0], None)
        elif role == "clock-read":
            self.ready.append(((schema.roles["clock"].tag, fields[0],
                                time.monotonic_ns() // 1_000_000), cause))

    def disk(self, wake):
        """The disk, in its own thread and in the order asked: a write is appended and
        synced before its acknowledgement exists; a read scans the durable file."""
        schema = self.schema
        while True:
            role, cause, fields = self.jobs.get()
            if role == "disk-write":
                token, key, *vals = fields
                line = wal.frame([*cause, key, *vals])
                torn = self.crash("disk-torn")
                os.write(self.disk_fd, line[:len(line) // 2] if torn else line)
                wal.sync(self.disk_fd)
                if torn or self.crash("synced"):
                    die()
                event = (schema.roles["disk-ack"].tag, token) if "disk-ack" in schema.roles else None
            else:
                token, key = fields
                result = schema.roles["disk-read-result"]
                rows = [row[3:] for row in wal.read(self.data / "disk.log")[0] if row[2] == key]
                vals = rows[-1] if rows else schema.default(result.fields[3:])
                event = (result.tag, token, key, int(bool(rows)), *vals)
            self.done.put((event, cause))
            os.write(wake, b"!")

    def completions(self, pipe):
        os.read(pipe, 4096)
        while not self.done.empty():
            event, cause = self.done.get()
            if event:
                self.ready.append((event, cause))

    # ---- sockets: one JSON value per line

    def listen(self, address, role, members):
        server = socket.create_server(address)
        server.setblocking(False)

        def accept(_):
            conn, _ = server.accept()
            conn.setblocking(False)
            if members is not None:
                members.append(conn)
            buffer = bytearray()
            self.selector.register(conn, selectors.EVENT_READ, lambda c: self.receive(c, buffer, role))
        self.selector.register(server, selectors.EVENT_READ, accept)

    def receive(self, conn, buffer, role):
        try:
            data = conn.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            return self.drop(conn)
        buffer += data
        *lines, rest = bytes(buffer).split(b"\n")
        buffer[:] = rest
        for line in lines:
            # Only a declared event of this port's role, with well-typed fields, gets in.
            try:
                message = json.loads(line)
                event, cause = (message, None) if role == "request" else (message["event"], message["cause"])
                event = tuple(event)
                if self.schema.events[event[0]].role != role:
                    raise SchemaError(f"{event[0]} is not a {role}")
                self.schema.encode_input(self.state, event)
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            self.ready.append((event, cause))

    def broadcast(self, line):
        for conn in list(self.clients):
            try:
                conn.sendall(line)
            except OSError:                 # the output is lost, and so is this client
                self.drop(conn)

    def drop(self, conn):
        self.selector.unregister(conn)
        if conn in self.clients:
            self.clients.remove(conn)
        conn.close()

    def send(self, dest, line):
        """At most once: a message that cannot be handed to TCP now is dropped."""
        for _ in range(2):
            try:
                if dest not in self.links:
                    self.links[dest] = socket.create_connection(self.peers[dest], timeout=0.2)
                self.links[dest].sendall(line)
                return
            except OSError:
                link = self.links.pop(dest, None)
                if link:
                    link.close()


def configuration(argv):
    """host.json as it is, or the same thing from a packed directory and addresses."""
    packed = Path(argv[0])
    if not packed.is_dir():
        return json.loads(packed.read_text())
    address = lambda text: (text.rpartition(":")[0], int(text.rpartition(":")[2]))
    node, inits = int(argv[1]), json.loads((packed / "inits.json").read_text())
    peers = [address(peer) for peer in argv[4:]]
    if len(peers) != len(inits) or not 0 <= node < len(inits):
        sys.exit(f"{packed} was packed for {len(inits)} nodes; node {node} of {len(peers)} asked")
    return {"manifest": str(packed / "node.json"), "wasm": str(packed / "node.wasm"), "node": node,
            "init": inits[node], "data": argv[2], "client": address(argv[3]), "peers": peers}


if __name__ == "__main__":
    Host(configuration(sys.argv[1:])).run()
