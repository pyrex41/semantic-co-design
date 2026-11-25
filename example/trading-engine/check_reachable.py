"""Check independent accounting laws on bounded histories of the generated engine."""

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

if "trading_checks" not in sys.modules:
    import check as trading_checks
    sys.modules["trading_checks"] = trading_checks

from trading_checks import CLIENT, MANIFEST, VENUE, Session, accounts, appc, fill, import_native, intent, order
from values import digest, same


LAWS = Path(__file__).with_name("invariants.json")


class LedgerLaws:
    def __init__(self, work):
        manifest, evaluator = appc.load(LAWS)
        out = work / "ledger-laws"
        appc.build(LAWS, out)
        self.reference = appc.reference_entries(manifest, evaluator)["ledger-laws"]
        self.native = import_native(out)["ledger-laws"]
        self.observations = 0

    def check(self, before, entry, reply, expected="holds", label=None):
        observation = {"before": before, "entry": entry, "status": reply["status"], "after": reply["state"]}
        checks = self.evaluate(observation)
        if checks != (expected, expected):
            raise AssertionError((label, entry, reply["status"], expected, checks, observation))
        self.observations += 1

    def evaluate(self, observation):
        return self.reference(None, observation), self.native(None, observation)

    def wrap(self, entries):
        def observed(name, entry):
            def invoke(ctx, envelope):
                result = entry(ctx, envelope)
                self.check(envelope["state"], name, result, label=envelope["event"])
                return result
            return invoke
        return {name: observed(name, entry) for name, entry in entries.items()}


def pair_step(reference, native, laws, before, entry, event, label=None):
    expected = reference[entry](None, {**event, "state": before})
    actual = native[entry](None, {**event, "state": before})
    if not same(expected, actual):
        raise AssertionError((label, entry, event, expected, actual))
    laws.check(before, entry, expected, label=label or event)
    return actual


def preopening(entries):
    """Opening the account cannot erase an earlier financial posting."""
    engine = Session(entries)
    engine.run("acquire-writer", "reconciliation-required", {"fence": 1})
    adjustment = {"adjustment-id": CLIENT.format(6), "asset": "USD", "amount": 10**16,
                  "reason": "Verified deposit appeared on the opening statement"}
    engine.run("propose-adjustment", "opening-statement-required", adjustment, actor="proposer")
    engine.run("approve-adjustment", "opening-statement-required", adjustment, actor="approver")
    engine.run("activate-release", "release-activated")
    engine.run("start", "initialized", {"accounts": accounts("100")})
    return engine


def legacy_opening_conflict(reference, native, laws):
    seed = Session(laws.wrap(reference))
    seed.run("acquire-writer", "reconciliation-required", {"fence": 1})
    legacy = deepcopy(seed.state)
    adjustment = {"actor": "proposer", "reason": "Legacy proposal before the statement", "amount": 1, "asset": "USD"}
    identity = CLIENT.format(6)
    legacy["adjustments"][identity] = adjustment
    start = {"fence": 1, "release": "acceptance-r1", "time": 1000, "actor": "operator",
             "event": {"accounts": accounts("100")}}
    reply = pair_step(reference, native, laws, legacy, "start", start, "legacy unposted proposal")
    assert reply["status"] == "initialized" and reply["state"]["adjustments"] == {}

    legacy["balances"]["USD"] = 1
    legacy["journal"].append({"kind": "adjustment", "identity": {**adjustment, "approver": "independent"},
                             "postings": [["USD", "asset", 1], ["USD", "adjustment", -1]]})
    outcomes = [entries["start"](None, {**start, "state": legacy}) for entries in (reference, native)]
    assert same(*outcomes) and outcomes[0]["status"] == "opening-ledger-conflict"
    assert outcomes[0]["state"]["balances"] == legacy["balances"] and outcomes[0]["state"]["journal"] == legacy["journal"]
    laws.check(legacy, "start", outcomes[0], "journal-reconstruction", "legacy posted adjustment")


