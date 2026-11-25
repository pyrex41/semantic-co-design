"""python3 -m tool.host.ask <host:port> <seconds> '<request as JSON>' [received.json]

Send one request to a node's client port and print the output that answers it:
the first one whose first field is the request's first field, which is how both
examples number their requests. The request is sent again only after the
connection was lost. Every output read is appended to received.json, a JSON
list in the form conform.py takes with --received. Exit 1 on silence.
"""
import json
from pathlib import Path
import sys
import time

from . import Client


def ask(address, request, seconds, record=None):
    client, deadline, answer = Client(address), time.monotonic() + seconds, None
    while answer is None and time.monotonic() < deadline:
        if client.conn is None:
            client.send(request)
            if client.conn is None:
                time.sleep(0.1)
                continue
        answer = client.wait(lambda o: o[1:2] == tuple(request[1:2]), min(1, seconds))
    client.close()
    if record:
        path = Path(record)
        path.write_text(json.dumps((json.loads(path.read_text()) if path.exists() else [])
                                   + client.got))
    return answer


if __name__ == "__main__":
    where, seconds, request, *record = sys.argv[1:]
    host, _, port = where.rpartition(":")
    answer = ask((host, int(port)), json.loads(request), float(seconds), *record)
    print(json.dumps(answer))
    sys.exit(0 if answer else 1)
