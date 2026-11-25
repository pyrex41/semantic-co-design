"""Run a driver against a scripted client and record what happened.

    python3 -m tool.solo5.run <host|spt> <directory of images> <out.json> [scenario ...]

A scenario (tool/solo5/scenarios.py) is a list of boots on one block image. In each boot
the client makes its moves one at a time, waiting for the node to go idle in between,
and the boot ends with SIGKILL: at the point named by `hold` (after K effects of the
N-th event, where the driver has stopped and waits to be killed) or after the last move.

The record of a boot has three independent parts: the driver's console lines, the frames
the client received, and the rows found in the block image after the process was killed.
Nothing here interprets them; tool/solo5/compare.py does.

Beside them the harness notes what only it can know: when it sent each request and when
it read each console line, on its own clock (CLOCK_MONOTONIC, in milliseconds); the
processor time the process used; its exit status; its argument vector; whatever else it
printed (a tender's banner); and the SHA-256 of the image and of the tender it executed.

A boot may be given a device that fails. `fsize: 0` gives the process a file size limit
of zero with SIGXFSZ ignored, so that every write to the block file fails (EFBIG) and
nothing else does: reads, the console and the network are not regular files being
extended. The limit is set once the node has booted, with prlimit(2), because the spt
tender itself writes a file while it starts; where there is no prlimit (macOS, host
process only) it is set before exec. The move `shrink N` truncates the block file to N
blocks while the process runs, so that a read beyond them comes back short; the file
gets its size back before its rows are read.

    host   the image is `<name>.host`, a host process (driver.c + host.c); the block device
           is a file and the network a pair of datagram sockets in a directory
    spt    the image is `<name>.spt`, run by solo5-spt; the block device is a file and the
           network a tap interface, which this process reaches through an AF_PACKET socket
"""
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
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


def clock():
    """The harness's clock in milliseconds: CLOCK_MONOTONIC, which is also what host.c
    reads and what solo5-spt gives its guest."""
    return time.clock_gettime(time.CLOCK_MONOTONIC) * 1000.0


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


PRLIMIT = hasattr(resource, "prlimit")


def refuse_writes():
    """In the child, before exec: exceeding the file size limit is an error (EFBIG)
    instead of a fatal signal, and, where the limit cannot be set later, it is zero now."""
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    if not PRLIMIT:
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))


def children():
    """Processor seconds used so far by child processes that have been waited for."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


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
    def __init__(self, command, fsize=None):
        # `lines` is the record (console lines starting with '@') and `at` the time each
        # was read; `other` is everything else the process printed (a tender's banner and
        # its complaints).
        self.lines, self.at, self.other, self.changed = [], [], [], threading.Condition()
        self.before = children()
        limited = {} if fsize is None else {"preexec_fn": refuse_writes, "restore_signals": False}
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, **limited)
        self.done = False
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for raw in self.process.stdout:
            text = raw.decode("ascii", "replace").rstrip("\n")
            with self.changed:
                if text.startswith("@"):
                    self.lines.append(text)
                    self.at.append(clock())
                else:
                    self.other.append(text)
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
        and the node then went idle. Or the node has stopped. Or, when an event is waited
        for, the driver said it skipped a frame instead: the comparison will find the
        request missing."""
        def ready():
            seen, idle, skipped = start == 0, False, False
            for line in self.lines[start:]:
                words = line.split()
                if words[0] == "@event":
                    seen, idle = seen or code is None or int(words[3]) == code, False
                elif words[0] == "@idle":
                    idle = True
                elif words[0] == "@skip" and code is not None and words[1] != "ethertype":
                    skipped = True
            return self.stopped() or skipped or (seen and idle and self.count("@event") >= events)
        self.wait(ready, "the node to settle")

    def tick(self):
        """The tick of the last event the driver logged."""
        return [int(line.split()[2]) for line in self.lines if line.startswith("@event ")][-1]

    def kill(self):
        """SIGKILL. Returns the exit status (negative when the signal ended the process;
        a process that had already stopped keeps its own) and the processor seconds used."""
        self.process.send_signal(signal.SIGKILL)
        status = self.process.wait(timeout=TIMEOUT)
        self.reader.join(timeout=TIMEOUT)
        self.process.stdout.close()
        return status, children() - self.before


