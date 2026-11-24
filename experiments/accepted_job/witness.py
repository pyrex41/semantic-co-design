"""Replay the versioned first-review histories against both submit implementations."""
import json
from collections import Counter
from functools import partial
from pathlib import Path

from replay import SUBMIT, execute, interpret, specialize, violation


def review(packet):
    if (packet["contract"], packet["mapping"]) != ("AcceptedJob/1", "sqlite-durable-rows/1"):
        raise ValueError("unsupported witness version")
    if packet["schedule"] != ["submit A", "first transaction outcome", "lose reply", "restart",
                              "retry A", "commit"]:
        raise ValueError("unsupported schedule")
    required = Counter([
        ("committed", "submit", "commit", True),
        ("duplicate", "duplicate", "commit", False),
        ("rolled-back", "submit", "rollback", True),
    ])
    presented = Counter((case["name"], case["program"], case["outcome"], case["violation"] is None)
                        for case in packet["cases"])
    if presented != required:
        raise ValueError(f"incomplete or substituted witnesses: missing {required - presented}, "
                         f"unexpected {presented - required}")
    programs = {"submit": SUBMIT, "duplicate": (SUBMIT[0], ("same", "duplicate"), SUBMIT[2])}
    for case in packet["cases"]:
        program = programs[case["program"]]
        generated, _ = specialize(program)
        for submit in (partial(interpret, program), generated):
            original, history = execute(submit, case["outcome"], "A", capture_lost_reply=True)
            before, retry, after = history
            actual = dict(lost_reply=original, restart=before, reply=retry, after=after,
                          violation=violation(case["outcome"], "A", history))
            expected = {key: case[key] for key in actual}
            if json.loads(json.dumps(actual)) != expected:
                raise AssertionError((case["name"], actual, expected))
        print(case["name"], "first result", original, "restart", before, "retry", retry,
              "durable rows", after, actual["violation"] or "admitted")


if __name__ == "__main__":
    review(json.loads(Path(__file__).with_name("witnesses.json").read_text()))
