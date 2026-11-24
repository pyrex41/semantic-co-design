"""Compile both policies and check the same durable history across process restarts."""
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def check(directory, policy):
    tool = [sys.executable, "-B", HERE / "spec.py", HERE / "accepted-job.json", "--bind", f"cancel={policy}"]
    binary = directory / "job"
    subprocess.run([*tool, "--compile", binary], check=True)
    digest, other = [hashlib.sha256(data).hexdigest() for data in (b"request", b"other")]
    receipt = f"receipt-{digest}"
    terminal = "withdrawn" if policy == "withdrawn" else "delivered"
    events = [
        (f"submit-{digest}", "pending", receipt),
        (f"submit-{digest}", "pending", receipt),
        (f"submit-{other}", "pending", "conflict"),
        ("cancel", policy, "cancelled"),
        ("work", terminal, "ignored" if policy == "withdrawn" else f"deliver-{digest}"),
        ("work", terminal, "ignored"),
        (f"submit-{digest}", terminal, receipt),
        (f"submit-{other}", terminal, "conflict"),
    ]
    for mode in ("native", "interpreted"):
        db = directory / f"{policy}-{mode}.db"
        cmd = [binary, db, "42"] if mode == "native" else [*tool, "--database", db, "--key", "42", "--run"]
        for event, state, output in events:
            actual = subprocess.check_output([*cmd, event], text=True).strip()
            if actual != f"{state}-{digest} {output}":
                raise ValueError((policy, mode, event, actual))
        with sqlite3.connect(db) as store:
            if store.execute("SELECT id,state FROM instances").fetchall() != [(42, f"{terminal}-{digest}")]:
                raise ValueError("unexpected durable rows")
    print(policy, "passed native and interpreted histories")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as scratch:
        for policy in ("withdrawn", "pending"):
            check(Path(scratch), policy)
