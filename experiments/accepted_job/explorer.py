"""Finite explorer for one initial submit, with at most one restart and retry."""
import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from replay import SUBMIT, interpret, snapshot, specialize, violation


def choices(prefix):
    if not prefix:
        return ("commit", "rollback")
    if len(prefix) == 1:
        return ("deliver", "lose-and-restart")
    if len(prefix) == 2 and prefix[1] == "lose-and-restart":
        return ("A", "B")
    return ()


def schedules(prefix=()):
    enabled = choices(prefix)
    if not enabled:
        yield prefix
    for choice in enabled:
        yield from schedules((*prefix, choice))


def observe(submit, schedule):
    outcome, transport, *retry = schedule
    with TemporaryDirectory() as directory:
        path = Path(directory) / "jobs.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, digest TEXT NOT NULL)")
            first = submit(db, "A")
            if outcome == "commit":
                db.commit()
            else:
                db.rollback()
                first = ("aborted", None)
            if transport == "deliver":
                return (first,), snapshot(db)
        with closing(sqlite3.connect(path)) as restarted:
            before = snapshot(restarted)
            reply = submit(restarted, retry[0])
            restarted.commit()
            return (before, reply, snapshot(restarted))


def specialized_explorer(program):
    fixed_schedules = tuple(schedules())
    submit, _ = specialize(program)
    def explore():
        return {schedule: observe(submit, schedule) for schedule in fixed_schedules}
    return explore


def verify(results, mutant):
    expected = {(outcome, "deliver") for outcome in ("commit", "rollback")}
    expected.update((outcome, "lose-and-restart", digest)
                    for outcome in ("commit", "rollback") for digest in "AB")
    if results.keys() != expected:
        raise ValueError("missing or unexpected schedule")
    failures = set()
    for schedule, observation in results.items():
        if schedule[1] == "deliver":
            expected_terminal = ((("ok", 1),), ((1, "A"),)) if schedule[0] == "commit" else (
                (("aborted", None),), ())
            if observation != expected_terminal:
                failures.add(schedule)
        elif violation(schedule[0], schedule[2], observation):
            failures.add(schedule)
    expected_failures = {("commit", "lose-and-restart", "A")} if mutant else set()
    if failures != expected_failures:
        raise ValueError((failures, expected_failures))


def main():
    for name, program in (("submit", SUBMIT),
                          ("duplicate", (SUBMIT[0], ("same", "duplicate"), SUBMIT[2]))):
        interpreted = {s: observe(partial(interpret, program), s) for s in schedules()}
        generated = specialized_explorer(program)()
        if generated != interpreted:
            raise AssertionError("schedule-to-observation maps differ")
        verify(generated, name == "duplicate")
        for schedule, observation in generated.items():
            print(name, schedule, observation)


if __name__ == "__main__":
    main()
