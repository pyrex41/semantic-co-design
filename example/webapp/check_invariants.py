"""Check the terminal-delivery law after bounded histories through the live application."""
import argparse
import contextlib
import http.client
import json
import signal
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError

import psycopg
from playwright.sync_api import sync_playwright

from check import EMAILS, HERE, REFERENCE, WORKFLOW, Local, database, identity_provider, port, sign_in, smtp_receiver


LAW = (HERE / "terminal_delivery.sql").read_text()
TENANTS = ("cedar", "oak")
REVIEWERS = ("north", "south")
FAULT_SERVER = r"""
import os
import runpy
import signal
import sys
from unittest.mock import patch

phase, key, library, script, *args = sys.argv[1:]
sys.path.insert(0, library)
from drivers import Context

real_sql = Context.sql
real_commit = Context.commit

def sql(ctx, statement, args):
    result = real_sql(ctx, statement, args)
    if statement.startswith("INSERT INTO command_receipt") and args[2] == "vote" and args[3] == key:
        ctx.interrupt_vote = True
    return result

def commit(ctx):
    if not getattr(ctx, "interrupt_vote", False):
        return real_commit(ctx)
    if phase == "after":
        real_commit(ctx)
    os.kill(os.getpid(), signal.SIGKILL)

sys.argv = [script, *args]
with patch.object(Context, "sql", sql), patch.object(Context, "commit", commit):
    runpy.run_path(script, run_name="__main__")
"""


@dataclass
class Review:
    tenant: str
    submission: str
    document: str
    revision: int
    quorum: int
    version: int = 1
    status: str = "pending"
    approved: int = 0
    rejected: int = 0

    def path(self):
        return f"/api/v1/tenants/{self.tenant}/reviews/{self.submission}"


def histories():
    # All normal decisions and expiry choices after zero through quorum - 1 approvals.
    for quorum in (1, 2):
        yield quorum, ("approve",) * quorum
        for prefix in range(quorum):
            for stop in ("reject", "expire"):
                yield quorum, ("approve",) * prefix + (stop,)


def api(tenant):
    return f"/api/v1/tenants/{tenant}"


