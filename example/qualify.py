"""Build the tracked packages and exercise them in disposable local environments."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "webapp"
TRADING = ROOT / "trading-engine"
ROUTER = ROOT / "router-nlb"
PYTHON = sys.executable


def run(*command, timeout=360, **kwargs):
    print("+", *(str(x) for x in command), flush=True)
    return subprocess.run(command, check=True, timeout=timeout, **kwargs)


@contextmanager
def database(work):
    # Unix sockets in a private directory avoid TCP port collisions with a live server.
    data = work / "pg"
    run("initdb", "-D", data, "-A", "trust", "--no-locale", "--encoding=UTF8", "-U", "appcheck")
    run("pg_ctl", "-D", data, "-l", work / "pg.log", "-w", "-o",
        f"-k {work} -c listen_addresses=''", "start")
    try:
        yield f"host={work} user=appcheck dbname=postgres"
    finally:
        run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")


def qualify(work, args):
    run(PYTHON, ROOT / "check_language.py")
    packages = {}
    for product in (WEB, ROUTER, TRADING):
        out = work / product.name
        run(PYTHON, WEB / "appc.py", "build", product / "manifest.json", "--out", out)
        manifest = json.loads((product / "manifest.json").read_text())
        record = json.loads((out / "ownership.json").read_text())
        assert record["language"] == manifest["language"] and record["entries"] == manifest["entries"]
        packages[product] = out
    policy = {"threshold": 100, "quorum": 1, "above_quorum": 2, "delegation": True, "expires_hours": 72}
    reply = run(PYTHON, packages[WEB] / "policy", input=json.dumps(policy), text=True, stdout=subprocess.PIPE)
    assert json.loads(reply.stdout) == {**policy, "abi": 1}

    if not (args.full or args.state_machines):
        run(PYTHON, TRADING / "check.py", packages[TRADING])
        run(PYTHON, ROUTER / "check.py", packages[ROUTER], "--replay-only")
        print("Passed package and language checks.")
        return

    if args.full:
        run(PYTHON, TRADING / "check.py")
        peer = ["--gobgp", args.gobgp] if args.gobgp else []
        run(PYTHON, ROUTER / "check.py", packages[ROUTER], "--compiler", WEB / "appc.py", *peer)
    else:
        run(PYTHON, TRADING / "check_reachable.py", packages[TRADING])
        run(PYTHON, ROUTER / "check.py", packages[ROUTER], "--state-law-only")

    chrome = ["--chromium", args.chromium] if args.chromium else []
    with database(work) as db:
        if args.full:
            run(PYTHON, WEB / "check.py", "--database", db, "--release", packages[WEB], "--frozen-change", *chrome)
        for mode in ([], ["--release", packages[WEB]]):
            run(PYTHON, WEB / "check_invariants.py", "--database", db, *mode, *chrome)
            if args.full:
                run(PYTHON, WEB / "check_restore.py", "--database", db, *mode)
                run(PYTHON, WEB / "check_recovery.py", "--database", db, *mode, *chrome)
    print("Passed local public-interface qualification." if args.full else "Passed reachable-state checks for all three products.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="include browser, database, socket and process-fault gates")
    mode.add_argument("--state-machines", action="store_true", help="build and check generated transition histories for all three products")
    parser.add_argument("--chromium", type=Path, help="installed Chromium executable for browser checks")
    parser.add_argument("--gobgp", type=Path, help="directory with gobgp and gobgpd for independent peer qualification")
    args = parser.parse_args()
    if not __debug__:
        parser.error("qualification requires Python assertions enabled")
    if args.gobgp and not args.full:
        parser.error("--gobgp applies to --full")
    if args.chromium and not (args.full or args.state_machines):
        parser.error("--chromium requires --full or --state-machines")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PATH"] = str(Path(PYTHON).parent) + os.pathsep + os.environ["PATH"]
    with tempfile.TemporaryDirectory(prefix="app-check-", dir="/tmp") as directory:
        qualify(Path(directory), args)
