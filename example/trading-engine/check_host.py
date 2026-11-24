"""Process-level checks for the SQLite, API and release boundaries."""

import json
import os
import pwd
import select
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from trading_checks import CLIENT, MANIFEST, PRODUCT, VENUE, accounts, fixture, intent, order
from event_host import Store
from package import appc


def command(release, database, *args, payload=None, check=True):
    return subprocess.run([str(release / "engine"), "--database", str(database), *args],
                          input=json.dumps(payload or {}), text=True, capture_output=True, timeout=20, check=check)


def child(release, database, payload):
    source = Path(__file__).resolve().parent
    code = "import check, sys; sys.modules['trading_checks'] = check; import check_host; check_host.child_main()"
    process = subprocess.Popen([sys.executable, "-c", code, str(release), str(database)], cwd=source,
                               stdin=subprocess.PIPE, text=True)
    process.stdin.write(json.dumps(payload))
    process.stdin.close()
    return process


def child_main():
    from trading_checks import import_native

    sys.setrecursionlimit(20_000)
    release, database = map(Path, sys.argv[1:])
    args = json.load(sys.stdin)
    manifest, _ = appc.load(MANIFEST)
    version = json.loads((release / "ownership.json").read_text())["release"]
    roles = {role for target in manifest["access"].values() for role in target["roles"]}
    store = Store(database, manifest, import_native(release), version)
    if "start-at" in args:
        time.sleep(max(0, args["start-at"] - time.time()))

    def fault(observed):
        if observed == args.get("at"):
            os._exit(args["code"])

    if args["mode"] == "admit":
        result = store.invoke("admit", intent(args["client"], args["price"]), time_ms=args["time"],
                              fence=1, actor="acceptance", roles=roles, fault=fault)
        os._exit({"admitted": 80, "insufficient-available": 81}[result["status"]])

    def accept(request, _):
        raw = request["body"]
        body = order(raw["client_oid"], VENUE.format(1), raw["price"], raw["size"])
        with Path(args["exchange"]).open("x") as file:
            file.write(json.dumps(body))
            file.flush()
            os.fsync(file.fileno())
        return {"network": "response", "status": 200, "body": body}

    store.send({"client-id": args["client"]}, None, fence=1, actor="acceptance", roles=roles,
               effect=accept, fault=fault, clock=lambda: args["time"])
    raise RuntimeError("the injected process failure did not occur")


def run(release, entries, root):
    manifest, _ = appc.load(MANIFEST)
    ownership = json.loads((release / "ownership.json").read_text())
    version = ownership["release"]
    database = root / "trading.sqlite"
    store = Store(database, manifest, entries, version)
    roles = {role for target in manifest["access"].values() for role in target["roles"]}
    now = 1000

    def invoke(entry, event, expected, *, fence=1, clock=None, fault=None):
        nonlocal now
        now += 200
        reply = store.invoke(entry, event, time_ms=now if clock is None else clock, fence=fence,
                             actor="acceptance", roles=roles, fault=fault)
        assert reply["status"] == expected, (entry, reply, expected)
        return reply["data"]

    invoke("acquire-writer", {"fence": 1}, "reconciliation-required")
    statement = accounts("100")
    invoke("start", {"accounts": statement}, "initialized")
    invoke("reconcile", {"from-cursor": 0, "to-cursor": 0, "fills-complete": True,
                         "open-orders": [], "accounts": statement}, "reconciled")
    invoke("resume", {}, "trading")
    invoke("metadata", {"product": PRODUCT, "body": fixture(PRODUCT, "product")}, "metadata")
    invoke("snapshot", {"product": PRODUCT, "body": fixture(PRODUCT, "book")}, "book")
    invoke("limits", {"ceiling": 100 * 10**16, "fee-ppm": 200_000}, "limits-updated")
    prior, first, second, lost = (CLIENT.format(i) for i in range(4))

    admission = {"mode": "admit", "client": prior, "price": 5_000, "time": now + 200}
    assert child(release, database, {**admission, "at": "before-commit", "code": 72}).wait(timeout=20) == 72
    invoke("order", {"client-id": prior}, "unknown-order")
    assert child(release, database, {**admission, "at": "after-commit", "code": 73}).wait(timeout=20) == 73
    invoke("admit", intent(prior, 5_000), "duplicate-client-id")

    start_at = time.time() + 0.5
    children = [child(release, database, {"mode": "admit", "client": client, "price": 2_500,
                                        "time": now + 200, "start-at": start_at}) for client in (first, second)]
    assert sorted(process.wait(timeout=20) for process in children) == [80, 81]
    now += 200
    picked = invoke("blotter", {}, "blotter")
    winner = next(client for client in (first, second) if client in picked)
    external = root / "exchange.json"
    now += 500
    assert child(release, database, {"mode": "send", "client": winner, "exchange": str(external),
                                     "time": now, "at": "after-effect", "code": 74}).wait(timeout=20) == 74
    accepted = json.loads(external.read_text())
    assert invoke("order", {"client-id": winner}, "order")["phase"] == "sent-unknown"
    invoke("acquire-writer", {"fence": 2}, "reconciliation-required", fence=2)
    invoke("claim", {"client-id": winner}, "no-work", fence=2)
    invoke("order-evidence", {"client-id": winner, "body": accepted}, "acknowledged", fence=2)
    invoke("admit", intent(lost, 2_500), "admission-fenced", fence=2)
    report = store.replay({version: entries})
    assert report["events"] == 16
    backup = root / "restored.sqlite"
    assert json.loads(command(release, database, "backup", str(backup)).stdout) == {"backup": str(backup)}
    assert json.loads(command(release, backup, "replay").stdout) == report
    assert json.loads(command(release, backup, "call", "status").stdout)["data"]["outstanding"]
    auth_events = api(release, store, root)
    return {"durable_events": report["events"], "process_deaths": 3, "api_checks": auth_events,
            "recovered_execution_identity": accepted["id"], "durable_fingerprint": report["state_digest"]}


