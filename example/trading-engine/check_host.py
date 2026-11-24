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

from trading_checks import CLIENT, MANIFEST, PRODUCT, VENUE, accounts, fill, fixture, intent, order
from event_host import Store
from oracle import QUOTE_SCALE, balances_from_statement
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
        if observed == "after-authorization" and "prewire" in args:
            Path(args["prewire"]).touch()
            wait_file(Path(args["unblock"]))

    if args["mode"] == "admit":
        result = store.invoke("admit", intent(args["client"], args["price"]), time_ms=args["time"],
                              fence=1, actor="acceptance", roles=roles, fault=fault)
        os._exit({"admitted": 80, "insufficient-available": 81}[result["status"]])

    def accept(request, _):
        raw = request["body"]
        body = order(raw["client_oid"], VENUE.format(1), raw["price"], raw["size"])
        observed = Path(args["exchange"])
        staged = observed.with_suffix(".pending")
        with staged.open("x") as file:
            file.write(json.dumps(body))
            file.flush()
            os.fsync(file.fileno())
        staged.replace(observed)
        if "unblock" in args and "prewire" not in args:
            wait_file(Path(args["unblock"]))
        return {"network": "response", "status": 200, "body": body}

    result = store.send({"client-id": args["client"]}, None, fence=1, actor="acceptance", roles=roles,
                        effect=accept, fault=fault, clock=lambda: args["time"])
    if "reply" not in args:
        raise RuntimeError("the injected process failure did not occur")
    Path(args["reply"]).write_text(json.dumps(result))


def wait_file(path, process=None):
    end = time.monotonic() + 15
    while not path.exists():
        if (process is not None and process.poll() is not None) or time.monotonic() >= end:
            raise AssertionError(f"process did not signal {path.name}")
        time.sleep(0.01)


def prepared_case(entries, manifest, version, database):
    store = Store(database, manifest, entries, version)
    roles = {role for target in manifest["access"].values() for role in target["roles"]}
    now = 1000

    def clock():
        nonlocal now
        now += 200
        return now

    def invoke(entry, event, expected, fence=1):
        reply = store.invoke(entry, event, time_ms=clock(), fence=fence, actor="acceptance", roles=roles)
        assert reply["status"] == expected, (entry, reply, expected)
        return reply["data"]

    opening = accounts("100")
    invoke("acquire-writer", {"fence": 1}, "reconciliation-required")
    invoke("start", {"accounts": opening}, "initialized")
    invoke("reconcile", {"from-cursor": 0, "to-cursor": 0, "fills-complete": True,
                         "open-orders": [], "accounts": opening}, "reconciled")
    invoke("resume", {}, "trading")
    invoke("metadata", {"product": PRODUCT, "body": fixture(PRODUCT, "product")}, "metadata")
    invoke("snapshot", {"product": PRODUCT, "body": fixture(PRODUCT, "book")}, "book")
    return store, invoke, clock


