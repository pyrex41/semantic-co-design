#!/usr/bin/env python3
import json
try:
    from .native_replay import EVENTS, replay
except ImportError:
    from native_replay import EVENTS, replay

def expected(events):
    reservation = train = switch = 0
    output = []
    for code in events:
        route = code // 4 + 1
        kind = code % 4
        verdict = 0
        if kind == 0 and reservation == 0:
            reservation, switch, verdict = route, route, 1
        elif kind == 1 and reservation == route and train == 0:
            reservation, switch, verdict = 0, 0, 2
        elif kind == 2 and reservation == route and train == 0:
            train, verdict = 1, 3
        elif kind == 3 and reservation == route and train == 1:
            reservation, train, switch, verdict = 0, 0, 0, 4
        output.extend((verdict, reservation, train, switch))
    return output

shen = replay("shen")
maude = replay("maude")
assert shen == maude, (shen, maude)
assert shen == expected(EVENTS), (shen, expected(EVENTS))
print(json.dumps({"ok": True, "events": len(EVENTS), "native_values": len(shen)}))
