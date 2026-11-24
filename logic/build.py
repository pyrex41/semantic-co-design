"""Specialize a Shen or Maude step function over the finite AcceptedJob interface."""
import argparse
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from spec import compile_native

STATES = ["empty", *(f"{s}-{d}" for s in ("pending", "withdrawn", "delivered") for d in "ab")]
EVENTS = ["submit-a", "submit-b", "cancel", "work"]
OUTPUTS = {"receipt-a", "receipt-b", "deliver-a", "deliver-b", "cancelled", "ignored", "conflict"}


def term(symbol, shen):
    name, sep, field = symbol.partition("-")
    if not sep:
        return name
    return f"[{name} {field}]" if shen else f"{name}({field})"


def evaluate(path, policy):
    queries = list(itertools.product(STATES, EVENTS))
    shen = path.suffix == ".shen"
    if shen:
        cmd = [os.environ.get("SHEN_GO", "shen-go"), "eval", "-l", str(path)]
        for state, event in queries:
            cmd.extend(["-e", f"(job.step {policy} {term(state, True)} {term(event, True)})"])
        request = ""
        atom = r"(?:\[([a-z]+) ([ab])\]|([a-z]+))"
        pattern = rf"^\[{atom} {atom}\]$"
    elif path.suffix == ".maude":
        cmd = [os.environ.get("MAUDE", "maude"), "-no-banner", "-no-advise", "-batch", str(path)]
        request = "".join(f"red in ACCEPTED-JOB : step({policy},{term(s, False)},{term(e, False)}) .\n"
                          for s, e in queries) + "quit\n"
        atom = r"(?:([a-z]+)\(([ab])\)|([a-z]+))"
        pattern = rf"^result Result: result\({atom}, {atom}\)$"
    else:
        raise ValueError("expected .shen or .maude")
    proc = subprocess.run(cmd, input=request, text=True, capture_output=True, check=True, timeout=30)
    if proc.stderr.strip():
        raise ValueError(proc.stderr)
    rows = re.findall(pattern, proc.stdout, re.MULTILINE)
    if len(rows) != len(queries):
        raise ValueError(f"expected {len(queries)} normal forms; found {len(rows)}\n{proc.stdout}")
    rules = {}
    for query, (sc, sd, ss, oc, od, oo) in zip(queries, rows):
        target, output = ss or f"{sc}-{sd}", oo or f"{oc}-{od}"
        if target not in STATES or output not in OUTPUTS:
            raise ValueError((query, target, output))
        rules[query] = target, output
    return rules


def build(path, policy, binary):
    rules = evaluate(path, policy)
    interface = dict(states=STATES, events=EVENTS, initial="empty",
                     storage={"effect": "keyed-state", "key": "uint32"})
    compile_native(interface, rules, binary)
    return rules


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--policy", choices=("withdraw", "keep"), default="withdraw")
    parser.add_argument("--compile", type=Path, required=True)
    args = parser.parse_args()
    build(args.spec, args.policy, args.compile)
