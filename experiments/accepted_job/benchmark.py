"""Paired timings for the AcceptedJob/1 two-submit histories."""
import hashlib
import json
import math
import platform
import random
import sqlite3
import statistics
import sys
from functools import partial
from time import perf_counter_ns

from replay import LOOKUP, SUBMIT, execute, interpret, specialize, violation


def direct(db, digest):
    row = db.execute(LOOKUP).fetchone()
    if row is None:
        inserted = db.execute("INSERT INTO jobs(digest) VALUES (?)", (digest,))
        return ("ok", inserted.lastrowid)
    if row[1] == digest:
        return ("ok", row[0])
    return ("conflict", None)


class Memory:
    """Inject only the three storage responses used by this fixed program."""
    def __init__(self):
        self.row = None
        self.answer = None
        self.lastrowid = 0

    def execute(self, sql, args=()):
        if sql == LOOKUP:
            self.answer = self.row
        elif sql == "INSERT INTO jobs(digest) VALUES (?)":
            self.lastrowid += 1
            self.row = self.lastrowid, args[0]
        elif sql == "SELECT last_insert_rowid()":
            self.answer = (self.lastrowid,)
        else:
            raise ValueError(sql)
        return self

    def fetchone(self):
        return self.answer


def memory_history(submit, outcome, digest):
    db = Memory()
    submit(db, "A")
    if outcome == "rollback":
        db = Memory()
    before = (db.row,) if db.row else ()
    reply = submit(db, digest)
    return before, reply, (db.row,)


def measure(fn):
    start = perf_counter_ns()
    fn()
    return perf_counter_ns() - start


def summary(values):
    low, _, high = statistics.quantiles(values, n=4, method="inclusive")
    return {"median_ns": round(statistics.median(values)), "q1_ns": round(low),
            "q3_ns": round(high)}


def main():
    residual, source = specialize(SUBMIT)
    methods = {"interpreter": partial(interpret, SUBMIT), "residual": residual, "direct": direct}
    schedules = [(outcome, digest) for outcome in ("commit", "rollback") for digest in "AB"]
    gen = summary([measure(lambda: specialize(SUBMIT)) for _ in range(101)])
    report = {"platform": platform.platform(), "machine": platform.machine(),
              "python": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version,
              "residual_bytes": len(source.encode()),
              "residual_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "generation": gen}
    rng = random.Random(903)
    for label, history, cycles in (("injected", memory_history, 1000), ("sqlite", execute, 1)):
        first = {}
        for name, submit in methods.items():
            first[name] = measure(lambda: history(submit, "commit", "A"))
            for outcome, digest in schedules:
                observed = history(submit, outcome, digest)
                if violation(outcome, digest, observed) or observed != execute(direct, outcome, digest):
                    raise RuntimeError((label, name, observed))
        samples = {name: [] for name in methods}
        for _ in range(21):
            order = list(methods)
            rng.shuffle(order)
            for name in order:
                def batch():
                    for _ in range(cycles):
                        for outcome, digest in schedules:
                            history(methods[name], outcome, digest)
                samples[name].append(measure(batch) / (cycles * len(schedules)))
        savings = [a - b for a, b in zip(samples["interpreter"], samples["residual"])]
        paired = summary(savings)
        report[label] = {"first_history_ns": first, "histories_per_batch": cycles * 4,
                         "rounds": 21, "per_history": {k: summary(v) for k, v in samples.items()},
                         "paired_savings": paired,
                         "median_break_even_histories": math.ceil(gen["median_ns"] /
                         statistics.median(savings)) if statistics.median(savings) > 0 else None}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
