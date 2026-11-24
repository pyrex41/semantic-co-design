"""Run the deterministic public trading-engine acceptance histories."""

import importlib.util
from functools import lru_cache
from decimal import Decimal
import json
import shutil
import subprocess
from pathlib import Path
import sys
import sysconfig
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))
import appc
from oracle import QUOTE_SCALE, balances_from_statement, book_checkpoint


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
PRODUCT = "BTC-USD"
SECOND_PRODUCT = "BCH-USD"
CLIENT = "10000000-1234-4abc-8123-01234567890{}"
VENUE = "20000000-1234-4abc-8123-01234567890{}"


def accounts(usd, btc="0", bch="0"):
    return [{"currency": currency, "balance": text, "available": text, "hold": "0"}
            for currency, text in zip(("USD", "BTC", "BCH"), (usd, btc, bch))]


def order(client, venue, price, size="1", filled="0", status="open", reason=None, side="buy"):
    return {"id": venue, "client_oid": client, "product_id": PRODUCT, "side": side, "type": "limit",
            "price": price, "size": size, "filled_size": filled, "status": status,
            "done_reason": reason, "settled": status in {"done", "settled", "rejected"}}


def fill(venue, trade, price, size, fee, side="buy"):
    return {"product_id": PRODUCT, "order_id": venue, "trade_id": trade, "price": price,
            "size": size, "fee": fee, "side": side, "settled": True}


def intent(client, price, size=100_000_000, lifetime=20_000, product=PRODUCT, side="buy"):
    return {"client-id": client, "product": product, "side": side, "price": price, "size": size,
            "lifetime": lifetime}


def fixture(product, kind):
    return json.loads((ROOT / "fixtures" / f"{product}.{kind}.json").read_text())


class Session:
    def __init__(self, entries):
        self.entries = entries
        self.state = None
        self.fence = 1
        self.time = 1
        self.release = "acceptance-r1"
        self.trace = []

    def run(self, entry, expected, event=None, *, actor="test-runner", fence=None, step=200):
        self.time += step
        envelope = {"event": event or {}, "time": self.time, "release": self.release, "actor": actor,
                    "fence": self.fence if fence is None else fence, "state": self.state}
        reply = self.entries[entry](None, envelope)
        assert reply["status"] == expected, (entry, expected, reply["status"], reply["data"])
        self.state = reply["state"]
        self.trace.append((entry, {k: v for k, v in envelope.items() if k != "state"}, reply["status"], reply["data"]))
        return reply["data"]

    def dispatch(self, client):
        work = self.run("claim", "claimed", {"client-id": client})
        self.run("authorize-send", "transport-authorized", work, step=0)
        return work

    def product(self, product):
        metadata = fixture(product, "product")
        self.run("metadata", "metadata", {"product": product, "body": metadata})
        return self.snapshot(product)

    def snapshot(self, product):
        body = fixture(product, "book")
        view = self.run("snapshot", "book", {"product": product, "body": body})
        assert {name: view[name] for name in book_checkpoint(body)} == book_checkpoint(body), view
        return view

    def statement(self, status, usd, *, btc="0", bch="0", open_ids=(), cursor=0, start=0, complete=True):
        rows = []
        for key in open_ids:
            site = self.state["venue-orders"].get(key)
            if site is None:
                rows.append({"id": key})
                continue
            local = self.state["orders"][site]
            rows.append(order(site, key, str(Decimal(local["price"]) / 100),
                              str(Decimal(local["size"]) / 10**8),
                              str(Decimal(local["size"] - local["remaining"]) / 10**8), side=local["side"]))
        return self.run("reconcile", status, {"from-cursor": start, "to-cursor": cursor,
                        "fills-complete": complete, "open-orders": rows, "accounts": accounts(usd, btc, bch)})

    def start(self, usd="100", btc="0", bch="0"):
        self.run("acquire-writer", "reconciliation-required", {"fence": self.fence})
        self.run("start", "initialized", {"accounts": accounts(usd, btc, bch)})
        self.statement("reconciled", usd, btc=btc, bch=bch)
        self.run("resume", "trading")
        self.product(PRODUCT)
        self.product(SECOND_PRODUCT)

    def balance(self, asset, total, reserved):
        report = self.run("status", "status")["balances"][asset]
        assert report == {"total": total, "reserved": reserved, "available": total-reserved}, report

    def invariant(self):
        held = dict.fromkeys(self.state["balances"], 0)
        for order_state in self.state["orders"].values():
            held[order_state["reserve-asset"]] += order_state["reservation"]
        assert held == self.state["reserved"]
        assert all(self.state["balances"][asset] >= value for asset, value in held.items())
        for posting in self.state["journal"]:
            if posting["kind"] == "release-activation":
                assert posting["postings"] == []
                continue
            assert sum(row[2] for row in posting["postings"]) == 0
            assert len({row[0] for row in posting["postings"]}) == 1


