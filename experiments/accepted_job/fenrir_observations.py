"""Fenrir da55d436 observation mapping v1 for a single tenant, key and digest."""
import json
import sys
from pathlib import Path


def observe(path):
    prefix = [["submit", 0, 0, 0], ["crash"], ["submit", 0, 0, 0]]
    outcomes = set()
    matched = 0
    with path.open() as stream:
        for line in stream:
            if not line.startswith("HISTORY "):
                continue
            schedule, state = json.loads(line.removeprefix("HISTORY "))
            if schedule[:3] != prefix:
                continue
            stores, _, _ = state
            store, = stores
            _, jobs, _, _ = store
            # The rest of this bounded schedule may acknowledge or complete jobs.
            # Keep every durable job identity even when its status is terminal.
            outcomes.add(tuple(sorted(job_id for job_id, report_id, status in jobs)))
            matched += 1
    return matched, outcomes


if __name__ == "__main__":
    for mode, expected in (("model", {(0,)}), ("retry-reapplies", {(0, 1)})):
        count, outcomes = observe(Path(sys.argv[1]) / f"{mode}.shen.log")
        if outcomes != expected:
            raise ValueError((mode, count, outcomes))
        print(f"Fenrir observation v1: {mode}, {count} terminal schedules after retry,"
              f" durable job IDs {sorted(outcomes)}")