def play(schema, command, link, image, scenario):
    """Run one scenario. `command(options, arguments)` is the argv of one boot.

    A boot that cannot be played to its end (the driver exits, or never settles) is
    recorded with the reason under `error`, and the scenario stops there.
    """
    disk = emit.row_width(schema) is not None
    blocks = scenario.get("blocks", 8) if disk else 0
    if disk:
        Path(image).write_bytes(bytes(BLOCK * blocks))
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
        argv = command(options, arguments)
        boot = Boot(argv, plan.get("fsize"))
        sent, sent_at, received, error = [], [], [], None
        try:
            boot.settled()
            if plan.get("fsize") is not None and PRLIMIT:
                resource.prlimit(boot.process.pid, resource.RLIMIT_FSIZE,
                                 (plan["fsize"], resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
            for move in plan.get("moves", []):
                if boot.stopped():
                    break
                start = len(boot.lines)
                if move[0] == "send":
                    # A field {"due": T} is the delay that makes a timer armed now due at
                    # tick T of the driver's clock.
                    event = [field["due"] - boot.tick() if isinstance(field, dict) else field
                             for field in move[1]]
                    sent_at.append(clock())
                    link.send(frame(schema, event))
                    sent.append(event)
                    boot.settled(start, schema.code[event[0]])
                elif move[0] == "shrink":         # the device loses all but its first blocks
                    os.truncate(image, BLOCK * move[1])
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
            status, cpu = boot.kill()
        drain(received)
        if disk:
            os.truncate(image, BLOCK * blocks)    # a shrunk device gets its (empty) blocks back
        boots.append({"hold": plan.get("hold"), "lines": list(boot.lines), "at": list(boot.at),
                      "other": boot.other[:60], "argv": list(argv), "sent": sent,
                      "sent_at": sent_at, "received": received, "status": status, "cpu": cpu,
                      "blocks": blocks, "rows": rows(schema, image) if disk else []})
        if error is not None:
            boots[-1]["error"] = error
            break
    return {"schema": schema.name, "boots": boots}


def session(kind, directory, work, tap="tap0", tender="solo5-spt"):
    """(link, command builder for an image name, the tender's path or None) for one kind
    of driver."""
    directory = Path(directory)
    if kind == "host":
        link = UnixLink(work)
        return link, lambda name: (lambda options, arguments:
                                   [str(directory / f"{name}.host"), *options, "--", *arguments]), None
    if kind == "spt":
        path = shutil.which(tender)
        if path is None:
            raise RunError(f"no {tender} on PATH")
        link = TapLink(tap)
        return link, lambda name: (lambda options, arguments:
                                   [path, "--mem=16", *options, str(directory / f"{name}.spt"),
                                    *arguments]), path
    raise RunError(f"unknown driver kind {kind}")


def run_all(kind, directory, names=None, **where):
    """Every scenario (or the named ones) against the images in `directory`."""
    from .scenarios import IMAGES, SCENARIOS, manifest
    out = {}
    # A socket path is at most about a hundred bytes.
    base = tempfile.gettempdir() if len(tempfile.gettempdir()) < 60 else "/tmp"
    with tempfile.TemporaryDirectory(prefix="s5", dir=base) as work:
        link, builder, tender = session(kind, directory, work, **where)
        executed = {}                             # file -> SHA-256, read once
        try:
            for name, scenario in SCENARIOS.items():
                if names and name not in names:
                    continue
                schema = Schema(manifest(IMAGES[scenario["image"]]))
                file = Path(directory) / f"{scenario['image']}.{kind}"
                for path in (file, tender):
                    if path and str(path) not in executed:
                        executed[str(path)] = sha(path)
                out[name] = play(schema, builder(scenario["image"]), link,
                                 os.path.join(work, "disk.img"), scenario)
                out[name]["driver"] = kind
                out[name]["image"] = {"file": file.name, "sha256": executed[str(file)]}
                out[name]["tender"] = tender and {"path": tender, "sha256": executed[tender]}
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
