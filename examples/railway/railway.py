#!/usr/bin/env python3
"""Small deterministic railway interlocking oracle and native-style runner."""
import json, sys

ROUTES = ("R1", "R2")

def step(state, event):
    route, occupied = state
    kind, name = event
    if name not in ROUTES:
        return state, "reject"
    if kind == "request":
        if route is None:
            return (name, occupied), "grant"
        return state, "reject"
    if kind == "entry":
        return (route, True), "entry" if route == name and not occupied else "reject"
    if kind == "cancel":
        if route == name and not occupied:
            return (None, False), "cancel"
        return state, "reject"
    if kind == "clear":
        if route == name and occupied:
            return (None, False), "clear"
        return state, "reject"
    return state, "reject"

def run(events):
    state = (None, False)
    observations = []
    for event in events:
        state, verdict = step(state, event)
        observations.append({"event": list(event), "verdict": verdict,
                             "route": state[0], "occupied": state[1]})
    return observations

def main(path):
    data = json.load(open(path))
    print(json.dumps(run([tuple(x) for x in data["events"]]), sort_keys=True))

if __name__ == "__main__":
    main(sys.argv[1])
