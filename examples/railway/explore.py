#!/usr/bin/env python3
"""Exhaustively explore serialized event schedules through depth six."""
from itertools import product
from railway import ROUTES, step

EVENTS = tuple((kind, route) for kind in ("request", "cancel", "entry", "clear") for route in ROUTES)

def explore(depth=6):
    frontier = [((None, False), ())]
    visited = {(None, False)}
    transitions = 0
    shortest = {}
    for _ in range(depth):
        nxt = []
        for state, trace in frontier:
            for event in EVENTS:
                new, verdict = step(state, event)
                transitions += 1
                if verdict == "grant" and new[0] in ROUTES and new[0] not in shortest:
                    shortest[new[0]] = trace + (event,)
                if new not in visited:
                    visited.add(new)
                    nxt.append((new, trace + (event,)))
        frontier = nxt
    return {"depth": depth, "states": len(visited), "transitions": transitions,
            "shortest_grants": {k: list(v) for k, v in shortest.items()}}

if __name__ == "__main__":
    import json
    print(json.dumps(explore(), sort_keys=True))
