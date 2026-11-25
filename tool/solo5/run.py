"""Run a driver against a scripted client and record what happened.

    python3 -m tool.solo5.run <host|spt> <directory of images> <out.json> [scenario ...]

A scenario (tool/solo5/scenarios.py) is a list of boots on one block image. In each boot
the client makes its moves one at a time, waiting for the node to go idle in between,
and the boot ends with SIGKILL: at the point named by `hold` (after K effects of the
N-th event, where the driver has stopped and waits to be killed) or after the last move.

The record of a boot has three independent parts: the driver's console lines, the frames
the client received, and the rows found in the block image after the process was killed.
Nothing here interprets them; tool/solo5/compare.py does.

    host   the image is `<name>.host`, a host process (driver.c + host.c); the block device
           is a file and the network a pair of datagram sockets in a directory
    spt    the image is `<name>.spt`, run by solo5-spt; the block device is a file and the
           network a tap interface, which this process reaches through an AF_PACKET socket
"""
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from ..schema import Schema
from . import emit

ETHERTYPE = 0x88B5
CLIENT_MAC, BROADCAST = bytes([2, 0, 0, 0, 0, 2]), b"\xff" * 6
BLOCK = 512
TIMEOUT = 20.0
STOPS = ("@hold", "@trap", "@fail")


class RunError(RuntimeError):
    pass


# ---- frames

def frame(schema, event):
    """A request: Ethernet header, the number of integers, the event's tag code and fields."""
    kind = schema.events[event[0]]
    ints = [schema.code[kind.tag], *schema.encode(tuple(event[1:]), kind.fields)]
    data = (BROADCAST + CLIENT_MAC + struct.pack(">H", ETHERTYPE)
            + struct.pack(f"<{1 + len(ints)}q", len(ints), *ints))
    return data.ljust(60, b"\0")


def unframe(schema, data):
    """An effect the node sent, or None for a frame that is not one."""
    if len(data) < 30 or struct.unpack(">H", data[12:14])[0] != ETHERTYPE:
        return None
    count, code = struct.unpack("<2q", data[14:30])
    kind = schema.effects.get(schema.symbol.get(code))
    if kind is None or count != 1 + kind.width or len(data) < 30 + 8 * kind.width:
        return None
    ints = struct.unpack(f"<{kind.width}q", data[30:30 + 8 * kind.width])
    return [kind.tag, *schema.decode(iter(ints), kind.fields)]


def rows(schema, image):
    """The rows in a block image: [key, value...] for every block that holds one."""
    width, data, out = emit.row_width(schema), Path(image).read_bytes(), []
    if width is None:
        return out
    for key in range(len(data) // BLOCK):
        words = struct.unpack_from(f"<{1 + width}q", data, key * BLOCK)
        if words[0] == 1:
            out.append([key, *words[1:]])
        elif any(data[key * BLOCK:(key + 1) * BLOCK]):
            raise RunError(f"block {key} of {image} is neither empty nor a row")
    return out


# ---- the two ways to reach a node's network device

class UnixLink:
    """host.c: the node is bound to DIR/node and sends to DIR/client."""

    def __init__(self, directory):
        self.directory = str(directory)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(self.directory + "/client")

    def options(self):
        return [f"--net:{emit.NET}={self.directory}"]

    def send(self, data):
        self.socket.sendto(data, self.directory + "/node")

    def receive(self, wait):
        self.socket.settimeout(wait)
        try:
            return self.socket.recv(4096)
        except socket.timeout:
            return None

    def close(self):
        self.socket.close()


class TapLink:
    """solo5-spt: the node's device is a tap interface; we are a raw socket on it."""

    def __init__(self, interface):
        self.interface = interface
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.socket.bind((interface, ETHERTYPE))

    def options(self):
        return [f"--net:{emit.NET}={self.interface}"]

    def send(self, data):
        self.socket.send(data)

    def receive(self, wait):
        self.socket.settimeout(wait)
        while True:
            try:
                data, address = self.socket.recvfrom(4096)
            except socket.timeout:
                return None
            if address[2] != 4:                 # PACKET_OUTGOING: what we sent ourselves
                return data

    def close(self):
        self.socket.close()


# ---- one boot

class Boot:
    def __init__(self, command):
        # `lines` is the record (console lines starting with '@'); `other` is everything
        # else the process printed (a tender's banner and its complaints), kept for errors.
        self.lines, self.other, self.changed = [], [], threading.Condition()
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL)
        self.done = False
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for raw in self.process.stdout:
            text = raw.decode("ascii", "replace").rstrip("\n")
            with self.changed:
                (self.lines if text.startswith("@") else self.other).append(text)
                self.changed.notify_all()
        with self.changed:
            self.done = True
            self.changed.notify_all()

    def count(self, prefix):
        return sum(line.split()[0] == prefix for line in self.lines)

    def wait(self, ready, what):
        deadline = time.monotonic() + TIMEOUT
        with self.changed:
            while not ready():
                left = deadline - time.monotonic()
                if self.done or left <= 0:
                    raise RunError(f"waiting for {what}: the driver "
                                   f"{'exited' if self.done else 'did not get there'}\n"
                                   + "\n".join(self.lines[-12:] + self.other[-12:]))
                self.changed.wait(left)

    def stopped(self):
        """The node will do nothing more: it holds, or it stopped itself."""
        return any(line.split()[0] in STOPS for line in self.lines)

    def settled(self, start=0, code=None, events=0):
        """After line `start`: an event with tag `code` was handled (any event when None;
        none needed when `start` is 0), at least `events` events have been handled in all,
        and the node then went idle. Or the node has stopped."""
        def ready():
            seen, idle = start == 0, False
            for line in self.lines[start:]:
                words = line.split()
                if words[0] == "@event":
                    seen, idle = seen or code is None or int(words[3]) == code, False
                elif words[0] == "@idle":
                    idle = True
            return self.stopped() or (seen and idle and self.count("@event") >= events)
        self.wait(ready, "the node to settle")

    def kill(self):
        """SIGKILL. Returns the exit status: negative when the signal ended the process."""
        self.process.send_signal(signal.SIGKILL)
        status = self.process.wait(timeout=TIMEOUT)
        self.reader.join(timeout=TIMEOUT)
        self.process.stdout.close()
        return status