def api(release, store, root):
    credentials = root / "principals.json"
    values = {"acceptance-observer-token": {"actor": "Observer", "roles": ["observer"]},
              "acceptance-operator-one-token": {"actor": "Operator One", "roles": ["operator"]},
              "acceptance-operator-two-token": {"actor": "Operator Two", "roles": ["operator"]}}
    credentials.write_text(json.dumps(values))
    credentials.chmod(0o600)
    process = subprocess.Popen([str(release / "engine"), "--database", str(store.file), "serve",
                                "--port", "0", "--credentials", str(credentials)], stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "operator API startup timed out"
        address = json.loads(process.stdout.readline())
        authority = f"http://{address['address']}:{address['port']}"

        def send(endpoint, token=None, event=None, status=200):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            wire = Request(authority + "/v1/" + endpoint, data=json.dumps({"fence": 2, "event": event or {}}).encode(), headers=headers)
            try:
                with urlopen(wire, timeout=5) as reply:
                    assert reply.status == status
                    return json.load(reply)
            except HTTPError as exc:
                assert exc.code == status, (exc.code, status)
                return json.load(exc)

        viewer, proposer, approver = values
        send("status", status=401)
        send("kill", viewer, status=403)
        assert send("status", viewer)["status"] == "status"
        req = {"adjustment-id": CLIENT.format(5), "asset": "USD", "amount": 100,
               "reason": "Independent cash adjustment acceptance"}
        assert send("propose-adjustment", proposer, req)["status"] == "adjustment-proposed"
        assert send("approve-adjustment", proposer, req)["status"] == "invalid-adjustment"
        assert send("approve-adjustment", approver, req)["status"] == "adjusted"
        log = send("journal", viewer)["data"]
        assert log[0]["identity"] == {"actor": "Operator One", "approver": "Operator Two", "reason": req["reason"]}
        return 7
    finally:
        process.terminate()
        process.wait(timeout=10)


def versions(original, mutant, work):
    database = work / "versions.sqlite"
    old_release = json.loads((original / "ownership.json").read_text())["release"]
    new_release = json.loads((mutant / "ownership.json").read_text())["release"]
    assert old_release != new_release

    def call(release, entry, expected, event=None):
        result = json.loads(command(release, database, "call", entry, "--fence", "1", payload=event).stdout)
        assert result["status"] == expected, (entry, result, expected)
        return result["data"]

    call(original, "acquire-writer", "reconciliation-required", {"fence": 1})
    call(original, "start", "initialized", {"accounts": accounts("100")})
    call(original, "reconcile", "reconciled", {"from-cursor": 0, "to-cursor": 0, "fills-complete": True,
                                              "open-orders": [], "accounts": accounts("100")})
    call(original, "resume", "trading")
    other = "BCH-USD"
    for product in (PRODUCT, other):
        call(original, "metadata", "metadata", {"product": product, "body": fixture(product, "product")})
        call(original, "snapshot", "book", {"product": product, "body": fixture(product, "book")})
    call(original, "limits", "limits-updated", {"ceiling": 100 * 10**16, "fee-ppm": 200_000})
    prior, candidate, comparison = (CLIENT.format(i) for i in range(3))
    call(original, "admit", "admitted", intent(prior, 5_000))
    call(original, "claim", "claimed", {"client-id": prior})
    call(original, "send-result", "acknowledged", {"client-id": prior, "outcome": "accepted",
                                                    "body": order(prior, VENUE.format(0), "50.00")})
    call(original, "cancel", "cancel-pending", {"client-id": prior})
    assert call(original, "admit", "admitted", intent(candidate, 500))["origin-release"] == old_release
    call(original, "cancel", "cancelled", {"client-id": candidate})
    policy = {"product": other, "lower": 0, "upper": 200_000, "lifetime": 500,
              "max-age": 60_000, "enabled": True, "clip": 10_000}
    call(original, "configure-strategy", "strategy-configured", policy)
    query = {"product": other, "client-id": candidate}
    call(original, "strategy", "within-target-band", query)
    assert command(mutant, database, "call", "status", check=False).returncode == 1
    assert call(mutant, "activate-release", "release-activated") == {"actor": pwd.getpwuid(os.geteuid()).pw_name,
                                                                          "from": old_release, "to": new_release, "schema": 1}
    assert call(mutant, "order", "order", {"client-id": prior})["origin-release"] == old_release
    call(mutant, "admit", "risk-ceiling", intent(comparison, 500))
    assert call(mutant, "strategy", "intent", query)["size"] == 10_000
    mixed = json.loads(command(mutant, database, "replay", "--pinned", str(original)).stdout)
    call(original, "activate-release", "release-activated")
    call(original, "strategy", "within-target-band", query)
    call(original, "admit", "admitted", intent(comparison, 500))
    assert call(original, "order", "order", {"client-id": prior})["reservation"] == 60 * 10**16
    rolled = json.loads(command(original, database, "replay", "--pinned", str(mutant)).stdout)
    assert rolled["events"] > mixed["events"] and mixed["state_digest"] != rolled["state_digest"]
    return {"activation_events": mixed["events"], "rollback_events": rolled["events"],
            "source_mutations": ["inventory target", "pending-cancel risk exposure"]}
