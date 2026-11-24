#!/usr/bin/env python3
"""Bind a generated application to persistent records and local Ethernet I/O."""

import argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from hashlib import sha256
from ipaddress import ip_address
import json
import os
from pathlib import Path
import selectors
import socket
import sqlite3
import stat
import sys
from threading import BoundedSemaphore, Lock
import time


MAX_COMMAND = 2 * 1024 * 1024
MAX_FRAME = 9218
MAX_CONTROL_CONNECTIONS = 8
MAX_DURABLE_OPERATIONS = 32
CHECKPOINT_INTERVAL = 1.0


class DriverError(ValueError):
    pass


def read_json(stream):
    raw = stream.readline(MAX_COMMAND + 1)
    if len(raw) > MAX_COMMAND:
        raise DriverError("request exceeds the management message limit")
    if not raw:
        return None
    return json.loads(raw)


def wire(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def app(references):
    here = Path(__file__).resolve().parent
    if references:
        for candidate in (here.parents[1], here.parent / "webapp"):
            sys.path.insert(0, str(candidate))
        try:
            import appc
        except ImportError as exc:
            raise DriverError("appc is required for reference execution") from exc
        if not hasattr(appc, "load"):
            from appc import compiler as appc
        manifest, evaluator = appc.load(references)
        source = b"".join((references.parent / name).read_bytes() for name in manifest["sources"])
        return manifest, appc.reference_entries(manifest, evaluator), sha256(source).hexdigest()

    from _application import entries

    manifest = json.loads((here / "manifest.json").read_text())
    receipt = json.loads((here.parent / "ownership.json").read_text())
    return manifest, entries, receipt["digests"]["native"]


class Process:
    def __init__(self, entries, database, digest):
        self.entries = entries
        self.database = database
        self.digest = digest
        with closing(self.connection()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS application_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL,
                    release_digest TEXT NOT NULL
                )
                """)
            conn.execute("PRAGMA journal_mode=WAL")

    def connection(self):
        conn = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def evaluate(self, command, body, now, state, *, allow_empty=False):
        entry = self.entries.get(command)
        if entry is None:
            raise DriverError("unknown entry")
        if not isinstance(body, dict):
            raise DriverError("arguments must be an object")
        if type(now) is not int or not 0 <= now < 2**63 - 65536:
            raise DriverError("clock outside the supported range")
        if state is None and command != "boot":
            if allow_empty:
                return None
            raise DriverError("the application has no committed configuration")
        if state is not None and command == "boot":
            raise DriverError("the application is already configured")
        return entry(None, {**body, "now": now, "state": state})

    @staticmethod
    def read(conn):
        row = conn.execute("SELECT payload FROM application_state WHERE singleton = 1").fetchone()
        return json.loads(row[0]) if row else None

    def write(self, conn, payload):
        conn.execute("""
            INSERT INTO application_state VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload, release_digest=excluded.release_digest
            """, (payload, self.digest))

    def load(self):
        with closing(self.connection()) as conn:
            return self.read(conn)

    def save(self, state):
        payload = wire(state).decode()
        with closing(self.connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.write(conn, payload)
            conn.commit()

    def call(self, command, body, now, *, details=False, allow_empty=False):
        # Offline replay can have multiple callers sharing a database.
        with closing(self.connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self.read(conn)
            result = self.evaluate(command, body, now, state, allow_empty=allow_empty)
            if result is None:
                return None
            next_state = result.get("state")
            if next_state is not None and next_state != state:
                self.write(conn, wire(next_state).decode())
            conn.commit()
        return result if details else public(result)


def public(result):
    return {name: value for name, value in result.items() if name != "state"}


class BoundedWork:
    def __init__(self, workers, capacity, name):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.slots = BoundedSemaphore(capacity)

    def submit(self, work, *args):
        if not self.slots.acquire(blocking=False):
            return None
        try:
            future = self.pool.submit(work, *args)
        except RuntimeError:
            self.slots.release()
            return None
        future.add_done_callback(lambda _: self.slots.release())
        return future

    def close(self, *, wait=True, cancel=True):
        self.pool.shutdown(wait=wait, cancel_futures=cancel)


class Dataplane:
    def __init__(self, process, profiles, ports, replica):
        self.process, self.ports, self.replica = process, ports, replica
        self.packet = profiles["packet"]
        self.replication = profiles["replication"]
        self.publication = profiles["publication"]
        self.lock = Lock()
        self.state = process.load()
        self.saved = self.state
        self.last_clock = 0
        self.durable = BoundedWork(1, MAX_DURABLE_OPERATIONS, "router-state")
        self.replica_io = BoundedWork(1, 1, "router-replica")
        self.checkpoint_lock = Lock()

    def now_locked(self):
        self.last_clock = max(self.last_clock, int(time.time()))
        return self.last_clock

    def call(self, command, body, _now):
        if command in self.publication["reads"]:
            with self.lock:
                state, now = self.state, self.now_locked()
            return public(self.process.evaluate(command, body, now, state))
        future = self.durable.submit(self.commit, command, body, False, False)
        if future is None:
            raise DriverError("state operation queue is full")
        return future.result()

    def receive(self, label, raw):
        arg = {self.packet["interface"]: label, self.packet["request-bytes"]: list(raw)}
        with self.lock:
            if self.state is None:
                return
            result = self.process.evaluate(self.packet["entry"], arg, self.now_locked(), self.state)
            if not result.get("durable"):
                self.apply_packet_locked(result)
                return
        # Re-evaluate the bounded proposal against the current generation when the
        # durable worker owns it. Until then it has no published binding or frame.
        future = self.durable.submit(self.commit, self.packet["entry"], arg, True, True)
        if future is not None:
            future.add_done_callback(self.report)

    @staticmethod
    def report(future):
        if not future.cancelled() and (error := future.exception()) is not None:
            print("dataplane:", error, file=sys.stderr)

    def commit(self, command, body, packet, proposal):
        with self.lock:
            base, now = self.state, self.now_locked()
            if proposal:
                result = self.process.evaluate(command, body, now, base)
                if not result.get("durable"):
                    self.apply_packet_locked(result)
                    return
        if not proposal:
            result = self.process.evaluate(command, body, now, base)
        next_state = result.get("state")
        if next_state is not None and next_state != base:
            self.process.save(next_state)
            with self.lock:
                if base is None:
                    self.state = next_state
                else:
                    self.state = self.process.evaluate(self.publication["entry"], {
                        "base": base, "next": next_state, "reason": result.get("reason"),
                    }, self.now_locked(), self.state)["state"]
                self.saved = next_state
                if packet:
                    self.finish_packet_locked(result)
        elif packet:
            with self.lock:
                self.finish_packet_locked(result)
        return public(result)

    def apply_packet_locked(self, result):
        self.state = result["state"]
        self.finish_packet_locked(result)

    def finish_packet_locked(self, result):
        if result.get("resume") is not None:
            result = self.process.evaluate(self.packet["resume"], result["resume"], self.now_locked(), self.state)
            self.state = result["state"]
        if self.replica and (offer := result.get(self.replication["offer"])) is not None:
            self.replica_io.submit(self.send_replica, offer)
        frames = result.get(self.packet["response-bytes"], [])
        if frames and self.process.evaluate(self.packet["sending-permitted"], {}, self.now_locked(), self.state)["ready"]:
            for outgoing in frames:
                try:
                    port = self.ports[outgoing[self.packet["interface"]]]
                    port.send(bytes(outgoing[self.packet["request-bytes"]]))
                except (OSError, KeyError) as exc:
                    print("transmit:", exc, file=sys.stderr)

    def send_replica(self, offer):
        profile = self.replication
        try:
            response = remote_call(self.replica, profile["operation"], {profile["arg"]: offer})
            if isinstance(response, dict) and profile["ack"] in response:
                future = self.durable.submit(self.commit, profile["success"], {
                    profile["ack"]: response[profile["ack"]],
                }, True, False)
                if future is not None:
                    future.add_done_callback(self.report)
        except (OSError, ValueError) as exc:
            print("replication:", exc, file=sys.stderr)

    def checkpoint(self):
        try:
            with self.lock:
                state = self.state
            if state is not None and state is not self.saved:
                self.process.save(state)
                self.saved = state
        finally:
            self.checkpoint_lock.release()

    def schedule_checkpoint(self):
        if self.checkpoint_lock.acquire(blocking=False):
            future = self.durable.submit(self.checkpoint)
            if future is not None:
                future.add_done_callback(self.report)
            else:
                self.checkpoint_lock.release()

    def close(self):
        self.replica_io.close()
        self.durable.close()


class RemoteProcess:
    def __init__(self, address):
        self.address = address

    def call(self, command, body, _now):
        response = remote_call(self.address, command, body, timeout=8)
        if not isinstance(response, dict):
            raise DriverError("invalid management response")
        if "error" in response:
            raise DriverError(response["error"])
        return response


@contextmanager
def ownership(database, *, exclusive):
    with open(str(database.resolve()) + ".port-owner", "a") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if exclusive:
                raise DriverError("another process accesses this appliance state") from exc
            raise DriverError("the dataplane owns this database; use its management socket") from exc
        yield


class VirtualPort:
    def __init__(self, endpoints):
        self.local, self.remote = endpoints.split(",", 1)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.sock.bind(self.local)
            os.chmod(self.local, 0o600)
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            raise

    def receive(self):
        return self.sock.recv(MAX_FRAME + 1)

    def send(self, frame):
        self.sock.sendto(frame, self.remote)

    def close(self):
        self.sock.close()
        Path(self.local).unlink(missing_ok=True)


class LinuxPort:
    def __init__(self, interface):
        if not hasattr(socket, "AF_PACKET"):
            raise DriverError("physical Ethernet requires Linux AF_PACKET")
        sysfs = Path("/sys/class/net") / interface
        if (not interface or Path(interface).name != interface or not sysfs.is_dir()
                or not (sysfs / "device").exists() or (sysfs / "master").exists()
                or (sysfs / "type").read_text().strip() != "1"):
            raise DriverError("select a dedicated Ethernet device outside any bridge or bond")
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        try:
            # PACKET_IGNORE_OUTGOING on SOL_PACKET. Fail startup on an unsupported kernel.
            self.sock.setsockopt(263, 23, 1)
            self.sock.bind((interface, 0))
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            raise

    def receive(self):
        return self.sock.recv(MAX_FRAME + 1)

    def send(self, frame):
        self.sock.send(frame)

    def close(self):
        self.sock.close()


def receive_message(connection, deadline):
    buffer = bytearray()
    while len(buffer) <= MAX_COMMAND:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("management request expired")
        connection.settimeout(remaining)
        chunk = connection.recv(min(65_536, MAX_COMMAND + 1 - len(buffer)))
        if not chunk:
            break
        line, sep, _ = chunk.partition(b"\n")
        buffer.extend(line)
        if sep:
            return json.loads(buffer)
    raise DriverError("incomplete or oversized management request")


def process_request(process, operations, connection):
    with connection:
        try:
            message = receive_message(connection, time.monotonic() + 2)
            entry = message.get("entry") if isinstance(message, dict) else None
            if not isinstance(entry, str) or entry not in operations:
                raise DriverError("unavailable management operation")
            response = process.call(operations[entry], message.get("arg", {}), int(time.time()))
        except (ValueError, OSError, sqlite3.Error) as exc:
            response = {"error": str(exc)}
        try:
            connection.settimeout(2)
            connection.sendall(wire(response))
        except OSError:
            pass


def remote_call(address, operation, arg, timeout=2):
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(address)
        client.settimeout(max(0.001, deadline - time.monotonic()))
        client.sendall(wire({"entry": operation, "arg": arg}))
        return receive_message(client, deadline)


def probe_endpoints(process, profile):
    probes = process.call(profile["entry"], {}, int(time.time()))[profile["response"]]
    results = []
    for probe in probes:
        address, port = probe[profile["target"]]
        try:
            with socket.create_connection((str(ip_address(bytes(address))), port), timeout=0.25):
                healthy = True
        except OSError:
            healthy = False
        results.append(process.call(profile["outcome"], {
            **probe, profile["value"]: healthy,
        }, int(time.time())))
    return results


def stream_peer(process, profile, peer, target):
    """Declare a fixed-length prefix to carry an opaque record over TCP."""
    host, port = target.rsplit(",", 1)
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        conn.settimeout(0.5)

        def dispatch(event, record=b""):
            result = process.call(profile["entry"], {
                profile["id"]: peer, profile["event"]: event, profile["request"]: list(record),
            }, int(time.time()))
            for message in result[profile["response"]]:
                conn.sendall(bytes(message))
            return not result[profile["close"]]

        size = profile["header"]
        start = profile["offset"]
        end = start + profile["width"]
        try:
            if not dispatch("connected"):
                return
            buffered = bytearray()
            last_tick = int(time.time())
            while True:
                try:
                    segment = conn.recv(profile["maximum"])
                    if not segment:
                        return
                    buffered.extend(segment)
                except socket.timeout:
                    pass
                while len(buffered) >= size:
                    length = int.from_bytes(buffered[start:end], "big")
                    if not size <= length <= profile["maximum"]:
                        dispatch("record", buffered[:size])
                        return
                    if len(buffered) < length:
                        break
                    record = buffered[:length]
                    del buffered[:length]
                    if not dispatch("record", record):
                        return
                now = int(time.time())
                if now != last_tick:
                    if not dispatch("tick"):
                        return
                    last_tick = now
        finally:
            process.call(profile["entry"], {profile["id"]: peer, profile["event"]: "closed"}, int(time.time()))


def serve(process, manifest, address, virtual, physical, replica):
    bindings = [(*value.split("=", 1), kind)
                for options, kind in ((virtual, VirtualPort), (physical, LinuxPort)) for value in options]
    if len(bindings) < 2 or len({label for label, _, _ in bindings}) != len(bindings):
        raise DriverError("supply at least two distinct logical Ethernet ports")
    if len({name for _, name, kind in bindings if kind is LinuxPort}) != len(physical):
        raise DriverError("a physical interface can only be bound once")
    if os.stat(Path(address).parent).st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise DriverError("management sockets require a private parent directory")

    ports = {}
    selector = selectors.DefaultSelector()
    management = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ingress = BoundedWork(MAX_CONTROL_CONNECTIONS, MAX_CONTROL_CONNECTIONS, "router-management")
    dataplane = None
    bound = False
    try:
        management.bind(address)
        bound = True
        os.chmod(address, 0o600)
        management.listen(MAX_CONTROL_CONNECTIONS)
        management.setblocking(False)
        selector.register(management, selectors.EVENT_READ)
        for label, name, kind in bindings:
            port = ports[label] = kind(name)
            selector.register(port.sock, selectors.EVENT_READ, label)
        profiles = manifest["processes"]
        dataplane = Dataplane(process, profiles, ports, replica)
        next_checkpoint = time.monotonic() + CHECKPOINT_INTERVAL
        while True:
            for key, _ in selector.select(timeout=CHECKPOINT_INTERVAL):
                label = key.data
                try:
                    if label is None:
                        connection, _ = management.accept()
                        task = ingress.submit(process_request, dataplane, profiles["management"], connection)
                        if task is None:
                            connection.close()
                        else:
                            task.add_done_callback(lambda future, sock=connection: sock.close() if future.cancelled() else None)
                    else:
                        dataplane.receive(label, ports[label].receive())
                except BlockingIOError:
                    pass
            now = time.monotonic()
            if now >= next_checkpoint:
                dataplane.schedule_checkpoint()
                next_checkpoint = now + CHECKPOINT_INTERVAL
    finally:
        selector.close()
        management.close()
        # Keep accepted sockets available until their bounded reads/replies finish,
        # and stop durable work before closing ports used by a finishing send.
        ingress.close(wait=False)
        if dataplane is not None:
            dataplane.close()
        ingress.close()
        if bound:
            Path(address).unlink(missing_ok=True)
        for port in ports.values():
            port.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--db", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    call = actions.add_parser("call")
    call.add_argument("entry")
    call.add_argument("arg", type=Path, nargs="?")
    call.add_argument("--now", type=int)
    call.add_argument("--socket")
    stream = actions.add_parser("stream")
    stream.add_argument("--details", action="store_true")
    probe = actions.add_parser("probe")
    probe.add_argument("--socket")
    daemon = actions.add_parser("serve")
    daemon.add_argument("--socket", required=True)
    daemon.add_argument("--virtual", action="append", default=[], metavar="LABEL=RX_SOCKET,TX_SOCKET")
    daemon.add_argument("--physical", action="append", default=[], metavar="LABEL=DEVICE")
    daemon.add_argument("--replica", metavar="STANDBY_SOCKET")
    peer = actions.add_parser("peer")
    peer.add_argument("id")
    peer.add_argument("target", metavar="HOST,PORT")
    peer.add_argument("--socket")
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "call" and args.socket:
        body = json.loads(args.arg.read_bytes()) if args.arg else {}
        print(json.dumps(remote_call(args.socket, args.entry, body, timeout=5)))
        return
    if args.action in {"probe", "peer"} and args.socket:
        manifest, _, _ = app(args.reference)
        run_adapter(args, RemoteProcess(args.socket), manifest)
        return
    if not args.db:
        parser.error("--db is required for local execution")
    manifest, entries, digest = app(args.reference)
    with ownership(args.db, exclusive=args.action == "serve"):
        process = Process(entries, args.db, digest)
        if args.action == "serve":
            recovery = manifest["processes"]["recovery"]
            outcome = process.call(recovery["entry"], {}, int(time.time()), allow_empty=True)
            if outcome and outcome["reason"] != recovery["success"]:
                raise DriverError(f"persisted state recovery rejected: {outcome['reason']}")
            try:
                serve(process, manifest, args.socket, args.virtual, args.physical, args.replica)
            except KeyboardInterrupt:
                pass
        elif args.action in {"probe", "peer"}:
            run_adapter(args, process, manifest)
        elif args.action == "stream":
            while (request := read_json(sys.stdin.buffer)) is not None:
                print(json.dumps(process.call(request["entry"], request.get("arg", {}),
                                              request["now"], details=args.details)), flush=True)
        else:
            body = json.loads(args.arg.read_bytes()) if args.arg else {}
            now = int(time.time()) if args.now is None else args.now
            print(json.dumps(process.call(args.entry, body, now)))


def run_adapter(args, process, manifest):
    if args.action == "probe":
        print(json.dumps(probe_endpoints(process, manifest["processes"]["probe"])))
    else:
        try:
            stream_peer(process, manifest["processes"]["peer"], args.id, args.target)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except (DriverError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from error
