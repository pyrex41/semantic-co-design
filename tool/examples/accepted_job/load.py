"""python3 -m tool.examples.accepted_job.load <record.json> <host:port> <answers> <pause> <seconds> [seed]

The client of real.py as a command, for a node on another machine. It makes
submissions until `answers` of them have been answered, `pause` seconds apart.
A submission is one of two keys with one of two digests; it is sent under a new
request number every 0.4 s until the reply to that number arrives, so a kill, a
cut link or a lost packet shows up as a retry.

record.json is kept across invocations, so request numbers go on where the last
one stopped: {"sent": {request: [key, digest]}, "got": [every output read],
"answered": n}. conform.py takes it with --received. Prints the facts of this
invocation; exit 1 when the time ran out first.
"""
import json
import os
from pathlib import Path
import random
import sys
import time

from ...host import Client
from .workload import DIGESTS, KEY

KEYS = (KEY, KEY + 1)


def load(record, address, answers, pause=0.0, seconds=60.0, seed=0):
    path = Path(record)
    kept = json.loads(path.read_text()) if path.exists() else {"sent": {}, "got": [], "answered": 0}
    rng, client = random.Random(f"{seed} {len(kept['sent'])}"), Client(address)
    deadline = time.monotonic() + seconds
    facts = {"answers": 0, "requests": 0, "statuses": {}, "fastest_ms": None}

    def save():
        kept["got"] += client.got
        client.got = []
        path.with_suffix(".part").write_text(json.dumps(kept))
        os.replace(path.with_suffix(".part"), path)

    while facts["answers"] < answers and time.monotonic() < deadline:
        key, digest = rng.choice(KEYS), rng.choice(DIGESTS)
        while time.monotonic() < deadline:
            request = len(kept["sent"])
            kept["sent"][str(request)] = [key, digest]
            facts["requests"] += 1
            began = time.monotonic()
            client.send(("submit", request, key, digest))
            reply = client.wait(lambda o: o[:2] == ("reply", request), 0.3)
            if reply:
                took = round(1000 * (time.monotonic() - began), 1)
                facts["fastest_ms"] = min(took, facts["fastest_ms"] or took)
                facts["statuses"][reply[2]] = facts["statuses"].get(reply[2], 0) + 1
                facts["answers"] += 1
                kept["answered"] += 1
                break
            time.sleep(0.1)
        save()
        time.sleep(pause)
    client.close()
    return facts


if __name__ == "__main__":
    record, where, answers, pause, seconds, *seed = sys.argv[1:]
    host, _, port = where.rpartition(":")
    facts = load(record, (host, int(port)), int(answers), float(pause), float(seconds), *seed)
    print(json.dumps(facts))
    sys.exit(0 if facts["answers"] == int(answers) else 1)
