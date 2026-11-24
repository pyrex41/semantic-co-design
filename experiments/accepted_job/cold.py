"""Time one validated commit/lost-reply/retry history per fresh Python process."""
import json
import platform
import random
import sqlite3
import subprocess
import sys
from functools import partial
from pathlib import Path

from benchmark import direct, measure, memory_history, summary
from replay import SUBMIT, execute, interpret, specialize, violation


def once(method, backend):
    if method == "residual":
        submit, _ = specialize(SUBMIT)
    elif method == "interpreter":
        submit = partial(interpret, SUBMIT)
    else:
        submit = direct
    run = execute if backend == "sqlite" else memory_history
    reason = violation("commit", "A", run(submit, "commit", "A"))
    if reason:
        raise ValueError(reason)


def main():
    rng = random.Random(903)
    report = {"platform": platform.platform(), "python": sys.version.split()[0],
              "sqlite": sqlite3.sqlite_version, "rounds": 11, "backends": {}}
    for backend in ("injected", "sqlite"):
        samples = {m: [] for m in ("interpreter", "residual", "direct")}
        for _ in range(report["rounds"]):
            order = list(samples)
            rng.shuffle(order)
            for method in order:
                cmd = [sys.executable, "-B", str(Path(__file__).resolve()), method, backend]
                elapsed = measure(lambda: subprocess.run(cmd, check=True, stdout=subprocess.PIPE, timeout=30))
                samples[method].append(elapsed)
        savings = [i-r for i, r in zip(samples["interpreter"], samples["residual"])]
        report["backends"][backend] = {"samples_ns": samples, "paired_savings": summary(savings),
                                        "elapsed": {m: summary(v) for m, v in samples.items()}}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3:
        once(*sys.argv[1:])
    else:
        main()
