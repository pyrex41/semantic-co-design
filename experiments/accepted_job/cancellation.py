"""Replay candidate AcceptedJob/2 policies while the requirement decision is open."""
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from replay import SUBMIT, specialize


def history(strategy, cancel_commits):
    submit, _ = specialize(SUBMIT)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "jobs.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY, digest TEXT,
                       status TEXT NOT NULL DEFAULT 'pending')""")
            db.execute("CREATE TABLE deliveries(job INTEGER NOT NULL)")
            receipt = submit(db, "A")
            db.commit()
            db.execute("BEGIN")
            if strategy == "withdraw-pending":
                db.execute("UPDATE jobs SET status='withdrawn' WHERE id=? AND status='pending'",
                           (receipt[1],))
            if cancel_commits:
                db.commit()
            # Both strategies acknowledge a committed cancel. A restart before
            # commit loses the cancellation transaction and its reply.
            cancel_reply = "ok" if cancel_commits else "lost"
        with closing(sqlite3.connect(path)) as worker:
            at_restart = tuple(worker.execute("SELECT id,status FROM jobs"))
            replay = submit(worker, "A")
            with worker:
                for job, in worker.execute("SELECT id FROM jobs WHERE status='pending'").fetchall():
                    worker.execute("INSERT INTO deliveries VALUES (?)", (job,))
                    worker.execute("UPDATE jobs SET status='delivered' WHERE id=?", (job,))
            deliveries = tuple(row[0] for row in worker.execute("SELECT job FROM deliveries"))
            final = tuple(worker.execute("SELECT id,status FROM jobs"))
        return receipt, cancel_reply, at_restart, replay, deliveries, final


def admits(policy, committed, observation):
    receipt, reply, restart, retry_receipt, deliveries, final = observation
    if receipt[0] != "ok" or retry_receipt != receipt or reply != ("ok" if committed else "lost"):
        return False
    job = receipt[1]
    if committed and policy == "withdraw-pending":
        return restart == final == ((job, "withdrawn"),) and deliveries == ()
    return restart == ((job, "pending"),) and deliveries == (job,) and final == ((job, "delivered"),)


def main():
    policies = ("stop-waiting", "withdraw-pending")
    for committed in (True, False):
        for strategy in policies:
            observation = history(strategy, committed)
            admitted = [policy for policy in policies if admits(policy, committed, observation)]
            expected = [strategy] if committed else list(policies)
            if admitted != expected:
                raise AssertionError((observation, admitted, expected))
            print(f"cancel committed={committed} strategy={strategy} restart={observation[2]}"
                  f" retry={observation[3]} deliveries={observation[4]} admitted={admitted}")


if __name__ == "__main__":
    main()
