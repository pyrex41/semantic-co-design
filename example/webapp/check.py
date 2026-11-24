"""Public browser, HTTP, SMTP and PostgreSQL acceptance against expendable databases."""
import argparse
import asyncio
import base64
import concurrent.futures
import contextlib
import csv
import hashlib
import http.cookiejar
import io
import json
import os
import secrets
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import jwt
import psycopg
from aiosmtpd.controller import Controller
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import sync_playwright
from psycopg import sql
from psycopg.conninfo import make_conninfo


HERE = Path(__file__).resolve().parent
APP = [sys.executable, str(HERE / "appc.py")]
REFERENCE = [*APP, "interpret", str(HERE / "manifest.json")]
WORKFLOW = {"threshold": 10000, "quorum": 1, "above_quorum": 2, "delegation": True, "expires_hours": 72}
MAILBOXES = ["admin", "writer", "north", "south", "west", "auditor"]
EMAILS = {user: f"{user}@approval.test" for user in MAILBOXES}
SUFFIX = secrets.token_hex(4)


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def int_b64(number):
    return b64(number.to_bytes((number.bit_length() + 7) // 8, "big"))


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body="", media="text/html", headers=None):
        body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, body):
        self.reply(status, json.dumps(body), "application/json")

    def do_GET(self):
        path = urlsplit(self.path)
        query = dict(parse_qsl(path.query))
        server = self.server
        if path.path == "/.well-known/openid-configuration":
            self.send_json(200, {"issuer": server.origin, "authorization_endpoint": server.origin + "/authorize",
                                 "token_endpoint": server.origin + "/token", "jwks_uri": server.origin + "/keys"})
        elif path.path == "/keys":
            numbers = server.signer.public_key().public_numbers()
            self.send_json(200, {"keys": [{"kid": "local-test", "alg": "RS256", "kty": "RSA",
                                         "n": int_b64(numbers.n), "e": int_b64(numbers.e)}]})
        elif path.path == "/authorize":
            handle = secrets.token_urlsafe(24)
            server.intents[handle] = query
            options = "".join(f'<option value="{name}">{name}</option>' for name in MAILBOXES)
            self.reply(200, f'<html lang="en"><title>Local test identity</title>'
                            f'<form method="get" action="/consent">'
                            f'<input type="hidden" name="handle" value="{handle}">'
                            f'<label for="user">Test identity</label><select id="user" name="user">{options}</select>'
                            f'<button>Continue</button></form></html>')
        elif path.path == "/consent":
            auth = server.intents.pop(query["handle"])
            user = query["user"]
            assert user in EMAILS and auth["scope"] == "openid email"
            code = secrets.token_urlsafe(34)
            server.codes[code] = user, auth
            self.reply(303, headers={"Location": auth["redirect_uri"] + "?" + urlencode({
                "code": code, "state": auth["state"],
            })})
        else:
            self.reply(404)

    def do_POST(self):
        assert self.path == "/token"
        body = dict(parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        stored = self.server.codes.pop(body["code"], None)
        if stored is None:
            self.send_json(400, {"error": "invalid_grant"})
            return
        user, auth = stored
        verifier = b64(hashlib.sha256(body["code_verifier"].encode()).digest())
        assert auth["code_challenge_method"] == "S256" and verifier == auth["code_challenge"]
        assert body["client_id"] == auth["client_id"] == "approval-check"
        assert body["redirect_uri"] == auth["redirect_uri"]
        stamped = int(time.time())
        token = jwt.encode({
            "iss": self.server.origin, "sub": "test:" + user, "aud": body["client_id"],
            "nonce": auth["nonce"], "iat": stamped, "exp": stamped + 240,
            "email": EMAILS[user], "email_verified": True,
        }, self.server.signer, algorithm="RS256", headers={"kid": "local-test"})
        self.send_json(200, {"id_token": token})


@contextlib.contextmanager
def identity_provider():
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        server.origin = f"http://127.0.0.1:{server.server_port}"
        server.signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server.intents, server.codes = {}, {}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.origin
        finally:
            server.shutdown()
            thread.join()


class MailReceiver:
    def __init__(self):
        self.received = []
        self.drop = True
        self.hold = False
        self.waiting = threading.Event()
        self.release = threading.Event()

    async def handle_DATA(self, server, _session, envelope):
        message = BytesParser(policy=policy.default).parsebytes(envelope.content)
        self.received.append((message["Message-ID"], message["Subject"], envelope.rcpt_tos[0]))
        if self.hold:
            self.hold = False
            self.waiting.set()
            while not self.release.is_set():
                await asyncio.sleep(.025)
        if self.drop and message["Subject"] == "Document review finished" and "/t/cedar/" in message.get_content():
            self.drop = False
            server.transport.close()
            return "451 fixture dropped the connection"
        return "250 accepted"


@contextlib.contextmanager
def smtp_receiver():
    receiver = MailReceiver()
    address = port()
    controller = Controller(receiver, hostname="127.0.0.1", port=address)
    controller.start()
    try:
        yield receiver, address
    finally:
        controller.stop()


@contextlib.contextmanager
def database(base, name):
    named = f"sc_{name}_{SUFFIX}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(named)))
    try:
        yield make_conninfo(base, dbname=named)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(named)))


