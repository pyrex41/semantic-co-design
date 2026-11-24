"""Materialize the pre-existing contract baseline from pinned repository blobs."""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

SOURCES = (
    ("fenrir", "da55d436f40bc15aedd16eed2305f4694d2bc6e7",
     "models/service-a2/store-contract.shen"),
    ("capcov", "c519f4315c91bd2c26d631444490612e1875afed",
     "tests/claim_semantics/fixtures/systems/outbox-intended.ops"),
)


def render(repos):
    parts = []
    for project, revision, path in SOURCES:
        # Keep the historical packet label; its identical fixture is publicly fetchable here.
        source = "23f7841ca7369bf793c633e682fb00c77afc644c" if project == "capcov" else revision
        blob = subprocess.check_output(["git", "-C", repos[project], "show", f"{source}:{path}"])
        parts.append(f"{project} {revision} {path}\n\n".encode() + blob)
    return b"\n".join(parts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fenrir", type=Path)
    parser.add_argument("capcov", type=Path)
    args = parser.parse_args()
    packet = render(vars(args))
    expected = Path(__file__).with_name("reference-library.sha256").read_text().split()[0]
    if hashlib.sha256(packet).hexdigest() != expected:
        raise SystemExit("reference library digest mismatch")
    sys.stdout.buffer.write(packet)
