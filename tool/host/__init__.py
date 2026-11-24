"""A real host for the wasm node: OS processes, TCP, files and the monotonic clock.

    python3 -m tool.host <host.json>                    one node (see __main__.py)
    python3 -m tool.host.conform <node.json> <dir> ...  check the logs of a run

This module is what a check needs to stage a run on one machine: compile a
bundle, start and kill -9 node processes on loopback, and talk to a client port.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from ..compile import DEPTH, build, initial_state
from ..wasm import link
from . import wal

ROOT = Path(__file__).parents[2]


def bundle(schema, directory, nodes=1, source=None):
    """Compile the specification to <directory>/node.wasm next to a copy of the manifest."""
    directory = Path(directory)
    build(schema, directory, source)
    link(directory / "node.c", directory / "node.wasm", 2 * DEPTH)
    (directory / "node.json").write_text(json.dumps(schema.manifest))
    return {"manifest": str(directory / "node.json"), "wasm": str(directory / "node.wasm"),
            "inits": [initial_state(schema, node, nodes, source) for node in range(nodes)]}


def free_ports(count):
    """Loopback ports nobody is listening on right now."""
    sockets = [socket.create_server(("127.0.0.1", 0)) for _ in range(count)]
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


class Cluster:
    """The nodes of one run: a data directory, a client port and a peer port each."""

    def __init__(self, bundle, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        count = len(bundle["inits"])
        ports = free_ports(2 * count)
        self.clients = [("127.0.0.1", port) for port in ports[:count]]
        self.data = [self.directory / f"node{node}" for node in range(count)]
        self.procs, self.kills = [None] * count, 0
        for node in range(count):
            (self.directory / f"host{node}.json").write_text(json.dumps({
                "manifest": bundle["manifest"], "wasm": bundle["wasm"], "node": node,
                "init": bundle["inits"][node], "data": str(self.data[node]),
                "client": self.clients[node],
                "peers": [("127.0.0.1", port) for port in ports[count:]]}))

    def start(self, node, crash=None):
        """Start the node's process and wait until its client port answers."""
        env = {**os.environ, "HOST_CRASH": crash or ""}
        self.procs[node] = subprocess.Popen(
            [sys.executable, "-m", "tool.host", str(self.directory / f"host{node}.json")],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and self.procs[node].poll() is None:
            try:
                socket.create_connection(self.clients[node], timeout=1).close()
                return
            except OSError:
                time.sleep(0.01)

    def dead(self, node):
        return self.procs[node] is None or self.procs[node].poll() is not None

    def kill(self, node):
        """kill -9. Returns False when the process had already died."""
        proc, self.procs[node] = self.procs[node], None
        if proc is None:
            return False
        alive = proc.poll() is None
        if alive:
            proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
        return alive

    def stop(self):
        for node in range(len(self.procs)):
            self.kill(node)


class Client:
    """Requests out and outputs back on a node's client port, one JSON value per line.

    A connection that breaks is reopened on the next call; everything read is kept in `got`.
    """

    def __init__(self, address):
        self.address, self.conn, self.buffer, self.got = address, None, b"", []

    def send(self, event):
        try:
            if self.conn is None:
                self.conn, self.buffer = socket.create_connection(self.address, timeout=1), b""
            self.conn.sendall(wal.line(event))
        except OSError:
            self.close()

    def wait(self, wanted, timeout):
        """Read outputs until one satisfies `wanted` or the time is up."""
        deadline = time.monotonic() + timeout
        while self.conn is not None:
            *lines, self.buffer = self.buffer.split(b"\n")
            outputs = [tuple(json.loads(line)) for line in lines]
            self.got += outputs
            found = next((o for o in outputs if wanted(o)), None)
            if found or time.monotonic() >= deadline:
                return found
            try:
                self.conn.settimeout(max(0.001, deadline - time.monotonic()))
                data = self.conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                data = b""
            if not data:
                self.close()
            self.buffer += data
        return None

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.conn = None
