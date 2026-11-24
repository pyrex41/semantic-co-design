#!/usr/bin/env python3
"""Bind a generated application to persistent records and local Ethernet I/O."""

import argparse
import fcntl
from contextlib import closing
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
import time


MAX_COMMAND = 2 * 1024 * 1024
MAX_FRAME = 9218


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

    def call(self, command, body, now, *, details=False, allow_empty=False):
        entry = self.entries.get(command)
        if entry is None:
            raise DriverError("unknown entry")
        if not isinstance(body, dict):
            raise DriverError("arguments must be an object")
        if type(now) is not int or not 0 <= now < 2**63 - 65536:
            raise DriverError("clock outside the supported range")
        with closing(self.connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM application_state WHERE singleton = 1").fetchone()
            previous = row[0] if row else None
            state = json.loads(previous) if previous else None
            if state is None and command != "boot":
                if allow_empty:
                    return None
                raise DriverError("the application has no committed configuration")
            if state is not None and command == "boot":
                raise DriverError("the application is already configured")
            result = entry(None, {**body, "now": now, "state": state})
            next_state = result.get("state")
            if next_state is not None:
                serialized = wire(next_state).decode()
                if serialized != previous:
                    conn.execute("""
                        INSERT INTO application_state VALUES (1, ?, ?)
                        ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload, release_digest=excluded.release_digest
                        """, (serialized, self.digest))
            conn.commit()
        return result if details else {name: value for name, value in result.items() if name != "state"}


class VirtualPort:
    def __init__(self, endpoints):
        self.local, self.remote = endpoints.split(",", 1)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.sock.bind(self.local)
            os.chmod(self.local, 0o600)
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
        except BaseException:
            self.sock.close()
            raise

    def receive(self):
        return self.sock.recv(MAX_FRAME + 1)

    def send(self, frame):
        self.sock.send(frame)

    def close(self):
        self.sock.close()


def process_request(process, operations, connection):
    connection.settimeout(2)
    with connection.makefile("rb") as request:
        message = read_json(request)
        if not isinstance(message, dict) or message.get("entry") not in operations:
            raise DriverError("unavailable management operation")
        return process.call(operations[message["entry"]], message.get("arg", {}), int(time.time()))


def remote_call(address, operation, arg, timeout=2):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(address)
        client.sendall(wire({"entry": operation, "arg": arg}))
        with client.makefile("rb") as response:
            return read_json(response)


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
    bound = False
    try:
        management.bind(address)
        bound = True
        os.chmod(address, 0o600)
        management.listen(16)
        selector.register(management, selectors.EVENT_READ)
        for label, name, kind in bindings:
            port = ports[label] = kind(name)
            selector.register(port.sock, selectors.EVENT_READ, label)
        profiles = manifest["processes"]
        packet = profiles["packet"]
        replication = profiles["replication"]
        while True:
            for key, _ in selector.select(timeout=1):
                label = key.data
                if label is None:
                    connection, _ = management.accept()
                    with connection:
                        try:
                            response = process_request(process, profiles["management"], connection)
                        except (ValueError, OSError, sqlite3.Error) as exc:
                            response = {"error": str(exc)}
                        try:
                            connection.sendall(wire(response))
                        except OSError:
                            pass
                    continue
                raw = ports[label].receive()
                try:
                    result = process.call(packet["entry"], {
                        packet["interface"]: label, packet["request-bytes"]: list(raw),
                    }, int(time.time()))
                except DriverError:
                    continue
                if replica and replication["offer"] in result:
                    try:
                        response = remote_call(replica, replication["operation"], {
                            replication["arg"]: result[replication["offer"]],
                        })
                        if response and replication["ack"] in response:
                            result = process.call(replication["success"], {
                                replication["ack"]: response[replication["ack"]],
                            }, int(time.time()))
                    except (OSError, ValueError) as exc:
                        print("replication:", exc, file=sys.stderr)
                for outgoing in result.get(packet["response-bytes"], []):
                    try:
                        ports[outgoing[packet["interface"]]].send(bytes(outgoing[packet["request-bytes"]]))
                    except (OSError, KeyError) as exc:
                        print("transmit:", exc, file=sys.stderr)
    finally:
        selector.close()
        management.close()
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
    actions.add_parser("probe")
    daemon = actions.add_parser("serve")
    daemon.add_argument("--socket", required=True)
    daemon.add_argument("--virtual", action="append", default=[], metavar="LABEL=RX_SOCKET,TX_SOCKET")
    daemon.add_argument("--physical", action="append", default=[], metavar="LABEL=DEVICE")
    daemon.add_argument("--replica", metavar="STANDBY_SOCKET")
    peer = actions.add_parser("peer")
    peer.add_argument("id")
    peer.add_argument("target", metavar="HOST,PORT")
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "call" and args.socket:
        body = json.loads(args.arg.read_bytes()) if args.arg else {}
        print(json.dumps(remote_call(args.socket, args.entry, body, timeout=5)))
        return
    if not args.db:
        parser.error("--db is required for local execution")
    manifest, entries, digest = app(args.reference)
    process = Process(entries, args.db, digest)
    if args.action == "serve":
        with open(str(args.db) + ".port-owner", "a") as ownership:
            try:
                fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DriverError("another process owns this dataplane state") from exc
            recovery = manifest["processes"]["recovery"]
            outcome = process.call(recovery["entry"], {}, int(time.time()), allow_empty=True)
            if outcome and outcome["reason"] != recovery["success"]:
                raise DriverError("the persisted state cannot be recovered")
            try:
                serve(process, manifest, args.socket, args.virtual, args.physical, args.replica)
            except KeyboardInterrupt:
                pass
    elif args.action == "probe":
        print(json.dumps(probe_endpoints(process, manifest["processes"]["probe"])))
    elif args.action == "peer":
        try:
            stream_peer(process, manifest["processes"]["peer"], args.id, args.target)
        except KeyboardInterrupt:
            pass
    elif args.action == "stream":
        while (request := read_json(sys.stdin.buffer)) is not None:
            print(json.dumps(process.call(request["entry"], request.get("arg", {}),
                                          request["now"], details=args.details)), flush=True)
    else:
        body = json.loads(args.arg.read_bytes()) if args.arg else {}
        now = int(time.time()) if args.now is None else args.now
        print(json.dumps(process.call(args.entry, body, now)))


if __name__ == "__main__":
    try:
        main()
    except (DriverError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from error
