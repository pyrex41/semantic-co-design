"""Replay railway event codes through both source-specialized native binaries."""
from pathlib import Path
import json, subprocess, tempfile, sys

from specializer import build

ROOT = Path(__file__).parent
STATIC = [0, 1]
EVENTS = [0, 2, 1, 3, 4, 6, 7]  # request/entry/cancel/clear for both routes

def replay(source):
    with tempfile.TemporaryDirectory() as d:
        binary = Path(d) / source
        build("railway", source, STATIC, binary,
              emit_ir=Path(d) / (source + ".json"),
              emit_c=Path(d) / (source + ".c"))
        proc = subprocess.run([str(binary)], input=" ".join(map(str, EVENTS)) + "\n",
                              text=True, capture_output=True, check=True)
        return [int(x) for x in proc.stdout.split()]

if __name__ == "__main__":
    values = {source: replay(source) for source in ("shen", "maude")}
    assert values["shen"] == values["maude"], values
    print(json.dumps(values, sort_keys=True))
