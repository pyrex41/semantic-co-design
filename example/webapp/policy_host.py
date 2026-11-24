"""Isolated, bounded host for a generated pure entry."""
import json
import resource
import sys
from pathlib import Path

from values import Problem


class PureContext:
    def effect(self, _name, *_args):
        raise ValueError("effects are forbidden in the policy process")


def run(entry):
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError("input too large")
        result = entry(PureContext(), json.loads(raw))
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (Problem, ValueError, RecursionError):
        raise SystemExit(2)


def main(entries):
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    entry = json.loads(Path(__file__).with_name("entry.json").read_text())
    run(entries[entry])