def transport_race(release, entries, manifest, version, root):
    database = root / "stalled.sqlite"
    store, invoke, clock = prepared_case(entries, manifest, version, database)
    active, staged = (CLIENT.format(i) for i in (1, 2))
    invoke("admit", intent(active, 2_500), "admitted")
    invoke("admit", intent(staged, 2_500), "admitted")
    unstarted = invoke("claim", {"client-id": staged}, "claimed")
    exchange, unblock, reply = (root / name for name in ("stalled.venue", "stalled.unblock", "stalled.reply"))
    process = child(release, database, {"mode": "send", "client": active, "exchange": str(exchange),
                "unblock": str(unblock), "reply": str(reply), "time": clock()})
    try:
        wait_file(exchange, process)
        accepted = json.loads(exchange.read_text())
        kill = invoke("kill", {}, "cancel-only")
        assert kill["outstanding"] == [active] and kill["reserved"]["USD"] == 2_525 * (QUOTE_SCALE // 100)
        invoke("authorize-send", unstarted, "transport-fenced")
        assert invoke("order", {"client-id": staged}, "order")["phase"] == "cancelled"
        invoke("claim", {"client-id": active}, "cancel-awaiting-identity")
        invoke("order-evidence", {"client-id": active, "body": accepted}, "cancel-pending")
        first = fill(accepted["id"], 1, "25", "0.4", "0.10")
        invoke("fill", {"body": first}, "cancel-pending")
        invoke("fill", {"body": first}, "duplicate-fill")
        assert process.poll() is None and not reply.exists(), "the transport must still be stalled"
        unblock.touch()
        assert process.wait(timeout=20) == 0
        observed = json.loads(reply.read_text())
        assert observed["status"] == "cancel-pending" and observed["data"]["remaining"] == 60_000_000
    finally:
        unblock.touch()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    cancel = invoke("claim", {"client-id": active}, "claimed")
    invoke("authorize-send", cancel, "transport-authorized")
    last = fill(accepted["id"], 2, "25", "0.6", "0.15")
    invoke("fill", {"body": last}, "fully-filled")
    outcome = {**cancel, "response": {"network": "response", "status": 200, "body": active}}
    invoke("http-result", outcome, "already-terminal")
    invoke("http-result", outcome, "stale-transport-result")
    status = invoke("status", {}, "status")
    expected = balances_from_statement({"USD": "100", "BTC": "0", "BCH": "0"}, [first, last])
    assert status["mode"] == "cancel-only" and status["outstanding"] == []
    assert {asset: value["total"] for asset, value in status["balances"].items()} == expected
    assert status["balances"]["USD"]["reserved"] == 0
    return store.replay({version: entries})["events"]


def transport_crashes(release, entries, manifest, version, root):
    scenarios = [("after-claim", False, "sent-unknown", False, "pending-send"),
                 ("after-authorization", False, "sent-unknown", True, "sent-unknown"),
                 ("before-result-commit", True, "sent-unknown", True, "sent-unknown"),
                 ("after-result-commit", True, "acknowledged", True, "acknowledged")]
    for code, (point, reached_venue, phase, submitted, recovered_phase) in enumerate(scenarios, 76):
        database = root / f"{point}.sqlite"
        exchange = root / f"{point}.venue"
        store, invoke, clock = prepared_case(entries, manifest, version, database)
        client = CLIENT.format(1)
        invoke("admit", intent(client, 2_500), "admitted")
        process = child(release, database, {"mode": "send", "client": client, "exchange": str(exchange),
                        "time": clock(), "at": point, "code": code})
        assert process.wait(timeout=20) == code
        actual = invoke("order", {"client-id": client}, "order")
        assert actual["phase"] == phase and actual["submitted"] is submitted
        assert exchange.exists() is reached_venue
        invoke("acquire-writer", {"fence": 2}, "reconciliation-required", fence=2)
        actual = invoke("order", {"client-id": client}, "order")
        assert actual["phase"] == recovered_phase and actual["reservation"] > 0
        if reached_venue and phase == "sent-unknown":
            evidence = {"client-id": client, "body": json.loads(exchange.read_text())}
            invoke("order-evidence", evidence, "acknowledged", fence=2)
        assert store.replay({version: entries})["events"] >= 9
    return len(scenarios)


def kill_before_wire(release, entries, manifest, version, root):
    database = root / "prewire.sqlite"
    store, invoke, clock = prepared_case(entries, manifest, version, database)
    client = CLIENT.format(1)
    invoke("admit", intent(client, 2_500), "admitted")
    ready, exchange, unblock, reply = (
        root / f"prewire.{name}" for name in ("ready", "venue", "unblock", "reply"))
    process = child(release, database, {"mode": "send", "client": client, "exchange": str(exchange),
                "prewire": str(ready), "unblock": str(unblock), "reply": str(reply), "time": clock()})
    try:
        wait_file(ready, process)
        assert not exchange.exists()
        kill = invoke("kill", {}, "cancel-only")
        assert kill["outstanding"] == [client] and kill["reserved"]["USD"] > 0
        invoke("claim", {"client-id": client}, "cancel-awaiting-identity")
        invoke("admit", intent(CLIENT.format(2), 2_500), "admission-fenced")
        assert process.poll() is None and not exchange.exists()
        unblock.touch()
        assert process.wait(timeout=20) == 0
        observed = json.loads(reply.read_text())
        assert observed["status"] == "cancel-pending"
        assert observed["data"]["venue-id"] == json.loads(exchange.read_text())["id"]
        cancel = invoke("claim", {"client-id": client}, "claimed")
        assert cancel["stage"] == "cancel"
    finally:
        unblock.touch()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    return store.replay({version: entries})["events"]


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
    assert report["events"] == 17
    backup = root / "restored.sqlite"
    assert json.loads(command(release, database, "backup", str(backup)).stdout) == {"backup": str(backup)}
    assert json.loads(command(release, backup, "replay").stdout) == report
    assert json.loads(command(release, backup, "call", "status").stdout)["data"]["outstanding"]
    auth_events = api(release, store, root)
    stalled_events = transport_race(release, entries, manifest, version, root)
    additional_deaths = transport_crashes(release, entries, manifest, version, root)
    prewire_events = kill_before_wire(release, entries, manifest, version, root)
    return {"durable_events": report["events"], "process_deaths": 3 + additional_deaths,
            "stalled_send_events": stalled_events, "kill_before_wire_events": prewire_events, "api_checks": auth_events,
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
    work = call(original, "claim", "claimed", {"client-id": prior})
    call(original, "authorize-send", "transport-authorized", work)
    call(original, "send-result", "acknowledged", {**work, "outcome": "accepted",
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
