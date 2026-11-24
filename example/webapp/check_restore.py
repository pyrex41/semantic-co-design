"""Fault-inject the public restore command against a disposable PostgreSQL instance."""
import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

from check import HERE, REFERENCE, database


BLOBS = {"alpha": b"first object\x00", "bravo": b"second object\xff"}
DATABASE_GATE = 4268310430
FAULT = r"""
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

fault, library, ready, script, *args = sys.argv[1:]
sys.path.insert(0, library)
import drivers

original_put = drivers.BlobStore.put
original_run = subprocess.run
original_install = drivers.Context.install_dump

def kill():
    os.kill(os.getpid(), signal.SIGKILL)

def put(store, key, data):
    if key == "bravo":
        if fault == "second-write-error":
            raise OSError("fixture failed the second object")
        if fault == "second-write-kill":
            with tempfile.NamedTemporaryFile(dir=store.root, prefix=".blob-", delete=False) as partial:
                partial.write(data[:1])
                partial.flush()
                os.fsync(partial.fileno())
                kill()
    return original_put(store, key, data)

def run(command, *args, **kwargs):
    if command[0] == "psql":
        if fault == "database-write-error":
            command = [*command[:-2], "-c", "SELECT 1 / 0", *command[-2:]]
        elif fault == "during-database-install":
            command = [*command[:-2], "-c",
                       "SET LOCAL lock_timeout='25s'; SELECT pg_advisory_xact_lock(4268310430)",
                       *command[-2:]]
    result = original_run(command, *args, **kwargs)
    if fault == "after-database-commit" and command[0] == "psql":
        kill()
    return result

def install(ctx, *args):
    if fault == "before-database-commit":
        Path(ready).touch()
        time.sleep(30)
    return original_install(ctx, *args)

sys.argv = [script, *args]
with patch.object(drivers.BlobStore, "put", put), patch.object(drivers.Context, "install_dump", install):
    with patch.object(subprocess, "run", run):
        runpy.run_path(script, run_name="__main__")
"""


