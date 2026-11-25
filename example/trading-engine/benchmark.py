"""Benchmark a bounded deterministic engine workload against a simulated venue."""

import argparse
import json
import platform
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

from check import MANIFEST, PRODUCT, accounts, fixture, import_native, intent, order
from event_host import Store, release_id
from package import appc, build


def p99(samples):
    return round(sorted(samples)[(99 * len(samples) + 99) // 100 - 1] * 1_000_000, 1)


def run(root, frames, order_count):
    started = time.perf_counter()
    release = root / "release"
    build(MANIFEST, release)
    build_seconds = time.perf_counter() - started
    ownership = json.loads((release / "ownership.json").read_text())
    version = release_id(ownership)
    manifest, _ = appc.load(MANIFEST)
    entries = import_native(release)
    roles = {role for target in manifest["access"].values() for role in target["roles"]}
    database = root / "account.sqlite"
    start = time.perf_counter()
    cold = subprocess.run([str(release / "engine"), "--database", str(database), "call", "status"],
                          input="{}", capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(cold.stdout)["status"] == "status"
    cold_seconds = time.perf_counter() - start
    store = Store(database, manifest, entries, version)
    clock = 1000

    def step():
        nonlocal clock
        clock += 101
        return clock

    def invoke(entry, event, expected):
        result = store.invoke(entry, event, time_ms=step(), fence=1, actor="benchmark", roles=roles)
        assert result["status"] == expected, (entry, result)
        return result["data"]

    invoke("acquire-writer", {"fence": 1}, "reconciliation-required")
    balances = accounts("100")
    invoke("start", {"accounts": balances}, "initialized")
    invoke("reconcile", {"accounts": balances, "from-cursor": 0, "to-cursor": 0,
                         "fills-complete": True, "open-orders": []}, "reconciled")
    invoke("resume", {}, "trading")
    invoke("metadata", {"product": PRODUCT, "body": fixture(PRODUCT, "product")}, "metadata")
    snapshot = fixture(PRODUCT, "book")
    invoke("snapshot", {"product": PRODUCT, "body": snapshot}, "book")

    # Attribute time within the same durable calls, with the same inputs and commits.
    compiled_delta = entries["delta"]
    transition_ns = 0
    durable_ns = 0

    def timed_delta(ctx, request):
        nonlocal transition_ns
        begin = time.perf_counter_ns()
        result = compiled_delta(ctx, request)
        transition_ns += time.perf_counter_ns() - begin
        return result

    store.entries = {**entries, "delta": timed_delta}
    start = time.perf_counter()
    queue = []
    for offset in range(1, frames + 1):
        event = {"type": "received", "product_id": PRODUCT, "sequence": snapshot["sequence"] + offset}
        arrival = start + (offset - 1) * 0.00025
        wait = arrival - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        queue.append(max(0, time.perf_counter() - arrival))
        begin = time.perf_counter_ns()
        invoke("delta", {"body": event}, "book")
        durable_ns += time.perf_counter_ns() - begin
    feed_seconds = time.perf_counter() - start
    store.entries = entries
    last_book_time = clock

    sends, cancels = [], []
    for count in range(order_count):
        client = f"10000000-1234-4abc-8123-{count:012x}"
        venue = f"20000000-1234-4abc-8123-{count:012x}"
        beginning = time.perf_counter()
        invoke("admit", intent(client, 500), "admitted")

        def accept(request, _):
            sends.append(time.perf_counter() - beginning)
            assert request["body"]["client_oid"] == client
            body = order(client, venue, "5.00")
            return {"network": "response", "status": 201, "body": body}

        reply = store.send({"client-id": client}, None, fence=1, actor="benchmark", roles=roles, effect=accept, clock=step)
        assert reply["status"] == "acknowledged", reply
        beginning = time.perf_counter()
        invoke("cancel", {"client-id": client}, "cancel-pending")

        def cancel(request, _):
            cancels.append(time.perf_counter() - beginning)
            assert request["method"] == "DELETE"
            return {"network": "response", "status": 200, "body": client}

        reply = store.send({"client-id": client}, None, fence=1, actor="benchmark", roles=roles, effect=cancel, clock=step)
        assert reply["status"] == "cancel-awaiting-proof", reply
        body = order(client, venue, "5.00", status="done", reason="canceled")
        invoke("order-evidence", {"client-id": client, "body": body, "fills-complete": True}, "cancelled")

    start = time.perf_counter()
    replay = Store(database, manifest, entries, version).replay({version: entries})
    replay_seconds = time.perf_counter() - start
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
                 "cpu": platform.processor()},
        "workload": {"mode": "local SQLite WAL, FULL synchronous, simulated venue without network delay",
                     "frames": frames, "arrival_interval_us": 250, "orders": order_count},
        "generation_and_build_seconds": round(build_seconds, 3),
        "package_bytes": sum(item.stat().st_size for item in release.rglob("*") if item.is_file()),
        "cold_start_seconds": round(cold_seconds, 3),
        "feed_delta_wall_ns": {"compiled_transition": transition_ns, "durable_calls": durable_ns},
        "durable_feed_frames_per_second": round(frames / feed_seconds, 1),
        "synthetic_feed_queue_lag_p99_us": p99(queue),
        "final_book_age_virtual_ms": clock - last_book_time,
        "decision_to_transport_p99_us": p99(sends),
        "cancel_to_transport_p99_us": p99(cancels),
        "replay_seconds": round(replay_seconds, 3),
        "replay_events": replay["events"],
        "python_peak_rss_bytes": max_rss if sys.platform == "darwin" else max_rss * 1024,
        "release": version,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=256, choices=range(1, 1025), metavar="1..1024")
    parser.add_argument("--orders", type=int, default=32, choices=range(1, 64), metavar="1..63")
    args = parser.parse_args()
    sys.setrecursionlimit(20_000)
    with tempfile.TemporaryDirectory(prefix="trading-benchmark-") as directory:
        print(json.dumps(run(Path(directory), args.frames, args.orders), sort_keys=True, indent=2))