class Local:
    def __init__(self, commands, db, base, root, idp, smtp_port):
        self.commands, self.db, self.root = commands, db, root
        self.base = base
        self.port = port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
            "SC_DATABASE": db, "SC_ORIGIN": self.origin, "SC_BLOBS": str(root / "blobs"),
            "SC_OIDC_ISSUER": idp, "SC_OIDC_CLIENT": "approval-check", "SC_INSECURE_LOCAL": "1",
            "SC_SMTP_HOST": "127.0.0.1", "SC_SMTP_PORT": str(smtp_port), "SC_SMTP_FROM": "system@approval.test",
            "SC_SMTP_PLAINTEXT_LOCAL": "1", "SC_LOG_LEVEL": "INFO",
            "SC_POLICY": shlex.join(commands("policy"))
        }

    def command(self, name, *args, env=None):
        run = subprocess.run([*self.commands(name), *args], env=env or self.env,
                             capture_output=True, text=True, timeout=135)
        if run.returncode:
            raise AssertionError(f"{name} {args[0] if args else ''} failed: {run.stderr}")
        return json.loads(run.stdout)

    def admin(self, name, params=None, env=None):
        return self.command("admin", name, json.dumps(params or {}), env=env)

    @contextlib.contextmanager
    def serving(self, overrides=None):
        with (self.root / "server.log").open("a") as log:
            process = subprocess.Popen([*self.commands("serve"), "--port", str(self.port)],
                                       env={**self.env, **(overrides or {})}, stdout=log, stderr=log)
            try:
                for _ in range(300):
                    try:
                        with urlopen(self.origin + "/health", timeout=.5) as result:
                            assert json.load(result)["schema"] == 2
                        break
                    except OSError:
                        if process.poll() is not None:
                            raise RuntimeError(f"server exited: {(self.root / 'server.log').read_text()}")
                        time.sleep(.05)
                else:
                    raise RuntimeError(f"server did not start: {(self.root / 'server.log').read_text()}")
                yield process
            finally:
                process.terminate()
                process.wait(timeout=10)

    def query(self, statement, args=()):
        with psycopg.connect(self.db) as connection:
            return connection.execute(statement, args).fetchall()


