"""Verdicts over a declared finite set of possible histories."""
from collections import defaultdict


def classify(cases, observe):
    """cases yields (witness ID, complete history, contract satisfied).

    Group only on information the selected observer can actually read.
    Keep witnesses for both sides so every ambiguous verdict is replayable.
    """
    groups = defaultdict(lambda: {"admitted": [], "violated": []})
    for witness, history, satisfied in cases:
        groups[observe(history)]["admitted" if satisfied else "violated"].append(witness)
    for observation, witnesses in groups.items():
        if witnesses["admitted"] and witnesses["violated"]:
            verdict = "unknown"
        elif witnesses["violated"]:
            verdict = "violated"
        else:
            verdict = "admitted-in-corpus"
        yield observation, verdict, witnesses
