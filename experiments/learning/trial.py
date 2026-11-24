"""Frozen trial v1. Grade a JSON policy over public observations."""
import argparse
import itertools
import json
from pathlib import Path

ACTIONS = {"retry", "settle", "hold"}


def public(task, tick, signal):
    return f"{task}/{tick}/{signal}"


def cases():
    # A single request has already been attempted. Its reply was lost.
    # The receipt is valid at ticks < 3. A committed effect is independent
    # of whether the client has received authoritative evidence of it.
    for task, tick, committed in itertools.product(("voucher", "dispatch"), (2, 3, 4), (False, True)):
        for signal in ("unavailable", "applied" if committed else "absent"):
            yield task, tick, committed, signal


def execute(task, tick, committed, signal, action):
    effects = int(committed)
    if action == "retry":
        protected = task == "voucher" and tick < 3
        if not (protected and committed):
            effects += 1
        terminal = True
    else:
        terminal = action == "settle"

    if effects > 1:
        return "duplicate"
    if terminal and effects != 1:
        return "false-completion"
    can_finish = signal != "unavailable" or (task == "voucher" and tick < 3)
    if can_finish and not terminal:
        return "missed-progress"
    return "pass"


def grade(policy):
    expected = {public(t, k, s) for t, k, _, s in cases()}
    if not isinstance(policy, dict) or set(policy) != expected:
        raise ValueError("policy must cover exactly the public observations")
    if any(action not in ACTIONS for action in policy.values()):
        raise ValueError("unknown action")
    reports = {}
    for task, tick, committed, signal in cases():
        key = public(task, tick, signal)
        outcome = execute(task, tick, committed, signal, policy[key])
        reports.setdefault(task, []).append({"input": key, "committed": committed, "result": outcome})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    args = parser.parse_args()
    reports = grade(json.loads(args.policy.read_text()))
    print(json.dumps(reports, indent=2))
    if any(row["result"] != "pass" for rows in reports.values() for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