def play(schema, command, link, image, scenario):
    """Run one scenario. `command(options, arguments)` is the argv of one boot.

    A boot that cannot be played to its end (the driver exits, or never settles) is
    recorded with the reason under `error`, and the scenario stops there.
    """
    disk = emit.row_width(schema) is not None
    if disk:
        Path(image).write_bytes(bytes(BLOCK * scenario.get("blocks", 8)))
    boots = []

    def drain(received):
        while (data := link.receive(0.15)) is not None:
            effect = unframe(schema, data)
            if effect is not None:
                received.append(effect)

    for plan in scenario["boots"]:
        arguments = ["node=0", "nodes=1"]
        if plan.get("hold"):
            arguments.append("hold=%d.%d" % tuple(plan["hold"]))
        options = ([f"--block:{emit.DISK}={image}"] if disk else []) + link.options()
        drain([])                                 # nothing from an earlier boot is ours
        boot = Boot(command(options, arguments))
        sent, received, error = [], [], None
        try:
            boot.settled()
            for move in plan.get("moves", []):
                if boot.stopped():
                    break
                start = len(boot.lines)
                if move[0] == "send":
                    link.send(frame(schema, move[1]))
                    sent.append(move[1])
                    boot.settled(start, schema.code[move[1][0]])
                elif move[0] == "raw":            # a frame that is not a request
                    link.send(bytes.fromhex(move[1]))
                    boot.wait(lambda: boot.stopped() or any(
                        line.startswith("@skip") for line in boot.lines[start:]),
                        "the frame to be skipped")
                elif move[0] == "events":         # wait for the node's own timers
                    boot.settled(0, None, move[1])
                elif move[0] == "sleep":
                    time.sleep(move[1])
                else:
                    raise RunError(f"unknown move {move}")
                drain(received)
            if plan.get("hold"):
                boot.wait(boot.stopped, "the hold point")
        except (RunError, OSError) as failure:
            error = str(failure)
        finally:
            status = boot.kill()
        drain(received)
        boots.append({"hold": plan.get("hold"), "lines": list(boot.lines), "sent": sent,
                      "received": received, "status": status,
                      "rows": rows(schema, image) if disk else []})
        if error is not None:
            boots[-1]["error"] = error
            break
    return {"schema": schema.name, "boots": boots}


def session(kind, directory, work, tap="tap0", tender="solo5-spt"):
    """(link, command builder for an image name) for one kind of driver."""
    directory = Path(directory)
    if kind == "host":
        link = UnixLink(work)
        return link, lambda name: (lambda options, arguments:
                                   [str(directory / f"{name}.host"), *options, "--", *arguments])
    if kind == "spt":
        link = TapLink(tap)
        return link, lambda name: (lambda options, arguments:
                                   [tender, "--mem=16", *options, str(directory / f"{name}.spt"),
                                    *arguments])
    raise RunError(f"unknown driver kind {kind}")


def run_all(kind, directory, names=None, **where):
    """Every scenario (or the named ones) against the images in `directory`."""
    from .scenarios import IMAGES, SCENARIOS, manifest
    out = {}
    # A socket path is at most about a hundred bytes.
    base = tempfile.gettempdir() if len(tempfile.gettempdir()) < 60 else "/tmp"
    with tempfile.TemporaryDirectory(prefix="s5", dir=base) as work:
        link, builder = session(kind, directory, work, **where)
        try:
            for name, scenario in SCENARIOS.items():
                if names and name not in names:
                    continue
                schema = Schema(manifest(IMAGES[scenario["image"]]))
                out[name] = play(schema, builder(scenario["image"]), link,
                                 os.path.join(work, "disk.img"), scenario)
                out[name]["driver"] = kind
        finally:
            link.close()
    return out


def main(argv):
    if len(argv) < 3 or argv[0] not in ("host", "spt"):
        print(__doc__)
        return 2
    where = {}
    for key in ("tap", "tender"):
        if os.environ.get("SOLO5_" + key.upper()):
            where[key] = os.environ["SOLO5_" + key.upper()]
    result = run_all(argv[0], argv[1], argv[3:], **where)
    Path(argv[2]).write_text(json.dumps(result, sort_keys=True) + "\n")
    print(f"{len(result)} scenarios recorded in {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