class Venue:
    """Independent mock sink. Track actually posted client identities."""
    def __init__(self):
        self.orders = {}
        self.posts = []
        self.cancels = []

    def accept(self, request, venue_id):
        assert request["method"] == "POST" and request["path"] == "/orders" and request["private"]
        raw = request["body"]
        assert raw["type"] == "limit" and raw["product_id"] == PRODUCT
        client = raw["client_oid"]
        assert client not in self.orders
        self.posts.append(client)
        self.orders[client] = order(client, venue_id, raw["price"], raw["size"], side=raw["side"])
        return self.orders[client]

    def lookup(self, request, client):
        assert request["method"] == "GET" and request["path"] == "/orders/client:" + client
        return self.orders.get(client)

    def cancel(self, request, client):
        assert request["method"] == "DELETE" and request["path"] == "/orders/client:" + client
        assert request["query"] == {"product_id": PRODUCT}
        self.cancels.append(client)
        return client


def decisive(entries):
    engine, venue = Session(entries), Venue()
    engine.start()
    unit = QUOTE_SCALE
    previous, winner, loser, fresh = (CLIENT.format(i) for i in range(4))
    prev_venue, winner_venue, fresh_venue = (VENUE.format(i) for i in (0, 1, 3))
    engine.run("limits", "limits-updated", {"ceiling": 100 * unit, "fee-ppm": 200_000})
    engine.run("admit", "admitted", intent(previous, 5_000))
    payload = engine.dispatch(previous)
    initial = venue.accept(payload["request"], prev_venue)
    engine.run("send-result", "acknowledged", {**payload, "outcome": "accepted", "body": initial})
    engine.balance("USD", 100 * unit, 60 * unit)
    engine.run("admit", "admitted", intent(winner, 2_500))
    engine.run("admit", "insufficient-available", intent(loser, 2_500))
    engine.balance("USD", 100 * unit, 90 * unit)
    request = engine.dispatch(winner)
    assert request["stage"] == "send" and request["request"]["body"]["price"] == "25.00"
    accepted = venue.accept(request["request"], winner_venue)
    assert venue.posts == [previous, winner]

    engine.state = json.loads(json.dumps(engine.state))
    engine.fence += 1
    engine.run("acquire-writer", "reconciliation-required", {"fence": engine.fence})
    engine.run("claim", "stale-fence", {"client-id": winner}, fence=1)
    engine.run("claim", "no-work", {"client-id": winner})
    engine.run("admit", "admission-fenced", intent(fresh, 500))
    engine.run("resume", "resume-blocked")
    engine.statement("open-orders-unexplained", "100", open_ids=(prev_venue, winner_venue))
    lookup = engine.run("wire", "wire-request", {"kind": "lookup", "client-id": winner})
    assert venue.lookup(lookup, winner) == accepted
    engine.run("order-evidence", "acknowledged", {"client-id": winner, "body": accepted})
    engine.statement("reconciled", "100", open_ids=(prev_venue, winner_venue))
    engine.snapshot(PRODUCT)
    engine.run("resume", "trading")

    first = fill(winner_venue, 41, "25.00", "0.40000000", "0.1500000000000000")
    last = fill(winner_venue, 42, "25.00", "0.60000000", "0.2500000000000000")
    session_fills = [first, first, last]
    engine.run("fill", "partially-filled", {"body": first})
    engine.balance("USD", 100 * unit - 1015 * (unit // 100), 60 * unit + 1985 * (unit // 100))
    engine.run("fill", "duplicate-fill", {"body": first})
    engine.run("cancel", "cancel-pending", {"client-id": winner})
    engine.balance("USD", 100 * unit - 1015 * (unit // 100), 60 * unit + 1985 * (unit // 100))
    cancel = engine.dispatch(winner)
    assert cancel["stage"] == "cancel"
    cancel_receipt = venue.cancel(cancel["request"], winner)
    engine.run("cancel-result", "cancel-awaiting-proof", {**cancel, "outcome": "accepted", "body": cancel_receipt})
    engine.run("fill", "fully-filled", {"body": last})
    engine.run("cancel-result", "stale-transport-result", {**cancel, "outcome": "unknown"})
    engine.run("fill", "duplicate-fill", {"body": last})
    journal = engine.run("journal", "journal")
    executions = engine.run("executions", "executions")
    assert len(executions) == 2
    assert [item["kind"] for item in journal[:6]] == ["fee", "fill-quote", "fill-base"] * 2
    expected = balances_from_statement({"USD": "100", "BTC": "0", "BCH": "0"}, session_fills)
    assert engine.state["balances"] == expected
    engine.balance("USD", expected["USD"], 60 * unit)
    assert engine.run("order", "order", {"client-id": winner})["phase"] == "fully-filled"
    engine.statement("balance-difference", "74.6000000000000001", btc="1", open_ids=(prev_venue,), cursor=42)
    engine.run("admit", "admission-fenced", intent(fresh, 500))
    engine.statement("reconciled", "74.60", btc="1", open_ids=(prev_venue,), cursor=42)
    engine.run("resume", "trading")
    engine.run("admit", "admitted", intent(fresh, 500))
    work = engine.dispatch(fresh)
    venue.accept(work["request"], fresh_venue)
    engine.run("send-result", "send-outcome-unknown", {**work, "outcome": "unknown"})
    engine.run("order-evidence", "missing-order-proof", {"client-id": fresh, "body": None})
    engine.run("resume", "resume-blocked")
    engine.invariant()
    assert venue.posts == [previous, winner, fresh] and venue.cancels == [winner]
    return engine


def market_history(entries):
    engine = Session(entries)
    engine.start()
    raw = fixture(PRODUCT, "book")
    sequence = raw["sequence"]
    top = raw["bids"][0]
    match = {"type": "match", "product_id": PRODUCT, "sequence": sequence + 1,
             "maker_order_id": top[2], "size": top[1], "side": "buy", "price": top[0]}
    removed = {**raw, "bids": raw["bids"][1:], "sequence": sequence + 1}
    fresh = engine.run("delta", "book", {"body": match})
    assert {name: fresh[name] for name in book_checkpoint(removed)} == book_checkpoint(removed)
    assert engine.run("delta", "book", {"body": match}) == fresh
    done = {"type": "done", "product_id": PRODUCT, "sequence": sequence + 2, "order_id": "never-rested"}
    assert engine.run("delta", "book", {"body": done})["orders"] == fresh["orders"]
    gap = {**done, "sequence": sequence + 4}
    assert not engine.run("delta", "resnapshot-required", {"body": gap})["valid"]
    engine.run("admit", "stale-market", intent(CLIENT.format(1), 6_000))
    seeded = {**raw, "sequence": sequence + 3}
    engine.run("snapshot", "book", {"product": PRODUCT, "body": seeded})
    engine.run("delta", "book", {"body": gap})
    engine.run("delta", "resnapshot-required", {"body": {**done, "sequence": sequence + 5, "type": "unrecognized"}})
    engine.run("disconnect", "resnapshot-required")
    engine.run("snapshot", "book", {"product": PRODUCT, "body": {**raw, "sequence": sequence + 5}})
    revision = {**fixture(PRODUCT, "product"), "base_increment": "0.000001", "quote_increment": "0.05"}
    engine.run("metadata", "metadata", {"product": PRODUCT, "body": revision})
    engine.run("admit", "invalid-increment", intent(CLIENT.format(1), 6_001))
    engine.run("admit", "invalid-increment", intent(CLIENT.format(1), 6_000, 100_000_001))
    engine.run("admit", "invalid-or-overflowing-amount", intent(CLIENT.format(1), 1_000_000_000))
    engine.run("admit", "invalid-or-overflowing-amount", intent(CLIENT.format(1), True))
    engine.run("disable", "instrument-disabled", {"product": PRODUCT})
    engine.run("metadata", "metadata", {"product": PRODUCT, "body": revision})
    engine.run("admit", "instrument-disabled", intent(CLIENT.format(1), 6_000))
    engine.snapshot(SECOND_PRODUCT)
    old = {"type": "received", "product_id": SECOND_PRODUCT, "sequence": fixture(SECOND_PRODUCT, "book")["sequence"] + 1}
    engine.run("venue-frame", "book", {"body": old, "live": True, "wire-time": 1}, step=61_000)
    engine.run("admit", "stale-market", intent(CLIENT.format(2), 600, product=SECOND_PRODUCT))
    engine.run("venue-frame", "feed-resnapshot", {"body": old, "live": True, "wire-time": None})
    return engine


def lifecycle(entries):
    engine = Session(entries)
    engine.start(usd="100", btc="2")
    first, second, third = (CLIENT.format(i) for i in (1, 2, 3))
    engine.run("admit", "admitted", intent(first, 5_000, side="sell"))
    request = engine.run("claim", "claimed", {"client-id": first})
    assert request["request"]["endpoint"] == "https://api-public.sandbox.exchange.coinbase.com"
    assert request["request"]["body"]["side"] == "sell"
    engine.run("authorize-send", "transport-authorized", request, step=0)
    engine.run("http-result", "pending-send", {**request, "response": {"network": "not-sent", "status": 0}})
    retry = engine.dispatch(first)
    assert request["attempt"] != retry["attempt"]
    engine.run("http-result", "stale-transport-result", {**request, "response": {"network": "not-sent", "status": 0}})
    body = order(first, VENUE.format(1), "50", side="sell")
    engine.run("send-result", "acknowledged", {**retry, "outcome": "accepted", "body": body})
    sold = fill(VENUE.format(1), 10, "51.00", "0.50", "0.10", "sell")
    assert engine.run("fill", "unsettled-fill", {"body": {**sold, "settled": False}}) is None
    engine.run("fill", "partially-filled", {"body": sold})
    engine.balance("BTC", 150_000_000, 50_000_000)
    engine.run("fill", "contradictory-fill", {"body": {**sold, "fee": "0.20"}})
    engine.run("resume", "resume-blocked")
    engine.statement("history-incomplete", "125.40", btc="1.5", open_ids=(VENUE.format(1),), complete=False)
    engine.statement("reconciled", "125.40", btc="1.5", open_ids=(VENUE.format(1),), cursor=10)
    engine.run("resume", "trading")
    engine.run("admit", "admitted", intent(second, 1_000))
    work = engine.dispatch(second)
    engine.run("send-result", "rejected", {**work, "outcome": "rejected"})
    engine.run("admit", "admitted", intent(third, 5_000, lifetime=300))
    assert third not in engine.run("tick", "ticked", step=500)
    assert engine.run("order", "order", {"client-id": third})["phase"] == "cancelled"
    control = engine.run("kill", "cancel-only")
    assert control["outstanding"] == [first]
    engine.run("resume", "resume-blocked")
    engine.run("rotate", "reconciliation-required", {"epoch": 2})
    engine.fence = 2
    engine.run("acquire-writer", "reconciliation-required", {"fence": 2})
    engine.statement("reconciled", "125.40", btc="1.5", open_ids=(VENUE.format(1),), cursor=10)
    engine.run("resume", "resume-blocked")
    terminal = order(first, VENUE.format(1), "50", filled="0.50", side="sell", status="done", reason="canceled")
    engine.run("order-evidence", "incomplete-terminal-evidence", {"client-id": first, "body": terminal})
    engine.run("order-evidence", "cancelled", {"client-id": first, "body": terminal, "fills-complete": True})
    unknown_hold = accounts("125.40", btc="1.5")
    unknown_hold[0].update({"hold": "0.20", "available": "125.20"})
    engine.run("reconcile", "unexplained-venue-hold", {"from-cursor": 10, "to-cursor": 10,
               "fills-complete": True, "open-orders": [], "accounts": unknown_hold})
    engine.statement("reconciled", "125.40", btc="1.5", cursor=10)
    engine.run("resume", "trading")
    engine.balance("BTC", 150_000_000, 0)
    engine.run("order-evidence", "venue-reopened-terminal-order", {"client-id": first,
               "body": {**terminal, "status": "open", "done_reason": None}})
    engine.run("resume", "resume-blocked")
    engine.invariant()
    return engine


def venue_history(entries):
    engine = Session(entries)
    engine.start()
    raw = fixture(PRODUCT, "book")
    ids = [CLIENT.format(1), VENUE.format(1)]
    client, venue = ids
    engine.run("admit", "admitted", intent(client, 6_000_000, 10_000))
    engine.dispatch(client)
    request = engine.run("wire", "wire-request", {"kind": "subscribe"})
    assert request["body"]["product_ids"] == [PRODUCT, SECOND_PRODUCT]
    subscribe = {"type": "subscriptions", "channels": [{"name": "full", "product_ids": [PRODUCT, SECOND_PRODUCT]}]}
    work = engine.run("venue-frame", "feed-subscribed", {"body": subscribe})
    assert [event["kind"] for event in work] == ["product", "snapshot", "product", "snapshot"]
    receipt = {"type": "received", "product_id": PRODUCT, "sequence": raw["sequence"] + 1,
               "client-oid": client, "order_id": venue, "price": "60000.00", "size": "0.00010000", "side": "buy"}
    engine.run("venue-frame", "book", {"body": receipt})
    assert engine.run("order", "order", {"client-id": client})["phase"] == "acknowledged"
    maker_price, _, maker_id = raw["asks"][0]
    frame = {"type": "match", "product_id": PRODUCT, "sequence": raw["sequence"] + 2,
             "maker_order_id": maker_id, "taker_order_id": venue, "trade_id": 3,
             "price": maker_price, "size": "0.00010000", "side": "sell"}
    work = engine.run("venue-frame", "feed-fills", {"body": frame})[0]
    request = engine.run("wire", "wire-request", work)
    assert request["query"]["order_id"] == venue and request["path"] == "/fills"
    report = fill(venue, 3, maker_price, "0.00010000", "0.01")
    engine.run("rest-result", "fill-page-observed", {**work, "response": {"network": "response", "status": 200, "body": [report]}})
    assert engine.run("order", "order", {"client-id": client})["phase"] == "fully-filled"
    assert engine.state["balances"] == balances_from_statement({"USD": "100", "BTC": "0", "BCH": "0"}, [report])
    engine.invariant()
    return engine


def strategy_history(entries, changed=False):
    engine = Session(entries)
    engine.start()
    client = CLIENT.format(1)
    configuration = {"product": PRODUCT, "lower": 0, "upper": 200_000, "lifetime": 500,
                     "max-age": 60_000, "enabled": True, "clip": 10_000}
    engine.run("configure-strategy", "strategy-configured", configuration)
    req = {"product": PRODUCT, "client-id": client}
    forecast = engine.run("strategy", "intent" if changed else "within-target-band", req)
    if changed:
        assert forecast["size"] == 10_000 and forecast["side"] == "buy"
    engine.run("configure-strategy", "strategy-configured", {**configuration, "lower": 20_000})
    proposal = engine.run("strategy", "intent", req)
    assert proposal["price"] == book_checkpoint(fixture(PRODUCT, "book"))["bid"]
    assert proposal["size"] == 10_000 and proposal["side"] == "buy"
    engine.run("admit", "admitted", proposal)
    engine.run("strategy", "working-order", req)
    engine.run("pause", "paused")
    engine.run("strategy", "strategy-paused", req)
    engine.run("resume", "trading")
    engine.run("tick", "ticked", step=1000)
    engine.run("strategy", "stale-market", req, step=70_000)
    return engine


def operator_history(entries):
    engine = Session(entries)
    engine.start()
    assert engine.run("blotter", "blotter") == {}
    engine.statement("balance-difference", "101")
    assert engine.run("exceptions", "exceptions")[0]["reason"] == "balance-difference"
    req = {"adjustment-id": CLIENT.format(7), "asset": "USD", "amount": QUOTE_SCALE,
           "reason": "Verified statement reconciliation adjustment"}
    engine.run("propose-adjustment", "adjustment-proposed", req, actor="proposer")
    engine.run("approve-adjustment", "invalid-adjustment", req, actor="proposer")
    engine.run("approve-adjustment", "adjusted", req, actor="approver")
    assert engine.run("journal", "journal")[0]["identity"] == {
        "actor": "proposer", "approver": "approver", "reason": req["reason"],
    }
    engine.statement("reconciled", "101")
    engine.release = "acceptance-r2"
    activation = engine.run("activate-release", "release-activated", actor="release-manager")
    assert activation["to"] == engine.release and activation["schema"] == 1
    return engine


def transport_scope_history(entries):
    trace = []
    for boundary in ("fence", "epoch", "release"):
        engine = Session(entries)
        engine.start()
        client = CLIENT.format(8)
        engine.run("admit", "admitted", intent(client, 2_500))
        work = engine.dispatch(client)
        body = order(client, VENUE.format(8), "25.00")
        observation = {**work, "response": {"network": "response", "status": 200, "body": body}}
        if boundary == "fence":
            engine.fence = 2
            engine.run("acquire-writer", "reconciliation-required", {"fence": 2})
            engine.run("http-result", "stale-fence", observation, fence=1)
        elif boundary == "epoch":
            engine.run("rotate", "reconciliation-required", {"epoch": 2})
        else:
            first_release = engine.release
            engine.release = "acceptance-r2"
            engine.run("activate-release", "release-activated")
            engine.run("http-result", "stale-transport-result", observation)
            engine.release = first_release
            engine.run("activate-release", "release-activated")
        engine.run("http-result", "stale-transport-result", observation)
        parked = engine.run("order", "order", {"client-id": client})
        assert parked["phase"] == "sent-unknown" and parked["venue-id"] is None and parked["reservation"] > 0
        assert engine.run("status", "status")["mode"] == "reconciliation-required"
        engine.run("order-evidence", "acknowledged", {"client-id": client, "body": body})
        engine.invariant()
        trace.extend(engine.trace)
    engine.trace = trace
    return engine


@lru_cache(maxsize=1)
def import_native(build):
    lib = build / "lib" / ("_application" + sysconfig.get_config_var("EXT_SUFFIX"))
    loader = importlib.util.spec_from_file_location("_application", lib)
    native = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(native)
    return native.entries


def native_acceptance(build):
    import package

    manifest, evaluator = appc.load(MANIFEST)
    reference = appc.reference_entries(manifest, evaluator)
    native = import_native(build)
    checks = [decisive, market_history, lifecycle, venue_history, strategy_history, operator_history, transport_scope_history]
    count, covered = 0, set()
    for history in checks:
        expected = history(reference)
        actual = history(native)
        assert expected.trace == actual.trace and expected.state == actual.state, history.__name__
        count += len(actual.trace)
        covered.update(event for event, *_ in actual.trace)
    assert covered == set(native)
    ownership = json.loads((build / "ownership.json").read_text())
    assert set(ownership["entries"]) == set(native)
    assert all(ownership["definitions"][name]["residual"] for name in ("tx.admit", "tx.fill", "tx.delta", "tx.inventory-direction"))
    source = (build / "_application.pyx").read_text()
    assert "from values import" in source and "eval(" not in source and "ctx.effect(" not in source
    return {"public_histories": len(checks), "observations": count, "native_entries": len(native),
            "native_bytes": next((build / "lib").glob("_application*.so")).stat().st_size,
            "revision": package.revision(MANIFEST, manifest)}


def main():
    sys.modules.setdefault("trading_checks", sys.modules[__name__])
    import check_host
    import package

    sys.setrecursionlimit(20_000)
    start = time.monotonic()
    if len(sys.argv) == 2:
        print(json.dumps(native_acceptance(Path(sys.argv[1])), sort_keys=True))
    else:
        with tempfile.TemporaryDirectory(prefix="trading-engine-") as root:
            build = Path(root) / "release"
            private = Path(root) / "signing.pem"
            public = Path(root) / "signing.pub"
            subprocess.run(["openssl", "genrsa", "-out", str(private), "2048"],
                           capture_output=True, check=True, timeout=10)
            subprocess.run(["openssl", "rsa", "-in", str(private), "-pubout", "-out", str(public)],
                           capture_output=True, check=True, timeout=10)
            package.build(MANIFEST, build, private)
            package.verify(build, public)
            results = native_acceptance(build)
            results.update(check_host.run(build, import_native(build), Path(root)))
            candidate = Path(root) / "edited-source"
            candidate.mkdir()
            manifest, _ = appc.load(MANIFEST)
            for name in ["manifest.json", *manifest["sources"], *manifest["assets"]]:
                shutil.copyfile(ROOT / name, candidate / name)
            changes = {"strategy.shen": ("Lower -> Lower", "Lower -> (+ Lower 10000)"),
                       "account.shen": ("(define tx.pending-cancel-weight\n  -> 1)",
                                        "(define tx.pending-cancel-weight\n  -> 2)")}
            for name, (before, after) in changes.items():
                source = candidate / name
                old = source.read_text()
                assert old.count(before) == 1
                source.write_text(old.replace(before, after))
            candidate_manifest = candidate / "manifest.json"
            changed, evaluator = appc.load(candidate_manifest)
            strategy_history(appc.reference_entries(changed, evaluator), changed=True)
            mutant = Path(root) / "mutant"
            package.build(candidate_manifest, mutant)
            results.update(check_host.versions(build, mutant, Path(root)))
            results["signed_package"] = True
            print(json.dumps({**results, "elapsed_seconds": round(time.monotonic() - start, 3)}, sort_keys=True))


if __name__ == "__main__":
    main()
