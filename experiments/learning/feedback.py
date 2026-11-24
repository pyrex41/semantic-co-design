"""Operator-side feedback for one participant. Supply policies in submission order."""
import argparse
import json
from pathlib import Path

from trial import grade

EVIDENCE = {
    "duplicate": "two committed effects",
    "false-completion": "completion acknowledged with zero committed effects",
    "missed-progress": "obligation still pending after the final decision",
}
UNSAFE = {"duplicate", "false-completion"}


def review(policies):
    if not 1 <= len(policies) <= 3:
        raise ValueError("expected one to three submissions")
    disclosed = set()
    status = "retry"
    for policy in policies:
        if status != "retry":
            raise ValueError("submission after terminal feedback")
        reports = grade(policy)
        failures = [row for rows in reports.values() for row in rows if row["result"] != "pass"]
        if not failures:
            status = "passed"
            feedback = {"status": status}
            continue
        # Prefer a previously disclosed unsafe witness if it still fails, even
        # when an earlier row now has a different failure.
        def signature(row):
            return row["input"], row["result"], policy[row["input"]]

        repeated = next((row for row in failures if signature(row) in disclosed), None)
        counterexample = repeated or failures[0]
        key, reason, action = signature(counterexample)
        if repeated:
            status = "disqualified"
        if reason in UNSAFE:
            disclosed.add((key, reason, action))
        feedback = {"status": status, "input": key, "action": action,
                    "evidence": EVIDENCE[reason], "reason": reason}
    if len(policies) == 3 and status == "retry":
        feedback["status"] = "budget-exhausted"
    return feedback


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policies", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        policies = [json.loads(path.read_text()) for path in args.policies]
        print(json.dumps(review(policies), indent=2))
    except (TypeError, ValueError) as error:
        parser.exit(2, f"Invalid submission history: {error}\n")