class Admin:
    def __init__(self, release, db, blobs):
        self.blobs = blobs
        self.db = db
        self.prefix = [sys.executable, str(release / "admin")] if release else [*REFERENCE, "admin"]
        self.library = release / "lib" if release else HERE
        self.env = {**os.environ, "SC_DATABASE": db, "SC_BLOBS": str(blobs),
                    "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}

    def call(self, operation, path=None, code=0, expected_error=None, fault=None):
        result = subprocess.run(self.command(operation, path, fault), env=self.env,
                                capture_output=True, text=True, timeout=135)
        assert result.returncode == code, (operation, fault, result.returncode, result.stderr)
        if expected_error:
            assert expected_error in result.stderr, result.stderr
        if code == 0:
            return json.loads(result.stdout)
        return None

    def command(self, operation, path, fault=None, ready=None):
        args = [operation]
        if path is not None:
            args.append(json.dumps({"path": str(path)}))
        if fault is None:
            return [*self.prefix, *args]
        return [sys.executable, "-c", FAULT, fault, str(self.library), str(ready or ""),
                *self.prefix[1:], *args]

    def query(self, sql):
        with psycopg.connect(self.db) as conn:
            result = conn.execute(sql)
            if result.description:
                return result.fetchall()
            return None


def pending(target):
    return target.query("SELECT to_regclass('sc_storage_restore.pending') IS NOT NULL") == [(True,)]


def verify_restore(target, snapshot):
    assert target.call("restore", snapshot)["status"] == "restored"
    assert target.query("SELECT value FROM witness") == [("from backup",)]
    assert not pending(target)
    recovered = {path.name: path.read_bytes() for path in target.blobs.iterdir()
                 if not path.name.startswith(".")}
    assert recovered == BLOBS, recovered
    target.call("doctor")
    # A repeat of a committed request must preserve later live writes.
    target.query("UPDATE witness SET value='live data'")
    (target.blobs / "new-live-object").write_bytes(b"live data")
    assert target.call("restore", snapshot)["status"] == "restored"
    assert target.query("SELECT value FROM witness") == [("live data",)]
    assert (target.blobs / "new-live-object").read_bytes() == b"live data"


def before_commit(target, snapshot, root, release):
    ready = root / "publishing-finished"
    command = target.command("restore", snapshot, "before-database-commit", ready)
    with subprocess.Popen(command, env=target.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as worker:
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.025)
            assert ready.exists() and worker.poll() is None, "restore did not reach the database handoff"
            assert pending(target)
            assert target.query("SELECT to_regclass('public.witness')") == [(None,)]
            assert {name: (target.blobs / name).read_bytes() for name in BLOBS} == BLOBS
            target.call("migrate", code=2, expected_error="restore-incomplete")
            target.call("backup", root / "pending-backup", code=2, expected_error="restore-incomplete")
            alternate = Admin(release, target.db, root / "alternate-blobs")
            alternate.call("restore", snapshot, code=2, expected_error="restore-destination")
            assert not alternate.blobs.exists()
            same_target = subprocess.run(target.command("restore", snapshot), env=target.env,
                                         capture_output=True, text=True, timeout=20)
            assert same_target.returncode == 2 and "restore-busy" in same_target.stderr, same_target.stderr
        finally:
            worker.kill()
            assert worker.wait(timeout=10) == -signal.SIGKILL
    assert pending(target)
    target.call("migrate", code=2, expected_error="restore-incomplete")


def interrupt_database_install(target, snapshot):
    command = target.command("restore", snapshot, "during-database-install")
    with psycopg.connect(target.db, autocommit=True) as control:
        control.execute("SELECT pg_advisory_lock(%s)", (DATABASE_GATE,))
        with subprocess.Popen(command, env=target.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as worker:
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and worker.poll() is None:
                    waiting = control.execute("""
                        SELECT EXISTS (SELECT FROM pg_locks
                                       WHERE locktype='advisory' AND classid=0 AND objid=%s AND NOT granted
                                         AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))
                    """, (DATABASE_GATE,)).fetchone()[0]
                    if waiting:
                        break
                    time.sleep(.025)
                else:
                    raise AssertionError("installer did not reach its transaction barrier")
                assert pending(target)
                assert target.query("SELECT to_regclass('public.witness')") == [(None,)]
                assert {name: (target.blobs / name).read_bytes() for name in BLOBS} == BLOBS
            finally:
                worker.kill()
                assert worker.wait(timeout=10) == -signal.SIGKILL
                control.execute("SELECT pg_advisory_unlock(%s)", (DATABASE_GATE,))
    # The independently locked installer may complete the transaction after its parent is gone.
    deadline = time.monotonic() + 15
    while pending(target) and time.monotonic() < deadline:
        time.sleep(.025)
    assert not pending(target)
    assert target.query("SELECT value FROM witness") == [("from backup",)]


def preserve_destinations(base, root, release, snapshot):
    with database(base, "restore_existing_blobs") as db:
        blobs = root / "existing-blobs"
        blobs.mkdir()
        (blobs / "alpha").write_bytes(b"unrelated object under a matching name")
        (blobs / "other").write_bytes(b"another unrelated object")
        admin = Admin(release, db, blobs)
        admin.call("restore", snapshot, code=2, expected_error="restore-destination")
        assert not pending(admin)
        assert {path.name: path.read_bytes() for path in blobs.iterdir()} == {
            "alpha": b"unrelated object under a matching name", "other": b"another unrelated object",
        }
    with database(base, "restore_existing_db") as db:
        admin = Admin(release, db, root / "untouched-blobs")
        admin.query("CREATE SCHEMA unrelated; CREATE TABLE unrelated.keep (data text);"
                    "INSERT INTO unrelated.keep VALUES ('untouched')")
        admin.call("restore", snapshot, code=2, expected_error="restore-destination")
        assert not pending(admin)
        assert not admin.blobs.exists()
        assert admin.query("SELECT data FROM unrelated.keep") == [("untouched",)]


def exercise(base, root, release):
    with database(base, "restore_source") as db:
        source = Admin(release, db, root / "source-blobs")
        source.call("migrate")
        source.query("CREATE TABLE witness (value text); INSERT INTO witness VALUES ('from backup')")
        source.blobs.mkdir()
        for name, data in BLOBS.items():
            (source.blobs / name).write_bytes(data)
        snapshot = root / "snapshot"
        source.call("backup", snapshot)

    preserve_destinations(base, root, release, snapshot)
    faults = ("second-write-error", "second-write-kill", "before-database-commit",
              "database-write-error", "during-database-install", "after-database-commit")
    for fault in faults:
        with database(base, fault.replace("-", "_")) as db:
            target = Admin(release, db, root / fault)
            if fault == "before-database-commit":
                before_commit(target, snapshot, root, release)
            elif fault == "during-database-install":
                interrupt_database_install(target, snapshot)
            elif fault == "database-write-error":
                target.call("restore", snapshot, code=2, expected_error="restore-incomplete", fault=fault)
                assert pending(target)
                assert target.query("SELECT to_regclass('public.witness')") == [(None,)]
                assert {name: (target.blobs / name).read_bytes() for name in BLOBS} == BLOBS
            elif fault == "after-database-commit":
                target.call("restore", snapshot, code=-signal.SIGKILL, fault=fault)
                assert target.query("SELECT value FROM witness") == [("from backup",)]
                assert not pending(target)
            else:
                target.blobs.mkdir()
                expected = {"code": 2, "expected_error": "restore-incomplete"} if fault.endswith("error") else {
                    "code": -signal.SIGKILL,
                }
                target.call("restore", snapshot, fault=fault, **expected)
                assert list(target.blobs.iterdir()) == []
                assert pending(target)
                assert target.query("SELECT to_regclass('public.witness')") == [(None,)]
            verify_restore(target, snapshot)

            if fault == "after-database-commit":
                onward = root / "onward-snapshot"
                target.call("backup", onward)
                with database(base, "restore_onward") as onward_db:
                    next_target = Admin(release, onward_db, root / "onward-blobs")
                    assert next_target.call("restore", onward)["status"] == "restored"
                    assert next_target.query("SELECT value FROM witness") == [("live data",)]
                    assert (next_target.blobs / "new-live-object").read_bytes() == b"live data"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="conninfo for a test account allowed to create databases")
    parser.add_argument("--release", type=Path, help="run the generated package instead of the reference runner")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="approval-restore-check-") as root:
        exercise(args.database, Path(root), args.release.resolve() if args.release else None)
    print("Restore retries, process death, destination ownership, and onward backups passed.")


if __name__ == "__main__":
    main()