def explore(reference, native, laws, seed, actions, atoms):
    counts, outcomes = Counter(atoms), Counter()
    visited = leaves = 0

    def walk(state, prefix):
        nonlocal visited, leaves
        if len(prefix) == len(atoms):
            leaves += 1
            return
        for name in sorted(counts):
            if not counts[name]:
                continue
            entry, body, old_fence = actions[name]
            if name == "restart":
                body = {"fence": state["fence"] + 1}
            event = {"event": body, "time": state["clock"] + 200, "actor": "explorer", "release": "acceptance-r1",
                     "fence": 1 if old_fence else state["fence"]}
            selected = (*prefix, name)
            reply = pair_step(reference, native, laws, state, entry, event, selected)
            outcomes[reply["status"]] += 1
            visited += 1
            counts[name] -= 1
            walk(reply["state"], selected)
            counts[name] += 1

    walk(seed, ())
    return visited, leaves, outcomes


def bounded(reference, native, laws):
    pre = preopening(laws.wrap(reference))
    other = preopening(laws.wrap(native))
    assert pre.trace == other.trace and pre.state == other.state
    legacy_opening_conflict(reference, native, laws)
    seed, actual = Session(laws.wrap(reference)), Session(laws.wrap(native))
    client, venue = CLIENT.format(1), VENUE.format(1)
    attempts = []
    for session in (seed, actual):
        session.start(usd="100", btc="1", bch="2")
        session.run("admit", "admitted", intent(CLIENT.format(3), 5_000, side="sell"))
        session.run("admit", "admitted", intent(CLIENT.format(4), 3_000, product="BCH-USD"))
        session.run("admit", "admitted", intent(client, 2_500))
        attempts.append(session.dispatch(client))
    assert seed.trace == actual.trace and seed.state == actual.state and attempts[0] == attempts[1]
    venue_order = order(client, venue, "25.00")
    fill_report = fill(venue, 41, "25.00", "0.40", "0.10")
    accepted = {**attempts[0], "response": {"network": "response", "status": 200, "body": venue_order}}
    actions = {
        "lookup": ("order-evidence", {"client-id": client, "body": venue_order}, False),
        "fill": ("fill", {"body": fill_report}, False),
        "cancel": ("cancel", {"client-id": client}, False),
        "kill": ("kill", {}, False),
        "receipt": ("http-result", accepted, True),
        "restart": ("acquire-writer", {}, False),
        "statement": ("reconcile", {"from-cursor": 0, "to-cursor": 41, "fills-complete": True,
                      "open-orders": [{**venue_order, "filled_size": "0.40"}],
                      "accounts": accounts("89.90", "1.40", "2")}, False),
    }
    variants = [("lookup", "fill", "fill", "cancel", "kill", "receipt"),
                ("restart", "lookup", "fill", "fill", "statement", "receipt")]
    prefixes, schedules, outcomes = 0, 0, Counter()
    for atoms in variants:
        visited, leaves, report = explore(reference, native, laws, seed.state, actions, atoms)
        prefixes += visited
        schedules += leaves
        outcomes.update(report)
    assert schedules == 720
    expected = {"partially-filled", "unmatched-fill", "duplicate-fill", "cancel-pending", "cancel-only",
                "reconciliation-required", "reconciled", "open-orders-unexplained", "stale-fence"}
    assert expected <= outcomes.keys(), (expected, outcomes)

    seed.run("http-result", "acknowledged", accepted)
    seed.run("fill", "partially-filled", {"body": fill_report})
    negative = negative_controls(laws, seed.state) + 1
    return {"bounded_schedules": schedules, "schedule_prefix_transitions": prefixes,
            "schedule_outcomes": dict(sorted(outcomes.items())), "negative_controls": negative}


