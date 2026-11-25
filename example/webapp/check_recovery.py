"""Exercise delegated reviews, timers, object retention, and pending deliveries on restore."""
import argparse
import base64
import contextlib
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from check import Client, EMAILS, Local, REFERENCE, WORKFLOW, database, identity_provider, sign_in, smtp_receiver


TENANT = "/api/v1/tenants/rescue"


def exercise(app, browser, idp, smtp_port, mail):
    app.admin("migrate")
    app.admin("provision", {"tenant": "rescue", "name": "Recovery", "email": EMAILS["admin"]})
    with app.serving(), contextlib.ExitStack() as contexts:
        ctx, _, admin = sign_in(browser, app, "admin")
        contexts.callback(ctx.close)
        users = {}
        for user, role in [("writer", "author"), ("north", "reviewer"), ("south", "reviewer")]:
            admin.call(TENANT + "/members/invite", "POST", {"email": EMAILS[user], "role": role})
            ctx, _, users[user] = sign_in(browser, app, user)
            contexts.callback(ctx.close)
        admin.call(TENANT + "/workflows/publish", "POST", {"workflow": WORKFLOW}, status=201)
        writer = users["writer"]
        draft = writer.call(TENANT + "/documents", "POST",
                            {"title": "Recovery", "body": "Pending decision", "amount": 0}, status=201)
        route = TENANT + "/documents/" + draft["id"]
        body = b"file retention evidence"
        unfinished = upload(writer, route, body, finish=False)
        preserved = upload(writer, route, body, finish=True)
        submission = writer.call(route + "/submit", "POST", {"expected_revision": 1})
        original = next(name for name in ["north", "south"] if users[name].call(TENANT + "/inbox")["items"])
        receiver = "north" if original == "south" else "south"
        review = TENANT + "/reviews/" + submission["id"]
        assert users[original].call(review + "/delegate", "POST", {
            "expected_version": 1, "email": EMAILS[receiver],
        })["status"] == "delegated"
        assert users[original].call(review, status=404)["error"]["code"] == "review-missing"
        assert users[receiver].call(review)["version"] == 2

        # Move deadlines in the disposable database so the scheduler can run immediately.
        app.query("UPDATE attachment SET expires_at=now()-interval '1 second' WHERE tenant='rescue' RETURNING id")
        app.query("UPDATE submission SET due_at=now()-interval '1 second' WHERE tenant='rescue' RETURNING id")
        app.command("worker", "--once")
        app.command("worker", "--once")
        assert users[receiver].call(review)["status"] == "expired"
        assert writer.call(route)["status"] == "rejected"
        assert app.query("SELECT count(*) FROM delivery WHERE tenant='rescue' AND kind='terminal'") == [(1,)]
        assert not (app.root / "blobs" / unfinished).exists()
        assert writer.call(route + "/files/" + preserved) == body

        pending_query = "SELECT count(*) FROM delivery WHERE tenant='rescue' AND status='pending'"
        pending = app.query(pending_query)
        assert pending[0][0] > 0
        snapshot = app.root / "backup"
        app.admin("backup", {"path": str(snapshot)})
        with database(app.base, "recovery_restore") as target:
            restored = Local(app.commands, target, app.base, app.root, idp, smtp_port)
            restored.env["SC_BLOBS"] = str(app.root / "recovered")
            restored.admin("restore", {"path": str(snapshot)})
            assert restored.query(pending_query) == pending
            assert (app.root / "recovered" / preserved).read_bytes() == body
            interrupt_delivery(restored, admin, mail)


def interrupt_delivery(app, admin, mail):
    seen = len(mail.received)
    mail.hold = True
    worker = subprocess.Popen([*app.commands("worker"), "--once"], env=app.env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert mail.waiting.wait(20), "worker did not reach SMTP DATA"
        leased = app.query("SELECT id FROM delivery WHERE tenant='rescue' AND status='leased'")
        assert len(leased) == 1
        delivery, = leased[0]
        worker.kill()
        assert worker.wait(timeout=10) < 0
    finally:
        mail.release.set()
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        worker.stderr.close()

    assert app.query("SELECT status FROM delivery WHERE tenant='rescue' AND id=%s", (delivery,)) == [("leased",)]
    app.query("UPDATE delivery SET lease_until=now()-interval '1 second' WHERE tenant='rescue' AND id=%s RETURNING id", (delivery,))
    app.command("worker", "--once")
    assert app.query("SELECT status FROM delivery WHERE tenant='rescue' AND id=%s", (delivery,)) == [("unknown",)]
    with app.serving():
        recovered_admin = Client(app)
        recovered_admin.cookies = admin.cookies
        recovered_admin.activate()
        recovered_admin.call(TENANT + "/deliveries/" + delivery + "/retry", "POST", {})
        for _ in range(15):
            if not app.command("worker", "--once")["worked"]:
                break
        assert app.query("SELECT status FROM delivery WHERE tenant='rescue' AND id=%s", (delivery,)) == [("accepted",)]
    identity = "<" + delivery + "@"
    attempts = [entry for entry in mail.received[seen:] if entry[0].startswith(identity)]
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def upload(writer, route, body, finish):
    stage = writer.call(route + "/files", "POST", {
        "expected_revision": 1, "filename": "preserve.txt", "length": len(body),
        "digest": hashlib.sha256(body).hexdigest(),
    }, status=201)
    handle = route + "/files/" + stage["id"]
    writer.call(handle + "/bytes", "PUT", {"expected_revision": 1, "file": base64.b64encode(body).decode()})
    if finish:
        writer.call(handle + "/complete", "POST", {"expected_revision": 1})
    return stage["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="conninfo for a test account allowed to create databases")
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--release", type=Path, help="run the generated package instead of the reference runner")
    args = parser.parse_args()

    def commands(command):
        if args.release:
            return [sys.executable, str(args.release.resolve() / command)]
        return [*REFERENCE, command]

    with (
        tempfile.TemporaryDirectory(prefix="approval-recovery-") as path,
        identity_provider() as idp,
        smtp_receiver() as (mail, smtp_port),
        database(args.database, "recovery") as db,
        sync_playwright() as play,
    ):
        browser = play.chromium.launch(executable_path=args.chromium)
        app = Local(commands, db, args.database, Path(path), idp, smtp_port)
        exercise(app, browser, idp, smtp_port, mail)
        browser.close()
        print("Delegation, expiry, staged-object reaping, restored pending deliveries, and interrupted SMTP recovery passed.")


if __name__ == "__main__":
    main()
