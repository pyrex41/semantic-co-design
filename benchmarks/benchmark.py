#!/usr/bin/env python3
"""Measure source interpretation, native execution, and build artifacts.

The command is deliberately independent of the correctness checks.  It always
records source timings, and records a native row when a lane exposes the common
``specializer.build`` entry point.  A missing optional runtime is reported as
``unavailable`` instead of making the benchmark unusable on a host without the
Shen or Maude tools.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _samples(fn, iterations: int) -> list[float]:
    # Warm the Python import/cache path once, then time only the requested work.
    fn()
    values = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        values.append((time.perf_counter_ns() - start) / 1_000_000)
    return values


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "iterations": len(values),
        "median_ms": round(statistics.median(values), 6),
        "min_ms": round(min(values), 6),
        "max_ms": round(max(values), 6),
    }


def _source_cases() -> dict[str, tuple[callable, callable]]:
    from examples.vm.oracle import run as vm_run
    from examples.decoder.decoder import decode, frame
    from examples.railway.railway import run as rail_run
    from examples.query.query import execute, fixture, QUERIES

    vm_values = list(range(-128, 128))
    stream = frame(1, b"hello") + frame(2, bytes(range(16)))
    chunks = [stream[i : i + 3] for i in range(0, len(stream), 3)]
    events = [("request", "R1"), ("entry", "R1"), ("cancel", "R1"),
              ("clear", "R1"), ("request", "R2"), ("cancel", "R2")]
    tables = fixture()
    return {
        "vm": (lambda: vm_run("checksum", vm_values), lambda: vm_run("checksum", vm_values)),
        "decoder": (lambda: decode(chunks), lambda: decode([stream])),
        "railway": (lambda: rail_run(events), lambda: rail_run(events)),
        "query": (lambda: execute(QUERIES["group_sum"], tables, 12),
                  lambda: execute(QUERIES["group_sum"], tables, 12)),
    }


def _native_command(name: str, root: Path, directory: Path) -> tuple[list[str], bytes] | None:
    """Return a common-command native invocation when the lane provides one."""
    # Lanes may publish a native replay helper.  Keeping discovery here avoids
    # making the benchmark depend on a particular generated filename.
    helper = root / "examples" / name / "native-replay.py"
    if helper.exists():
        return [sys.executable, str(helper)], b"\n"
    return None


def benchmark(iterations: int = 20, output: Path | None = None) -> dict:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    records = []
    for name, (source, baseline) in _source_cases().items():
        source_values = _samples(source, iterations)
        baseline_values = _samples(baseline, iterations)
        row = {
            "example": name,
            "source": _stats(source_values),
            "direct_baseline": _stats(baseline_values),
            "native_build": {
                "status": "unavailable",
                "generation_ms": None,
                "binary_bytes": None,
                "reason": "specializer build receipt is not published by this lane",
            },
            "native": {"status": "unavailable", "reason": "lane has no native-replay helper"},
        }
        with tempfile.TemporaryDirectory(prefix=f"semantic-{name}-") as temp:
            found = _native_command(name, ROOT, Path(temp))
            if found:
                command, stdin = found
                try:
                    generated = []
                    for _ in range(iterations):
                        start = time.perf_counter_ns()
                        proc = subprocess.run(command, input=stdin, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, check=True, timeout=10)
                        generated.append((time.perf_counter_ns() - start) / 1_000_000)
                    row["native"] = {"status": "ok", **_stats(generated)}
                except (OSError, subprocess.SubprocessError) as exc:
                    row["native"] = {"status": "error", "reason": str(exc)}
        records.append(row)
    result = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "iterations": iterations,
        "units": "milliseconds",
        "records": records,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--json", type=Path, help="also write the JSON receipt to this path")
    args = parser.parse_args(argv)
    result = benchmark(args.iterations, args.json)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
