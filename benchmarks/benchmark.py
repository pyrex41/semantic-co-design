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


def _staged_cases() -> dict[str, tuple[object, list[list[int]]]]:
    """Static values and dynamic rows for the shared ``specializer`` API."""
    return {
        "vm": ([[0]], [list(range(-32, 32))] * 32),
        "decoder": ([], [[0xA5, 1, 0, 1, 7, 9]] * 32),
        "railway": ([], [[0, 1, 2, 3, 4, 5]] * 32),
        "query": ([], [[1, 2, 1, 7, 2, 4]] * 32),
    }


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


def _native_persistent(binary: Path, rows: list[list[int]], iterations: int) -> dict:
    payload = "".join(" ".join(map(str, row)) + "\n" for row in rows)
    try:
        start = time.perf_counter_ns()
        process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        startup_ms = (time.perf_counter_ns() - start) / 1_000_000
        warm = []
        assert process.stdin is not None
        for _ in range(iterations):
            begin = time.perf_counter_ns()
            process.stdin.write(payload)
            process.stdin.flush()
            warm.append((time.perf_counter_ns() - begin) / 1_000_000)
        process.stdin.close()
        process.stdin = None
        process.communicate(timeout=20)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, str(binary))
        return {"status": "ok", "startup_ms": round(startup_ms, 6), **_stats(warm)}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": str(exc)}


def benchmark(iterations: int = 20, output: Path | None = None) -> dict:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    records = []
    staged = _staged_cases()
    for name, (source, baseline) in _source_cases().items():
        source_values = _samples(source, iterations)
        baseline_values = _samples(baseline, iterations)
        row = {
            "example": name,
            "source": _stats(source_values),
            "direct_baseline": _stats(baseline_values),
            "staged_source": {},
            "native_build": {},
            "native": {},
        }
        with tempfile.TemporaryDirectory(prefix=f"semantic-{name}-") as temp:
            static, rows = staged[name]
            try:
                from specializer import build, concrete
                source_rows = rows[:8]
                source_receipts = {}
                for source_name in ("shen", "maude"):
                    started = time.perf_counter_ns()
                    concrete(name, source_name, static, source_rows)
                    source_receipts[source_name] = {"startup_ms": round((time.perf_counter_ns() - started) / 1_000_000, 6)}
                row["staged_source"] = {"status": "ok", "receipts": source_receipts}
                binary = Path(temp) / "program"
                started = time.perf_counter_ns()
                build(name, "shen", static, binary, emit_ir=Path(temp) / "program.ir.json",
                      emit_c=Path(temp) / "program.c")
                generation_ms = (time.perf_counter_ns() - started) / 1_000_000
                row["native_build"] = {"status": "ok", "generation_ms": round(generation_ms, 6),
                                        "binary_bytes": binary.stat().st_size}
                row["native"] = _native_persistent(binary, rows, iterations)
            except (OSError, ValueError, subprocess.SubprocessError, KeyError) as exc:
                row["staged_source"] = {"status": "unavailable", "reason": str(exc)}
                row["native_build"] = {"status": "unavailable", "reason": str(exc)}
                row["native"] = {"status": "unavailable", "reason": str(exc)}
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
