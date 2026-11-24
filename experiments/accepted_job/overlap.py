"""Controlled two-client SQLite schedules for AcceptedJob."""
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path
from functools import partial

from replay import SUBMIT, interpret, specialize


def lookup(db, request, scoped):
    tenant, key, _ = request
    if scoped:
        return db.execute("SELECT id, digest FROM jobs WHERE tenant=? AND key=?",
                          (tenant, key)).fetchone()
    return db.execute("SELECT id, digest FROM jobs WHERE key=?", (key,)).fetchone()


def finish(db, request, seen):
    if seen is None:
        cursor = db.execute("INSERT INTO jobs(tenant,key,digest) VALUES (?,?,?)", request)
        return ("ok", cursor.lastrowid)
    if seen[1] == request[2]:
        return ("ok", seen[0])
    return ("conflict", None)


def execute(requests, strategy, submit=None):
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        path = Path(directory) / "jobs.db"
        a, b = [stack.enter_context(closing(sqlite3.connect(path, timeout=0)))
                for _ in requests]
        a.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, tenant, key, digest)")
        a.execute("BEGIN IMMEDIATE")
        scoped = strategy != "global-key"
        if submit is None:
            first = lookup(a, requests[0], scoped)
        if strategy == "split-lookup":
            # B observes absence while A holds the writer slot but has yet to insert.
            second = lookup(b, requests[1], scoped)
        else:
            try:
                b.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if "locked" not in str(error):
                    raise
            else:
                raise AssertionError("B acquired A's writer slot")
        if submit is None:
            reply_a = finish(a, requests[0], first)
        else:
            reply_a = submit(a, requests[0][2], scope=requests[0][:2])
        a.commit()
        b.execute("BEGIN IMMEDIATE")
        if submit is None:
            if strategy != "split-lookup":
                second = lookup(b, requests[1], scoped)
            reply_b = finish(b, requests[1], second)
        else:
            reply_b = submit(b, requests[1][2], scope=requests[1][:2])
        b.commit()
        rows = list(b.execute("SELECT id,tenant,key,digest FROM jobs ORDER BY id"))
        return [reply_a, reply_b], rows


def admitted(requests, replies, rows):
    # Build expected obligations from client intent. Receipts must identify actual rows.
    required = {}
    for request, reply in zip(requests, replies):
        tenant, key, digest = request
        prior = required.get((tenant, key))
        if prior and prior[1] != digest:
            if reply != ("conflict", None):
                return False
        else:
            if reply[0] != "ok" or (prior and reply[1] != prior[0]):
                return False
            required[(tenant, key)] = (reply[1], digest)
    return sorted(rows) == sorted((receipt, tenant, key, digest)
                                  for (tenant, key), (receipt, digest) in required.items())


def main():
    first = ("north", "k", "A")
    for suite, other in (("same", first), ("conflict", ("north", "k", "B")),
                         ("two-tenants", ("south", "k", "A"))):
        for strategy in ("serialized", "split-lookup", "global-key"):
            for requests in ((first, other), (other, first)):
                replies, rows = execute(requests, strategy)
                verdict = admitted(requests, replies, rows)
                expected = not ((strategy == "split-lookup" and suite != "two-tenants")
                                or (strategy == "global-key" and suite == "two-tenants"))
                if verdict != expected:
                    raise AssertionError((requests, strategy, replies, rows))
                print(suite, strategy, requests[0], replies, rows,
                      "admitted" if verdict else "rejected")

    programs = {"submit": SUBMIT,
                "duplicate": (SUBMIT[0], ("same", "duplicate"), SUBMIT[2]),
                "decline": (*SUBMIT[:2], ("otherwise", "decline"))}
    for name, program in programs.items():
        generated, _ = specialize(program, scoped=True)
        for suite, other in (("same", first), ("conflict", ("north", "k", "B")),
                             ("two-tenants", ("south", "k", "A")),
                             ("two-keys", ("north", "k2", "A"))):
            for requests in ((first, other), (other, first)):
                observed = execute(requests, "serialized", generated)
                reference = execute(requests, "serialized", partial(interpret, program))
                verdict = admitted(requests, *observed)
                expected = (name, suite) not in {("duplicate", "same"), ("decline", "conflict")}
                if observed != reference or verdict != expected:
                    raise AssertionError((name, requests, observed, reference, verdict))
                print("specialized", name, suite, requests[0], "admitted" if verdict else "rejected")


if __name__ == "__main__":
    main()