class History:
    def __init__(self, app, users):
        self.app, self.users = app, users
        self.documents = {}
        self.reviews = {}
        self.receipts = {}
        self.coverage = Counter()

    def observe(self, step):
        with psycopg.connect(self.app.db) as database:
            database.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            assert (violations := database.execute(LAW).fetchall()) == [], (step, violations)
            actual = database.execute("""
                SELECT s.tenant, s.id, s.document, s.revision, s.policy, s.quorum, s.version, s.status,
                       count(*) FILTER (WHERE v.decision='approve'),
                       count(*) FILTER (WHERE v.decision='reject')
                FROM submission s LEFT JOIN vote v ON (v.tenant,v.submission)=(s.tenant,s.id)
                GROUP BY s.tenant,s.id
            """).fetchall()
            expected = [
                (r.tenant, r.submission, r.document, r.revision, 1, r.quorum,
                 r.version, r.status, r.approved, r.rejected)
                for r in self.reviews.values()
            ]
            assert sorted(actual) == sorted(expected), (step, actual, expected)
            documents = database.execute("SELECT tenant,id,revision,status FROM document").fetchall()
            assert sorted(documents) == sorted((*k, *v) for k, v in self.documents.items()), (step, documents)
            receipts = database.execute("SELECT tenant,key,result FROM command_receipt WHERE command='vote'").fetchall()
            assert {(tenant, key): result for tenant, key, result in receipts} == self.receipts, (step, receipts)
        self.coverage[step] += 1

    def submit(self, tenant, quorum, number, prior=None):
        writer = self.users["writer"]
        values = {"title": f"History {number}", "body": "Approval transition fixture",
                  "amount": 10001 if quorum == 2 else 10000}
        if prior is None:
            draft = writer.call(api(tenant) + "/documents", "POST", values, status=201)
            revision = 1
        else:
            draft = writer.call(f"{api(tenant)}/documents/{prior.document}", "PATCH",
                                {"expected_revision": prior.revision, **values})
            revision = prior.revision + 1
        document = draft["id"]
        assert draft["revision"] == revision and draft["status"] == "draft", draft
        self.documents[(tenant, document)] = revision, "draft"
        self.observe("revision" if prior else "draft")

        submitted = writer.call(f"{api(tenant)}/documents/{document}/submit", "POST", {"expected_revision": revision})
        review = Review(tenant, submitted["id"], document, revision, quorum)
        assert submitted == {"id": review.submission, "document": document, "revision": revision,
                             "version": 1, "quorum": quorum, "status": "pending", "policy": 1}, submitted
        self.documents[(tenant, document)] = revision, "in-review"
        self.reviews[(tenant, review.submission)] = review
        self.observe("submit")
        return review

    def voters(self, review):
        voters = []
        for name in REVIEWERS:
            reviewer = self.users[name]
            items = reviewer.call(f"{api(review.tenant)}/inbox")["items"]
            if review.submission in {row["id"] for row in items}:
                voters.append(reviewer)
        assert len(voters) == review.quorum
        return iter(voters)

    def apply(self, review, event, key=None):
        review.version += 1
        if event == "approve":
            review.approved += 1
            if review.approved == review.quorum:
                review.status = "approved"
        elif event == "reject":
            review.rejected += 1
            review.status = "rejected"
        else:
            assert event == "expire"
            review.status = "expired"

        if review.status != "pending":
            self.documents[(review.tenant, review.document)] = (
                review.revision, "rejected" if event == "expire" else review.status,
            )
        if key is not None:
            result = {"id": review.submission, "version": review.version, "status": review.status,
                      "votes": review.approved, "quorum": review.quorum}
            self.receipts[(review.tenant, key)] = result
            return result
        return None

    def vote(self, review, voter, decision, key):
        path = review.path() + "/vote"
        body = {"expected_version": review.version, "decision": decision}
        assert voter.call(path, "POST", {**body, "expected_version": review.version - 1},
                          status=409, key=key)["error"]["code"] == "stale-version"
        self.observe("stale-version")
        result = voter.call(path, "POST", body, key=key)
        assert result == self.apply(review, decision, key), result
        self.observe(decision)

        assert voter.call(path, "POST", body, key=key) == result
        self.observe("exact-replay")
        changed = "reject" if decision == "approve" else "approve"
        assert voter.call(path, "POST", {**body, "decision": changed},
                          status=409, key=key)["error"]["code"] == "idempotency-conflict"
        self.observe("changed-replay")

    def expiry(self, review):
        # Move the deadline in the disposable database to drive the real worker clock path.
        assert self.app.query("""
            UPDATE submission SET due_at=now()-interval '1 second'
            WHERE tenant=%s AND id=%s RETURNING status
        """, (review.tenant, review.submission)) == [("pending",)]
        self.observe("deadline")
        self.app.command("worker", "--once")
        self.apply(review, "expire")
        self.observe("expire")

    def run(self, tenant, quorum, events, number, prior=None):
        review = self.submit(tenant, quorum, number, prior)
        assigned = self.voters(review)
        for index, event in enumerate(events):
            if event == "expire":
                self.expiry(review)
            else:
                self.vote(review, next(assigned), event, f"history-{number}-decision-{index}")
        assert review.status in ("approved", "rejected", "expired"), review
        self.coverage["complete-history"] += 1
        return review


def setup(app, browser, contexts):
    users = {}
    context, _, admin = sign_in(browser, app, "admin")
    users["admin"] = admin
    contexts.callback(context.close)
    for tenant in TENANTS:
        admin.call(api(tenant) + "/workflows/publish", "POST", {"workflow": WORKFLOW}, status=201)
        admin.call(api(tenant) + "/members/invite", "POST", {"email": EMAILS["writer"], "role": "author"})
        for name in REVIEWERS:
            admin.call(api(tenant) + "/members/invite", "POST", {"email": EMAILS[name], "role": "reviewer"})
    for name in ("writer", *REVIEWERS):
        context, _, users[name] = sign_in(browser, app, name)
        contexts.callback(context.close)
    return History(app, users)


