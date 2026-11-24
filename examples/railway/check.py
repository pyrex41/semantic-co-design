#!/usr/bin/env python3
import json
from railway import run

events = [("request", "R1"), ("cancel", "R1"), ("request", "R2"),
          ("entry", "R2"), ("cancel", "R2"), ("request", "R1"), ("clear", "R2")]
out = run(events)
assert [x["verdict"] for x in out] == ["grant", "cancel", "grant", "entry", "reject", "reject", "clear"]
assert out[4]["route"] == "R2" and out[4]["occupied"]
assert out[-1]["route"] is None and not out[-1]["occupied"]
print(json.dumps({"ok": True, "observations": len(out)}))