def negative_controls(laws, state):
    def check(transform, expected, entry="status", status="status"):
        after = deepcopy(state)
        transform(after)
        laws.check(state, entry, {"state": after, "status": status}, expected, "negative control")

    check(lambda after: after["balances"].update(USD=after["balances"]["USD"] + 1), "journal-reconstruction")
    check(lambda after: after["reserved"].update(USD=after["reserved"]["USD"] - 1), "reserved-sum")

    def release_early(after):
        order = after["orders"][CLIENT.format(1)]
        after["reserved"]["USD"] -= order["reservation"]
        order["reservation"] = 0

    check(release_early, "order-reservation")
    check(lambda after: after["venue-orders"].clear(), "venue-index")
    check(lambda after: after["executions"].clear(), "execution-lineage")

    def counterfeit_pair(after):
        pair = next(item["postings"] for item in after["journal"] if item["kind"] == "fill-quote")
        pair[0][2] += 1
        pair[1][2] -= 1
        after["balances"]["USD"] += 1

    check(counterfeit_pair, "journal-reconstruction")

    def duplicate_pair(after):
        row = next(item for item in after["journal"] if item["kind"] == "fill-base")
        after["journal"].insert(0, deepcopy(row))
        after["balances"]["BTC"] += row["postings"][0][2]

    check(duplicate_pair, "journal-reconstruction")

    def fabricated_reconciliation(after):
        row = {"kind": "adjustment", "identity": {"actor": "generated", "approver": "other", "reason": "fabricated reconciliation"},
               "postings": [["USD", "asset", 1], ["USD", "adjustment", -1]]}
        after["journal"].insert(0, row)
        after["balances"]["USD"] += 1

    check(fabricated_reconciliation, "nonposting-transition", "reconcile", "reconciled")
    return 8


def committed_histories(work, releases, laws):
    """Read actual SQLite histories after child exits and converge both engines on the saved digest."""
    events = 0
    databases = sorted(work.glob("*.sqlite"))
    for database in databases:
        prior = None
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            for seq, entry, revision, request, response, fingerprint in connection.execute(
                    "SELECT seq, entry, release, request, response, state_digest FROM event_log ORDER BY seq"):
                reference, native = releases[revision]
                reply = pair_step(reference, native, laws, prior, entry, json.loads(request), (database.name, seq))
                assert ({"status": reply["status"], "data": reply["data"]} == json.loads(response)
                        and digest(reply["state"]) == fingerprint), (database, seq)
                prior = reply["state"]
                events += 1
            assert json.loads(connection.execute("SELECT document FROM snapshot WHERE slot=1").fetchone()[0]) == prior, database
    return {"committed_databases": len(databases), "committed_events_checked": events}


def snapshot_diagnostic(database, laws):
    location = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(location, uri=True)) as connection:
        row = connection.execute("SELECT seq, release, document FROM snapshot WHERE slot=1").fetchone()
    if row is None:
        raise ValueError("database has no committed snapshot")
    sequence, release, raw = row
    state = json.loads(raw)
    reference, native = laws.evaluate({"entry": "status", "status": "status", "before": None, "after": state})
    return {"sequence": sequence, "release": release, "state_digest": digest(state),
            "reference_law": reference, "native_law": native}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", nargs="?", type=Path, help="freshly generated trading-engine release")
    parser.add_argument("--database", type=Path, help="read-only law check of a persisted snapshot")
    args = parser.parse_args()
    if (args.release is None) == (args.database is None):
        parser.error("supply a release or --database PATH")
    if not __debug__:
        parser.error("qualification requires assertions enabled")
    sys.setrecursionlimit(20_000)
    with tempfile.TemporaryDirectory(prefix="trading-reachable-") as directory:
        work = Path(directory).resolve()
        laws = LedgerLaws(work)
        if args.database is not None:
            try:
                report = snapshot_diagnostic(args.database, laws)
            except (ValueError, sqlite3.Error) as error:
                parser.exit(1, f"{error}\n")
            print(json.dumps(report, sort_keys=True))
            return 0 if report["reference_law"] == report["native_law"] == "holds" else 1

        import check_host
        from event_host import release_id

        release = args.release.resolve()
        manifest, evaluator = appc.load(MANIFEST)
        reference = appc.reference_entries(manifest, evaluator)
        native = import_native(release)
        revision = release_id(json.loads((release / "ownership.json").read_text()))
        report = bounded(reference, native, laws)
        report.update(check_host.run(release, native, work, include_api=False))
        report.update(committed_histories(work, {revision: (reference, native)}, laws))
        print(json.dumps({**report, "ledger_assertions": laws.observations}, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