def exercise_delivery(world, receiver):
    app = world.app
    delivery, = app.query("SELECT tenant,id FROM delivery WHERE kind='terminal'")
    tenant, key = delivery

    def stored():
        return app.query("SELECT status,attempts,lease FROM delivery WHERE tenant=%s AND id=%s", delivery)

    assert stored() == [("pending", 0, None)]
    for _ in range(12):
        result = app.command("worker", "--once")
        if result.get("id") == key:
            assert result["status"] == "unknown", result
            assert stored() == [("unknown", 1, None)]
            world.observe("lost-smtp-ack")
            break
        assert result["worked"], result
        assert stored() == [("pending", 0, None)]
        world.observe("other-delivery")
    else:
        raise AssertionError("terminal delivery was not attempted")

    admin = world.users["admin"]
    path = f"{api(tenant)}/deliveries/{key}/retry"
    assert admin.call(path, "POST", {}, key="outbox-redrive-key")["status"] == "pending"
    assert stored() == [("pending", 0, None)]
    world.observe("redrive")
    assert admin.call(path, "POST", {}, key="outbox-redrive-key")["status"] == "pending"
    assert stored() == [("pending", 0, None)]
    world.observe("redrive-replay")
    assert app.command("worker", "--once") == {"worked": True, "id": key, "status": "accepted"}
    assert stored() == [("accepted", 1, None)]
    world.observe("delivery-accepted")
    attempts = [message for message in receiver.received if message[0].startswith("<" + key + "@")]
    assert len(attempts) == 2 and attempts[0] == attempts[1], attempts


@contextlib.contextmanager
def fault_server(app, library, phase, key):
    commands = app.commands

    def instrument(command):
        if command != "serve":
            return commands(command)
        return [sys.executable, "-c", FAULT_SERVER, phase, key, str(library), *commands(command)[1:]]

    app.commands = instrument
    try:
        with app.serving() as process:
            yield process
    finally:
        app.commands = commands


def restart_address(app):
    app.port = port()
    app.origin = f"http://127.0.0.1:{app.port}"
    app.env["SC_ORIGIN"] = app.origin


def crash_vote(world, library, review, voter):
    app = world.app
    key = "crash-terminal-vote"
    path = review.path() + "/vote"
    body = {"expected_version": review.version, "decision": "approve"}
    for phase in ("before", "after"):
        restart_address(app)
        with fault_server(app, library, phase, key) as process:
            try:
                voter.call(path, "POST", body, key=key)
            except (http.client.RemoteDisconnected, ConnectionResetError, URLError):
                pass
            else:
                raise AssertionError(f"{phase}-commit vote acknowledged unexpectedly")
            assert process.wait(timeout=10) == -signal.SIGKILL
        if phase == "after":
            result = world.apply(review, "approve", key)
        world.observe(phase + "-commit-kill")
        restart_address(app)
        with app.serving():
            visible = voter.call(review.path())
            assert (visible["status"], visible["version"]) == (review.status, review.version), visible
            world.observe("restart")
            if phase == "after":
                assert voter.call(path, "POST", body, key=key) == result
                world.observe("restart-replay")


def intent_id(database, review):
    return database.execute("""
        SELECT encode(sha256(convert_to('[' || to_json(%s::text)::text || ',' ||
                  to_json(%s::text)::text || ',"terminal"]', 'UTF8')), 'hex')
    """, (review.tenant, review.submission)).fetchone()[0]