class Client:
    def __init__(self, app, browser_context=None):
        self.app = app
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.cookies = {}
        if browser_context:
            self.cookies = {field["name"]: field["value"] for field in browser_context.cookies(app.origin)}
        self.csrf = None

    def call(self, path, method="GET", body=None, status=200, key=None, csrf=True):
        headers = {"Accept": "application/json"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            headers["Idempotency-Key"] = key or secrets.token_urlsafe(24)
            if self.csrf and csrf:
                headers["X-CSRF-Token"] = self.csrf
        req = Request(self.app.origin + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = self.opener.open(req, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            if response.status != status:
                raise AssertionError(f"{method} {path}: expected {status}, got {response.status}: {raw[:1000]!r}")
            return json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw

    def activate(self):
        self.csrf = self.call("/api/v1/session")["csrf"]
        return self

    def drop_response(self, path, body, key):
        raw = json.dumps(body).encode()
        with socket.create_connection(("127.0.0.1", self.app.port), timeout=20) as sock:
            req = "\r\n".join([
                "POST " + path + " HTTP/1.1", "Host: 127.0.0.1",
                "Idempotency-Key: " + key, "Content-Type: application/json",
                "Content-Length: " + str(len(raw)), "X-CSRF-Token: " + self.csrf,
                "Cookie: sid=" + self.cookies["sid"], "Connection: close", "", "",
            ])
            sock.sendall(req.encode() + raw)


def sign_in(browser, app, user, keyboard=False):
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.goto(app.origin)
    if keyboard:
        for _ in range(3):
            page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.textContent") == "Sign in"
        page.keyboard.press("Enter")
    else:
        page.get_by_role("link", name="Sign in").click()
    page.get_by_label("Test identity").select_option(user)
    page.get_by_role("button", name="Continue").click()
    assert page.get_by_role("heading", name="Your workspaces").is_visible()
    return context, page, Client(app, context).activate()


def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        time.sleep(.04)
    raise AssertionError("expected committed observation did not appear")


def scenario(app, browser, receiver):
    api = "/api/v1/tenants/cedar"
    oak = "/api/v1/tenants/oak"
    doc_browser = app.origin + "/t/cedar/documents"
    observed = {}
    receiver.drop = True
    messages = len(receiver.received)
    assert app.admin("migrate") == {"schema": 2, "status": "ready"}
    assert app.admin("migrate")["schema"] == 2
    app.admin("provision", {"tenant": "cedar", "name": "Cedar Office", "email": EMAILS["admin"]})
    app.admin("provision", {"tenant": "oak", "name": "Oak Office", "email": EMAILS["admin"]})
    contexts = []
    try:
        with app.serving():
            admin_ctx, admin_page, admin = sign_in(browser, app, "admin", keyboard=True)
            contexts.append(admin_ctx)
            admin_page.get_by_role("link", name="Cedar Office").click()
            admin_page.get_by_role("link", name="Members").click()
            admin_page.get_by_label("Email address").fill(EMAILS["writer"])
            admin_page.locator("form").filter(has=admin_page.get_by_role("button", name="Send invitation")).get_by_label("Role", exact=True).select_option("author")
            admin_page.get_by_role("button", name="Send invitation").click()
            assert admin_page.get_by_text(EMAILS["writer"]).is_visible()
            for user, role in [("north", "reviewer"), ("south", "reviewer"), ("west", "reviewer"), ("auditor", "auditor")]:
                admin.call(api + "/members/invite", "POST", {"email": EMAILS[user], "role": role})
            for user, role in [("writer", "author"), ("north", "reviewer")]:
                admin.call(oak + "/members/invite", "POST", {"email": EMAILS[user], "role": role})
            admin_page.get_by_role("link", name="Workflows").click()
            admin_page.get_by_role("button", name="Publish workflow").click()
            assert admin_page.get_by_text("Version 1 · Active").is_visible(), admin_page.locator("main").inner_text()
            admin.call(oak + "/workflows/publish", "POST", {"workflow": {**WORKFLOW, "delegation": False}}, status=201)
            assert admin.call(api + "/workflows/publish", "POST", {"workflow": {"threshold": -1}},
                              status=422)["error"]["code"] == "invalid-workflow"
            schema = admin.call("/api/v1/openapi.json")
            assert schema["paths"]["/api/v1/tenants/{tenant}/documents"]["post"]["requestBody"]["content"][
                "application/json"]["schema"]["properties"]["amount"]["type"] == "integer"
            observed["api_operations"] = sum(len(methods) for methods in schema["paths"].values())

            users = {}
            pages = {}
            for user in ["writer", "north", "south", "west", "auditor"]:
                context, page, user_client = sign_in(browser, app, user)
                contexts.append(context)
                users[user], pages[user] = user_client, page
            writer = users["writer"]
            author_page = pages["writer"]
            author_page.get_by_role("link", name="Cedar Office").click()
            author_page.get_by_role("link", name="Documents", exact=True).click()
            author_page.get_by_role("link", name="New document").click()
            author_page.get_by_label("Title").fill("Supplier amendment <literal>")
            author_page.get_by_label("Document text").fill("Terms require an independent review. <script>literal</script>")
            author_page.get_by_label("Amount").fill("10001")
            author_page.get_by_role("button", name="Create draft").click()
            assert author_page.get_by_role("heading", name="Supplier amendment <literal>").is_visible()
            assert author_page.locator("script").count() == 0
            doc = author_page.url.rsplit("/", 1)[-1]
            route = api + "/documents/" + doc
            assert writer.call(oak + "/documents/" + doc, status=404)["error"]["code"] == "document-missing"
            assert users["north"].call(route, status=404)["error"]["code"] == "document-missing"
            assert writer.call(route + "/submit", "POST", {"expected_revision": 1}, status=403,
                               csrf=False)["error"]["code"] == "csrf"
            for expected_version in range(1, 7):
                writer.call(route, "PATCH", {"expected_revision": expected_version,
                                             "title": "Supplier amendment <literal>",
                                             "body": f"Document terms revision {expected_version + 1}.", "amount": 10001})
            assert writer.call(route, "PATCH", {"expected_revision": 6, "title": "stale", "body": "stale", "amount": 10001},
                               status=409)["error"]["code"] == "stale-version"
            author_page.reload()
            author_page.get_by_label("Filename").fill("terms.txt")
            author_page.get_by_label("File", exact=True).set_input_files({
                "name": "terms.txt", "mimeType": "text/plain", "buffer": b"browser attachment",
            })
            author_page.get_by_role("button", name="Attach file").click()
            assert author_page.get_by_role("link", name="terms.txt").is_visible()
            data = b"independent uploaded document\x00\x01"
            staged = writer.call(route + "/files", "POST", {"filename": "additional.bin", "expected_revision": 7,
                                 "length": len(data), "digest": hashlib.sha256(data).hexdigest()}, status=201)
            attachment = route + "/files/" + staged["id"]
            assert writer.call(attachment, status=404)["error"]["code"] == "file-missing"
            assert writer.call(attachment + "/complete", "POST", {"expected_revision": 7},
                               status=422)["error"]["code"] == "file-mismatch"
            writer.call(attachment + "/bytes", "PUT", {"expected_revision": 7, "file": base64.b64encode(data).decode()})
            writer.call(attachment + "/complete", "POST", {"expected_revision": 7})
            assert writer.call(attachment) == data
            author_page.get_by_role("button", name="Submit for review").click()
            row = writer.call(route)
            sub = row["submissions"][0]
            assert (sub["revision"], sub["quorum"], sub["policy"]) == (7, 2, 1)
            assert len(row["revisions"]) == 7 and len(row["files"]) == 2
            observed["first_submission"] = [sub["revision"], sub["quorum"], sub["policy"]]
            review_path = api + "/reviews/" + sub["id"]
            roster = [name for name in ["north", "south", "west"] if users[name].call(api + "/inbox")["items"]]
            assert len(roster) == 2

            policy2 = {**WORKFLOW, "above_quorum": 1, "delegation": False}
            assert admin.call(api + "/workflows/publish", "POST", {"workflow": policy2}, status=201)["version"] == 2
            oak_doc = writer.call(oak + "/documents", "POST", {"title": "Oak filing", "body": "Oak terms", "amount": 10000},
                                  status=201)["id"]
            oak_sub = writer.call(oak + "/documents/" + oak_doc + "/submit", "POST", {"expected_revision": 1})
            observed["second_tenant"] = [oak_sub["revision"], oak_sub["quorum"], oak_sub["policy"]]
            assert observed["second_tenant"] == [1, 1, 1]
            oak_review = oak + "/reviews/" + oak_sub["id"] + "/vote"
            oak_vote = {"expected_version": 1, "decision": "approve"}

            def race(key):
                try:
                    return users["north"].call(oak_review, "POST", oak_vote, key=key)["status"]
                except AssertionError as exc:
                    if "got 409" not in str(exc) or "stale-version" not in str(exc):
                        raise
                    return "stale-version"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                race_results = sorted(executor.map(race, ("competing-first", "competing-second")))
            assert race_results == ["approved", "stale-version"]
            assert app.query("SELECT count(*) FROM delivery WHERE tenant='oak' AND kind='terminal'") == [(1,)]
            observed["competing_final_votes"] = race_results

            first, final = (users[name] for name in roster)
            first_page = pages[roster[0]]
            first_page.get_by_role("link", name="Cedar Office").click()
            first_page.get_by_role("link", name="Review inbox", exact=True).click()
            first_page.get_by_role("link", name="Supplier amendment <literal>").click()
            assert first_page.get_by_text("Workflow 1 · 2 required approval(s)", exact=False).is_visible()
            assert first_page.get_by_role("link", name="terms.txt").is_visible()
            first_page.get_by_label("Decision", exact=True).select_option("approve")
            first_page.get_by_role("button", name="Record decision").click()
            assert final.call(review_path)["version"] == 2
            assert final.call(review_path + "/vote", "POST", {"expected_version": 1, "decision": "approve"},
                              status=409)["error"]["code"] == "stale-version"

            lost_key = "response-lost-" + SUFFIX
            vote = {"expected_version": 2, "decision": "approve"}
            pending = {"expected_version": 2, "decision": "reject"}
            final.drop_response(review_path + "/vote", vote, lost_key)
            wait_for(lambda: app.query("SELECT status FROM submission WHERE tenant='cedar' AND id=%s", (sub["id"],)) == [
                ("approved",),
            ])
            assert app.query("SELECT count(*) FROM command_receipt WHERE command='vote' AND key=%s", (lost_key,)).pop()[0] == 1
            receipt = final.call(review_path + "/vote", "POST", vote, key=lost_key)
            assert final.call(review_path + "/vote", "POST", pending, status=409, key=lost_key)["error"]["code"] == "idempotency-conflict"
            assert receipt["status"] == "approved" and receipt["version"] == 3 and receipt["votes"] == 2
            observed["terminal"] = [receipt["status"], receipt["version"], receipt["votes"]]
            assert app.query("SELECT count(*) FROM vote WHERE tenant='cedar' AND submission=%s", (sub["id"],)) == [(2,)]
            assert app.query("SELECT count(*) FROM audit WHERE tenant='cedar' AND event='submission.closed' AND object=%s",
                             (sub["id"],)) == [(1,)]
            assert app.query("SELECT count(*) FROM delivery WHERE tenant='cedar' AND kind='terminal'") == [(1,)]

            actor = final.call("/api/v1/session")["actor"]
            admin.call(api + "/members/" + actor + "/role", "PUT", {"role": "revoked"})
            observed["revoked"] = final.call(review_path + "/vote", "POST", vote, status=404, key=lost_key)["error"]["code"]
            admin.call(api + "/members/invite", "POST", {"email": EMAILS[roster[1]], "role": "reviewer"})
            restored_ctx, restored_page, restored = sign_in(browser, app, roster[1])
            contexts.append(restored_ctx)
            users[roster[1]], pages[roster[1]] = restored, restored_page

            next_doc = writer.call(api + "/documents", "POST", {"title": "Second policy", "body": "For review", "amount": 10001},
                                   status=201)["id"]
            next_route = api + "/documents/" + next_doc
            next_sub = writer.call(next_route + "/submit", "POST", {"expected_revision": 1})
            assert next_sub["policy"] == 2 and next_sub["quorum"] == 1
            next_review = api + "/reviews/" + next_sub["id"]
            holder = next(name for name in ["north", "south", "west"]
                          if any(row["id"] == next_sub["id"] for row in users[name].call(api + "/inbox")["items"]))
            spare = next(name for name in ["north", "south", "west"] if name != holder)
            assert users[holder].call(next_review + "/delegate", "POST",
                {"expected_version": 1, "email": EMAILS[spare]}, status=403)["error"]["code"] == "delegation-denied"
            assert users[holder].call(next_review + "/vote", "POST", {"expected_version": 1, "decision": "reject"})["status"] == "rejected"
            assert writer.call(next_route + "/submit", "POST", {"expected_revision": 1}, status=409)["error"]["code"] == "not-draft"
            writer.call(next_route, "PATCH", {"expected_revision": 1, "title": "Revised", "body": "Fixed", "amount": 0})
            assert users[holder].call(next_route, status=404)["error"]["code"] == "document-missing"
            assert users[holder].call(next_review)["revision"] == 1
            assert writer.call(next_route + "/submit", "POST", {"expected_revision": 2})["revision"] == 2
            delete_doc = writer.call(api + "/documents", "POST", {"title": "Temporary", "body": "Test delete", "amount": 0},
                                     status=201)["id"]
            writer.call(api + "/documents/" + delete_doc, "DELETE", {"expected_revision": 1})
            writer.call(api + "/documents/" + delete_doc, status=404)

            audit = users["auditor"].call(api + "/audit/export")
            entries = list(csv.DictReader(io.StringIO(audit.decode())))
            assert len(entries) == app.query("SELECT count(*) FROM audit WHERE tenant='cedar'")[0][0]
            assert all(int(a["id"]) < int(b["id"]) for a, b in zip(entries, entries[1:]))
            assert "vote.recorded" in {entry["event"] for entry in entries}
            users["north"].call(api + "/audit/export", status=403)
            pages["auditor"].get_by_role("link", name="Cedar Office").click()
            pages["auditor"].get_by_role("link", name="Audit history").click()
            with pages["auditor"].expect_download() as download:
                pages["auditor"].get_by_role("link", name="Download CSV").click()
            assert download.value.suggested_filename == "audit.csv"

            for _ in range(45):
                result = app.command("worker", "--once")
                if not result["worked"]:
                    break
            else:
                raise AssertionError("deliveries did not drain")
            unknown = app.query("SELECT id FROM delivery WHERE tenant='cedar' AND status='unknown'")
            assert len(unknown) == 1
            delivery_id, = unknown[0]
            admin.call(api + "/deliveries/" + delivery_id + "/retry", "POST", {})
            assert app.command("worker", "--once")["status"] == "accepted"
            matched = [entry for entry in receiver.received[messages:] if entry[0].startswith("<" + delivery_id + "@")]
            assert len(matched) == 2 and matched[0] == matched[1]
            observed["smtp_same_message_id"] = True

            snapshot = app.root / "backup"
            assert app.admin("backup", {"path": str(snapshot)})["status"] == "backed-up"
            with database(app.base, "restore") as db:
                restored_blobs = app.root / "restored"
                env = {**app.env, "SC_DATABASE": db, "SC_BLOBS": str(restored_blobs)}
                assert app.admin("restore", {"path": str(snapshot)}, env=env)["status"] == "restored"
                with psycopg.connect(db) as recovered:
                    assert recovered.execute("SELECT count(*) FROM audit").fetchone() == app.query("SELECT count(*) FROM audit")[0]
                    assert recovered.execute("SELECT count(*) FROM submission WHERE status='pending'").fetchone() == app.query(
                        "SELECT count(*) FROM submission WHERE status='pending'")[0]
                    assert recovered.execute("SELECT count(*) FROM delivery").fetchone() == app.query("SELECT count(*) FROM delivery")[0]
                assert (restored_blobs / staged["id"]).read_bytes() == data
                observed["restore"] = "audit, pending submissions, deliveries and blob bytes"

        with app.serving({"SC_POLICY": "/unavailable/tenant-compiler"}):
            assert admin.call(api + "/workflows/publish", "POST", {"workflow": WORKFLOW},
                              status=503)["error"]["code"] == "compiler-unavailable"
            assert admin.call(api + "/workflows")["items"][-1]["active"]
            assert writer.call(route)["status"] == "approved"
            assert writer.call(attachment) == data
            observed["compiler_offline"] = True
        return observed
    finally:
        for context in contexts:
            context.close()


def frozen_change(release, root, base, browser, idp, smtp_port):
    """Remove the new rule and field in the baseline while freezing the compiler and drivers."""
    baseline_source = root / "baseline-source"
    baseline_source.mkdir()
    manifest = json.loads((HERE / "manifest.json").read_text())
    for name in [*manifest["sources"], *manifest["assets"]]:
        shutil.copyfile(HERE / name, baseline_source / name)
    for route in manifest["routes"]:
        route.get("fields", {}).pop("amount", None)
    (baseline_source / "manifest.json").write_text(json.dumps(manifest))
    rules = baseline_source / "documents.shen"
    text = rules.read_text()
    condition = '(if (> Amount (get Policy "threshold")) (get Policy "above_quorum") (get Policy "quorum"))'
    assert text.count(condition) == 1
    text = text.replace(condition, '(get Policy "quorum")').replace('(get Body "amount")', "0")
    rules.write_text(text)
    baseline = root / "baseline-release"
    subprocess.run([*APP, "build", str(baseline_source / "manifest.json"), "--out", str(baseline)],
                   check=True, timeout=220)
    digests = [json.loads((item / "ownership.json").read_text())["digests"] for item in [baseline, release]]
    assert all(digests[0][field] == digests[1][field] for field in ("compiler", "drivers", "compiler_dependencies"))
    assert digests[0]["manifest"] != digests[1]["manifest"] and digests[0]["native"] != digests[1]["native"]
    api = "/api/v1/tenants/cedar"
    output = {}
    for name, package, amount_present in [("before", baseline, False), ("after", release, True)]:
        def commands(command):
            return [sys.executable, str(package / command)]
        folder = root / ("mutation-" + name)
        folder.mkdir()
        with database(base, "mutation_" + name) as db:
            app = Local(commands, db, base, folder, idp, smtp_port)
            app.admin("migrate")
            app.admin("provision", {"tenant": "cedar", "name": "Cedar Office", "email": EMAILS["admin"]})
            with app.serving(), contextlib.ExitStack() as stack:
                admin_context, _, admin = sign_in(browser, app, "admin")
                stack.callback(admin_context.close)
                admin.call(api + "/members/invite", "POST", {"email": EMAILS["writer"], "role": "author"})
                for reviewer in ["north", "south"]:
                    admin.call(api + "/members/invite", "POST", {"email": EMAILS[reviewer], "role": "reviewer"})
                    context, _, _ = sign_in(browser, app, reviewer)
                    stack.callback(context.close)
                admin.call(api + "/workflows/publish", "POST", {"workflow": WORKFLOW}, status=201)
                context, page, writer = sign_in(browser, app, "writer")
                stack.callback(context.close)
                page.goto(app.origin + "/t/cedar/documents/new")
                assert (page.get_by_label("Amount", exact=True).count() == 1) == amount_present
                schema = writer.call("/api/v1/openapi.json")
                fields = schema["paths"]["/api/v1/tenants/{tenant}/documents"]["post"]["requestBody"]["content"][
                    "application/json"]["schema"]
                assert ("amount" in fields["properties"]) == amount_present
                if amount_present:
                    writer.call(api + "/documents", "POST", {"title": "Invalid", "body": "Invalid", "amount": -1}, status=422)
                # Test the browser form independently of the API client.
                page.get_by_label("Title").fill("Upper bound")
                page.get_by_label("Document text").fill("Terms at 10001")
                if amount_present:
                    page.get_by_label("Amount", exact=True).fill("10001")
                page.get_by_role("button", name="Create draft").click()
                upper = page.url.rsplit("/", 1)[-1]
                lower_body = {"title": "At threshold", "body": "Terms at 10000"}
                if amount_present:
                    lower_body["amount"] = 10000
                lower = writer.call(api + "/documents", "POST", lower_body, status=201)["id"]
                quorums = []
                for document, expected in [(lower, 1), (upper, 2 if amount_present else 1)]:
                    result = writer.call(api + "/documents/" + document + "/submit", "POST", {"expected_revision": 1})
                    identifier = result["id"]
                    assert result["quorum"] == expected
                    assert app.query("SELECT quorum FROM submission WHERE tenant='cedar' AND id=%s", [identifier]) == [(expected,)]
                    assert app.query("SELECT (detail->>'quorum')::int FROM audit WHERE tenant='cedar' AND object=%s AND event='submission.created'", [identifier]) == [(expected,)]
                    assert app.query("SELECT count(*) FROM delivery WHERE tenant='cedar' AND kind='review' AND strpos(body,%s)>0", [identifier]) == [(expected,)]
                    quorums.append(expected)
                output[name] = {"browser_amount_field": amount_present, "quorums": quorums}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="conninfo for a test account allowed to create databases")
    parser.add_argument("--mode", choices=["reference", "native", "both"], default="both")
    parser.add_argument("--release", type=Path)
    parser.add_argument("--chromium", type=Path, help="path to an already-installed Chromium test browser")
    parser.add_argument("--frozen-change", action="store_true", help="run the source-only field and quorum comparison")
    args = parser.parse_args()
    if args.frozen_change and args.mode == "reference":
        parser.error("--frozen-change requires the generated package")
    with tempfile.TemporaryDirectory(prefix="approval-check-") as temp, identity_provider() as idp, smtp_receiver() as (receiver, smtp_port):
        root = Path(temp)
        results = {}
        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path=args.chromium)
            for mode in (["reference", "native"] if args.mode == "both" else [args.mode]):
                if mode == "reference":
                    def commands(command):
                        return [*REFERENCE, command]
                else:
                    release = args.release.resolve() if args.release else root / "release"
                    if args.release is None:
                        subprocess.run([*APP, "build", str(HERE / "manifest.json"), "--out", str(release)],
                                       check=True, timeout=220)

                    def commands(command):
                        return [sys.executable, str(release / command)]
                directory = root / mode
                directory.mkdir()
                with database(args.database, mode) as db:
                    app = Local(commands, db, args.database, directory, idp, smtp_port)
                    try:
                        results[mode] = scenario(app, browser, receiver)
                    except Exception:
                        log = directory / "server.log"
                        if log.exists():
                            print(log.read_text(), file=sys.stderr)
                        raise
            if args.frozen_change:
                results["frozen_change"] = frozen_change(release, root, args.database, browser, idp, smtp_port)
            browser.close()
        if args.mode == "both":
            assert results["reference"] == results["native"], results
        print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
