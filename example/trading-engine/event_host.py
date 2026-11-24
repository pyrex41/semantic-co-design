"""Generic durable JSON entry host. All application decisions live in compiled entries."""

import argparse
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import sqlite3
import sys
import time
from urllib.parse import unquote, urlsplit

import transport
from values import Problem, canonical, digest


MAX_REQUEST = 2 * 1024 * 1024
MAX_STATE = 32 * 1024 * 1024
RECURSION_LIMIT = 20_000


class HostError(Exception):
    pass


def fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON member")
        result[name] = value
    return result


def decode(raw, limit=MAX_REQUEST):
    if len(raw) > limit:
        raise ValueError("input exceeds request size bound")
    value = json.loads(raw, object_pairs_hook=fields)
    canonical(value)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def read(path, limit=MAX_REQUEST):
    if path == "-":
        return decode(sys.stdin.buffer.read(limit + 1), limit)
    with Path(path).open("rb") as source:
        return decode(source.read(limit + 1), limit)


def timestamp():
    return time.time_ns() // 1_000_000


def release_id(record):
    hashes = record["digests"]
    return digest({key: hashes[key] for key in ("manifest", "source", "compiler", "drivers", "assets",
                                                "compiler_dependencies")}
                  | {"python-abi": record["abi"]["python"]})


def external_entries(root, release):
    def invoke(name):
        def deliver(_, request):
            import subprocess

            completed = subprocess.run([str(root / "engine"), "evaluate", name], input=canonical(request),
                                       stdout=subprocess.PIPE, text=True, check=True, timeout=20)
            return json.loads(completed.stdout)
        return deliver

    return {name: invoke(name) for name in release["entries"]}