def negative_controls(world, terminal, other_terminal, pending):
    with psycopg.connect(world.app.db) as database:
        terminal_id = intent_id(database, terminal)
        pending_id = intent_id(database, pending)
        other_id = intent_id(database, other_terminal)
        database.commit()
        assert terminal.tenant == pending.tenant and terminal.document != pending.document
        controls = [
            ("missing-intent", "DELETE FROM delivery WHERE tenant=%s AND id=%s",
             (terminal.tenant, terminal_id), {"delivery-cardinality"}),
            ("duplicate-intent", """
                INSERT INTO delivery (tenant,id,kind,recipient,subject,body,status)
                SELECT tenant,id||'-duplicate',kind,recipient,subject,body,status
                FROM delivery WHERE tenant=%s AND id=%s
             """, (terminal.tenant, terminal_id), {"orphan-delivery"}),
            ("wrong-submission-same-totals", """
                UPDATE delivery SET id=%s,body=replace(body,%s,%s) WHERE tenant=%s AND id=%s
             """, (pending_id, terminal.document, pending.document, terminal.tenant, terminal_id),
             {"delivery-cardinality"}),
            ("wrong-tenant", "UPDATE delivery SET tenant=%s WHERE tenant=%s AND id=%s",
             (other_terminal.tenant, terminal.tenant, terminal_id),
             {"delivery-cardinality", "orphan-delivery"}),
            ("wrong-delivery-target", """
                UPDATE delivery SET body=(SELECT body FROM delivery WHERE tenant=%s AND id=%s)
                WHERE tenant=%s AND id=%s
             """, (other_terminal.tenant, other_id, terminal.tenant, terminal_id), {"delivery-target"}),
            ("missing-close", """DELETE FROM audit WHERE tenant=%s AND object=%s AND event='submission.closed'""",
             (terminal.tenant, terminal.submission), {"close-cardinality"}),
            ("duplicate-close", """
                INSERT INTO audit (tenant,actor,event,object,detail)
                SELECT tenant,actor,event,object,detail FROM audit
                WHERE tenant=%s AND object=%s AND event='submission.closed'
             """, (terminal.tenant, terminal.submission), {"close-cardinality"}),
            ("orphan-close", """
                UPDATE audit SET object='unbound-test-submission'
                WHERE tenant=%s AND object=%s AND event='submission.closed'
             """, (terminal.tenant, terminal.submission), {"close-cardinality", "orphan-close"}),
            ("wrong-close-metadata", """
                UPDATE audit SET detail=jsonb_set(detail,'{status}',to_jsonb('rejected'::text))
                WHERE tenant=%s AND object=%s AND event='submission.closed'
             """, (terminal.tenant, terminal.submission), {"close-metadata"}),
        ]
        baseline = database.execute("""
            SELECT tenant,count(*) FROM delivery WHERE kind='terminal' GROUP BY tenant ORDER BY tenant
        """).fetchall()
        database.commit()
        for name, statement, args, expected in controls:
            with database.transaction(force_rollback=True):
                assert database.execute(statement, args).rowcount == 1, name
                violations = database.execute(LAW).fetchall()
                assert {issue[0] for issue in violations} == expected, (name, violations)
                if name == "wrong-submission-same-totals":
                    assert database.execute("""
                        SELECT tenant,count(*) FROM delivery WHERE kind='terminal' GROUP BY tenant ORDER BY tenant
                    """).fetchall() == baseline
                    assert set(violations) == {
                        ("delivery-cardinality", terminal.tenant, terminal.submission),
                        ("delivery-cardinality", pending.tenant, pending.submission),
                    }, violations
            world.observe("negative-control-rollback")
        return [name for name, *_ in controls]


def exercise(app, browser, receiver, library):
    app.admin("migrate")
    for tenant in TENANTS:
        app.admin("provision", {"tenant": tenant, "name": tenant.capitalize(), "email": EMAILS["admin"]})
    with contextlib.ExitStack() as contexts:
        with app.serving():
            world = setup(app, browser, contexts)
            world.observe("initial-state")
            cases = list(histories())
            terminal = world.run(TENANTS[0], *cases[0], 0)
            exercise_delivery(world, receiver)
            finished = [terminal]
            for index, case in enumerate(cases[1:], 1):
                finished.append(world.run(TENANTS[index % len(TENANTS)], *case, index))
            rejected = next(review for review in finished if review.status == "rejected")
            resubmitted = world.run(rejected.tenant, 1, ("approve",), len(cases), prior=rejected)
            assert resubmitted.document == rejected.document and resubmitted.submission != rejected.submission

            pending = world.submit(terminal.tenant, 1, len(cases) + 1)
            voter = next(world.voters(pending))
            other = next(review for review in finished if review.tenant != terminal.tenant)
            controls = negative_controls(world, terminal, other, pending)
        crash_vote(world, library, pending, voter)
    return {"histories": len(cases), "tenants": len(TENANTS), "negative_controls": controls,
            "observations": sum(world.coverage.values()) - world.coverage["complete-history"],
            "coverage": dict(world.coverage)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="conninfo for a test account allowed to create databases")
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--release", type=Path, help="run a generated package instead of the reference runner")
    if not __debug__:
        parser.error("invariant checks require Python assertions enabled")
    args = parser.parse_args()
    release = args.release.resolve() if args.release else None

    def commands(command):
        return [sys.executable, str(release / command)] if release else [*REFERENCE, command]

    mode = "native" if release else "reference"
    with (
        tempfile.TemporaryDirectory(prefix="approval-invariants-") as path,
        identity_provider() as idp,
        smtp_receiver() as (receiver, smtp_port),
        database(args.database, f"invariants_{mode}") as db,
        sync_playwright() as play,
    ):
        browser = play.chromium.launch(executable_path=args.chromium)
        app = Local(commands, db, args.database, Path(path), idp, smtp_port)
        try:
            report = exercise(app, browser, receiver, release / "lib" if release else HERE)
        except Exception:
            log = app.root / "server.log"
            if log.exists():
                print("\n".join(log.read_text().splitlines()[-30:]), file=sys.stderr)
            raise
        finally:
            browser.close()
    print(json.dumps({"mode": mode, **report}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
