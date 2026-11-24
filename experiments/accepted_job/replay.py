"""AcceptedJob/1 bounded experiment. Run with Python 3.9 or later."""
import argparse
import itertools
import sqlite3
import tempfile
from pathlib import Path

# Ordered guards and their actions, evaluated inside one storage transaction.
SUBMIT = (("absent", "create"), ("same", "replay"), ("otherwise", "conflict"))
ACTIONS = {
    "create": ("inserted = db.execute('INSERT INTO jobs(digest) VALUES (?)', (digest,))",
               "return ('ok', inserted.lastrowid)"),
    "replay": ("return ('ok', row[0])",),
    "duplicate": ("db.execute('INSERT INTO jobs(digest) VALUES (?)', (digest,))",
                  "return ('ok', row[0])"),
    "conflict": ("return ('conflict', None)",),
    "decline": ("return ('declined', None)",),
}
GUARDS = {"absent": "row is None", "same": "row[1] == digest", "otherwise": "True"}
LOOKUP = "SELECT id, digest FROM jobs ORDER BY id LIMIT 1"


SCOPED_LOOKUP = "SELECT id, digest FROM jobs WHERE tenant=? AND key=? ORDER BY id LIMIT 1"
SCOPED_INSERT = "INSERT INTO jobs(tenant,key,digest) VALUES (?,?,?)"


def interpret(program, db, digest, *, scope=None):
    if scope is None:
        row = db.execute(LOOKUP).fetchone()
    else:
        row = db.execute(SCOPED_LOOKUP, scope).fetchone()
    for guard, action in program:
        if guard == "absent" and row is not None:
            continue
        if guard == "same" and row[1] != digest:
            continue
        if action in ("create", "duplicate"):
            if scope is None:
                cursor = db.execute("INSERT INTO jobs(digest) VALUES (?)", (digest,))
            else:
                cursor = db.execute(SCOPED_INSERT, (*scope, digest))
            if action == "create":
                return ("ok", cursor.lastrowid)
        if action in ("replay", "duplicate"):
            return ("ok", row[0])
        return ("conflict" if action == "conflict" else "declined", None)
    raise ValueError("unhandled input")


def specialize(program, *, scoped=False):
    actions = ACTIONS
    if scoped:
        lines = ["def submit(db, digest, *, scope):",
                 f"    row = db.execute({SCOPED_LOOKUP!r}, scope).fetchone()"]
        insert = f"db.execute({SCOPED_INSERT!r}, (*scope, digest))"
        actions = {**ACTIONS, "create": ("inserted = " + insert, "return ('ok', inserted.lastrowid)"),
                   "duplicate": (insert, "return ('ok', row[0])")}
    else:
        lines = ["def submit(db, digest):", f"    row = db.execute({LOOKUP!r}).fetchone()"]
    for guard, action in program:
        lines.append(f"    if {GUARDS[guard]}:")
        lines.extend("        " + line for line in actions[action])
    source = "\n".join(lines) + "\n"
    namespace = {}
    exec(source, namespace)
    return namespace["submit"], source


def execute(submit, first_outcome, retry_digest, *, capture_lost_reply=False):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "jobs.sqlite"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, digest TEXT NOT NULL)")
            original = submit(db, "A")
            if first_outcome == "commit":
                db.commit()
        finally:
            db.close()  # Uncommitted transaction rolls back. No first reply delivered.
        db = sqlite3.connect(path)
        try:
            before = snapshot(db)
            reply = submit(db, retry_digest)
            db.commit()
            history = before, reply, snapshot(db)
            return (original, history) if capture_lost_reply else history
        finally:
            db.close()


def snapshot(db):
    # Observation mapping v1 preserves each row, including repeated digests.
    return tuple(db.execute("SELECT id, digest FROM jobs ORDER BY id"))


def violation(outcome, digest, history):
    before, reply, after = history
    if outcome == "rollback":
        if before or len(after) != 1 or after[0][1] != digest:
            return "rollback must leave retry free to create exactly one job"
        expected = ("ok", after[0][0])
    else:
        if len(before) != 1 or before[0][1] != "A":
            return "committed job missing at restart"
        if after != before:
            return "retry changed durable job rows"
        expected = ("ok", before[0][0]) if digest == "A" else ("conflict", None)
    if reply != expected:
        return f"expected {expected}, received {reply}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    for name, program in (
        ("submit", SUBMIT),
        ("duplicate", (SUBMIT[0], ("same", "duplicate"), SUBMIT[2])),
        ("changed-conflict", (*SUBMIT[:2], ("otherwise", "decline"))),
    ):
        generated, source = specialize(program)
        if args.source:
            print(name, source, sep="\n")
        for outcome, digest in itertools.product(("commit", "rollback"), ("A", "B")):
            history = execute(generated, outcome, digest)
            reference = execute(lambda db, d: interpret(program, db, d), outcome, digest)
            if history != reference:
                raise RuntimeError((name, "specialization mismatch", history, reference))
            reason = violation(outcome, digest, history)
            expected_rejection = outcome == "commit" and (
                (name == "duplicate" and digest == "A") or
                (name == "changed-conflict" and digest == "B"))
            if bool(reason) != expected_rejection:
                raise RuntimeError((name, "unexpected verdict", history, reason))
            print(name, outcome, digest, history, reason or "admitted")


if __name__ == "__main__":
    main()