class Store:
    def __init__(self, file, manifest, entries, release):
        self.file, self.manifest, self.entries, self.release = file, manifest, entries, release
        os.umask(0o077)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshot(
                  slot INTEGER PRIMARY KEY CHECK(slot = 1),
                  seq INTEGER NOT NULL,
                  release TEXT NOT NULL,
                  document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_log(
                  seq INTEGER PRIMARY KEY,
                  entry TEXT NOT NULL,
                  release TEXT NOT NULL,
                  request TEXT NOT NULL,
                  response TEXT NOT NULL,
                  state_digest TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.file, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous = FULL")
            yield db
        finally:
            db.close()

    def permit(self, entry, roles):
        if entry not in self.entries:
            raise HostError("unknown entry")
        if not set(self.manifest["access"][entry]["roles"]).intersection(roles):
            raise PermissionError("entry permission denied")

    def invoke(self, entry, event, *, time_ms, fence, actor, roles, fault=None):
        self.permit(entry, roles)
        read_only = self.manifest["access"][entry].get("read-only", False)
        if not isinstance(event, dict):
            raise ValueError("event must be a JSON object")
        with self.connection() as db:
            db.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            row = db.execute("SELECT seq, document, release FROM snapshot WHERE slot=1").fetchone()
            previous = None if row is None else json.loads(row[1])
            active = self.release if row is None else row[2]
            activation = entry == self.manifest["activation"]
            if active != self.release and not activation:
                raise HostError("activate this release before opening the existing state")
            request = {"event": event, "time": time_ms, "fence": fence, "actor": actor,
                       "release": self.release, "previous-release": active}
            reply = self.entries[entry](None, {**request, "state": previous})
            data = {"status": reply["status"], "data": reply["data"]}
            if active != self.release and reply["status"] != self.manifest["activation-status"]:
                db.rollback()
                return data
            if not read_only:
                self.save(db, entry, request, reply["state"], data, 1 if row is None else row[0] + 1)
            if fault is not None:
                fault("before-commit")
            db.commit()
            if fault is not None:
                fault("after-commit")
            return data

    def send(self, event, signing, *, fence, actor, roles, effect=transport.http, fault=None, clock=timestamp):
        """Persist a claim before I/O. Hold the writer lock through the bounded send."""
        rules = self.manifest["transport"]
        for name in ("claim", "authorize", "result"):
            self.permit(rules[name], roles)
        claim = self.invoke(rules["claim"], event, time_ms=clock(), fence=fence, actor=actor, roles=roles)
        if claim["status"] != rules["ready-status"]:
            return claim
        if fault is not None:
            fault("after-claim")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            seq, encoded, active = db.execute("SELECT seq, document, release FROM snapshot WHERE slot=1").fetchone()
            if active != self.release:
                raise HostError("release changed after the send claim")
            state = json.loads(encoded)
            request = {"event": claim["data"], "time": clock(), "fence": fence,
                       "actor": actor, "release": self.release}
            verdict = self.entries[rules["authorize"]](None, {**request, "state": state})
            decision = {"status": verdict["status"], "data": verdict["data"]}
            self.save(db, rules["authorize"], request, verdict["state"], decision, seq + 1)
            if verdict["status"] == rules["authorized-status"]:
                response = effect(claim["data"]["request"], signing)
                if fault is not None:
                    fault("after-effect")
                observed = {**request, "event": {**claim["data"], "response": response}, "time": clock()}
                result = self.entries[rules["result"]](None, {**observed, "state": verdict["state"]})
                decision = {"status": result["status"], "data": result["data"]}
                self.save(db, rules["result"], observed, result["state"], decision, seq + 2)
            db.commit()
        return decision

    def fetch(self, event, signing, *, fence, actor, roles, effect=transport.http):
        result = self.invoke(self.manifest["wire-entry"], event, time_ms=timestamp(),
                             fence=fence, actor=actor, roles=roles)
        if result["status"] != "wire-request":
            return result
        request = result["data"]
        if request["method"] != "GET":
            raise HostError("fetch requires a generated GET request")
        return effect(request, signing)

    def stream(self, signing, limit, *, fence, actor, roles, frames=transport.frames, rest=transport.http):
        rules = self.manifest["stream"]
        if not 1 <= limit <= 100_000:
            raise ValueError("frame limit outside supported range")

        def invoke(entry, event):
            return self.invoke(entry, event, time_ms=timestamp(), fence=fence, actor=actor, roles=roles)

        result = invoke(self.manifest["wire-entry"], rules["subscribe"])
        if result["status"] != "wire-request":
            return result
        count = 0
        try:
            for frame in frames(result["data"], signing, limit):
                count += 1
                clock_field = rules["clock-field"]
                stamp = transport.epoch_millis(frame.get(clock_field)) if isinstance(frame, dict) else None
                result = invoke(rules["entry"], {"body": frame, "wire-time": stamp, "live": True})
                if result["status"] in rules["stop-status"]:
                    break
                if result["status"] in rules["rest-status"]:
                    for request in result["data"]:
                        response = self.fetch(request, signing, fence=fence, actor=actor, roles=roles, effect=rest)
                        body = response.get("body")
                        stamp = transport.epoch_millis(body.get(clock_field)) if isinstance(body, dict) else None
                        invoke(rules["rest"], {**request, "response": response, "wire-time": stamp, "live": True})
        finally:
            invoke(rules["disconnect"], {})
        return {"frames": count, "status": result["status"], "data": result["data"]}

    def save(self, db, entry, request, state, data, seq):
        encoded = canonical(state)
        data_bytes = encoded.encode()
        if len(data_bytes) > MAX_STATE:
            raise HostError("durable state exceeds the configured size bound")
        db.execute("INSERT INTO event_log VALUES (?, ?, ?, ?, ?, ?)",
                   (seq, entry, self.release, canonical(request), canonical(data), digest(data_bytes)))
        db.execute("INSERT INTO snapshot VALUES (1, ?, ?, ?) ON CONFLICT(slot) DO UPDATE SET "
                   "seq=excluded.seq, release=excluded.release, document=excluded.document", (seq, self.release, encoded))

    def replay(self, release_entries):
        prior, count = None, 0
        with self.connection() as db:
            db.execute("BEGIN")
            for seq, entry, release, request, response, expected in db.execute(
                    "SELECT seq, entry, release, request, response, state_digest FROM event_log ORDER BY seq"):
                if release not in release_entries:
                    raise HostError("missing pinned release " + release)
                result = release_entries[release][entry](None, {**json.loads(request), "state": prior})
                if expected != digest(result["state"]) or json.loads(response) != {"status": result["status"], "data": result["data"]}:
                    raise HostError(f"replay diverged at event {seq}")
                prior, count = result["state"], seq
            saved = db.execute("SELECT seq, document FROM snapshot WHERE slot=1").fetchone()
            if saved and (saved[0] != count or json.loads(saved[1]) != prior):
                raise HostError("snapshot differs from replay")
        return {"events": count, "state_digest": digest(prior)}

    def backup(self, filename):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(filename, flags, 0o600)
        os.close(fd)
        with self.connection() as db:
            copy = sqlite3.connect(filename)
            try:
                db.backup(copy)
            finally:
                copy.close()


def api(store, address, credentials):
    tokenfile = Path(credentials)
    if tokenfile.stat().st_mode & 0o077:
        raise HostError("API credential file must be owner-readable only")
    principals = read(str(tokenfile))
    if not all(isinstance(token, str) and len(token) >= 16 and isinstance(value, dict)
               and isinstance(value.get("actor"), str) and isinstance(value.get("roles"), list)
               for token, value in principals.items()):
        raise HostError("invalid API principal file")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_):
            return

        def send(self, status, data):
            raw = canonical(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def principal(self):
            scheme, _, value = self.headers.get("Authorization", "").partition(" ")
            if scheme != "Bearer" or len(value) > 1024:
                raise PermissionError("credential required")
            selected = None
            for token, record in principals.items():
                if hmac.compare_digest(token.encode(), value.encode()):
                    selected = record
            if selected is None:
                raise PermissionError("credential invalid")
            return selected

        def do_POST(self):
            path = urlsplit(self.path)
            if not path.path.startswith("/v1/") or path.query:
                self.send(404, {"error": "unknown path"})
                return
            try:
                actor = self.principal()
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_REQUEST:
                    raise ValueError("request exceeds size bound")
                data = decode(self.rfile.read(length))
                result = store.invoke(unquote(path.path[4:]), data.get("event", {}), time_ms=timestamp(),
                                      fence=data.get("fence"), actor=actor["actor"], roles=actor["roles"])
                self.send(200, result)
            except PermissionError as exc:
                self.send(403 if self.headers.get("Authorization") else 401, {"error": str(exc)})
            except HostError as exc:
                self.send(404, {"error": str(exc)})
            except (Problem, ValueError, TypeError, sqlite3.Error, RecursionError):
                self.send(400, {"error": "invalid or unavailable event"})

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    with Server(address, Handler) as server:
        print(canonical({"address": server.server_address[0], "port": server.server_address[1]}), flush=True)
        server.serve_forever()


def main(manifest, entries, release, argv=None):
    sys.setrecursionlimit(RECURSION_LIMIT)
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "evaluate" and argv[1] in entries:
        request = read("-", MAX_STATE + MAX_REQUEST)
        print(canonical(entries[argv[1]](None, request)))
        return
    parser = argparse.ArgumentParser(description="Run a generated durable application")
    parser.add_argument("--database", required=True)
    commands = parser.add_subparsers(dest="mode", required=True)
    call = commands.add_parser("call")
    call.add_argument("entry", choices=entries)
    call.add_argument("--input", default="-")
    call.add_argument("--fence", type=int)
    for mode in ("fetch", "send"):
        network = commands.add_parser(mode)
        network.add_argument("--input", default="-")
        network.add_argument("--fence", type=int)
        network.add_argument("--signing-file")
    feed = commands.add_parser("stream")
    feed.add_argument("--fence", type=int, required=True)
    feed.add_argument("--signing-file", required=True)
    feed.add_argument("--frames", type=int, default=10000)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--credentials", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--pinned", action="append", default=[], type=Path)
    backup = commands.add_parser("backup")
    backup.add_argument("output")
    args = parser.parse_args(argv)
    store = Store(args.database, manifest, entries, release)
    roles = {role for value in manifest["access"].values() for role in value["roles"]}
    actor = pwd.getpwuid(os.geteuid()).pw_name
    try:
        if args.mode == "call":
            result = store.invoke(args.entry, read(args.input), time_ms=timestamp(), fence=args.fence,
                                  actor=actor, roles=roles)
        elif args.mode in ("fetch", "send"):
            action = getattr(store, args.mode)
            result = action(read(args.input), transport.credential(args.signing_file),
                            fence=args.fence, actor=actor, roles=roles)
        elif args.mode == "stream":
            result = store.stream(transport.credential(args.signing_file), args.frames,
                                  fence=args.fence, actor=actor, roles=roles)
        elif args.mode == "replay":
            pinned = {release: entries}
            for root in args.pinned:
                root = root.resolve()
                record = read(str(root / "ownership.json"))
                pinned[release_id(record)] = external_entries(root, record)
            result = store.replay(pinned)
        elif args.mode == "backup":
            store.backup(args.output)
            result = {"backup": args.output}
        else:
            api(store, ("127.0.0.1", args.port), args.credentials)
            return
        print(canonical(result))
    except (ValueError, OSError, HostError, sqlite3.Error, Problem, TypeError, RecursionError, ImportError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    from _application import entries

    library = Path(__file__).resolve().parent
    ownership = json.loads((library.parent / "ownership.json").read_text())
    main(json.loads((library / "manifest.json").read_text()), entries, release_id(ownership))
