"""Run batches of Shen expressions in one shen-jit process and read the values back."""
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

TRAP = "tool.trap!"
PRELUDE = """(set *maximum-print-sequence-size* 100000000)
(define tool.emit X -> (do (output "@@ ~S~%" X) 0))
"""
TOKEN = re.compile(r'"[^"]*"|[\[\]|]|[^\s\[\]|"]+')


def term(value):
    """Nested lists or tuples of int, str and bool as a Shen list term."""
    if isinstance(value, (list, tuple)):
        return "[" + " ".join(map(term, value)) + "]"
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def read(text):
    """A printed Shen value as nested tuples."""
    tokens = TOKEN.findall(text)
    pos = 0

    def value():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token != "[":
            if re.fullmatch(r"-?\d+", token):
                return int(token)
            if re.fullmatch(r"-?\d+\.\d+(e-?\d+)?", token):
                return float(token)
            return {"true": True, "false": False}.get(token, token)
        items = []
        while tokens[pos] != "]":
            if tokens[pos] == "|":
                raise ValueError(f"improper list in {text!r}")
            items.append(value())
        pos += 1
        return tuple(items)

    result = value()
    if pos != len(tokens):
        raise ValueError(f"trailing text in {text!r}")
    return result


def evaluate(files, expressions, *, timeout=120):
    """Load `files`, evaluate each expression, and return the values in order.

    A Shen error inside an expression yields TRAP for that expression only.
    """
    expressions = list(expressions)
    with TemporaryDirectory() as work:
        driver = Path(work) / "driver.shen"
        driver.write_text(PRELUDE + "".join(
            f"(tool.emit (trap-error {e} (/. E {TRAP})))\n" for e in expressions))
        command = [os.environ.get("SHEN_JIT", "shen-jit"), "eval"]
        for file in files:
            command += ["-l", str(file)]
        # Shen asks on stdin whether to track a partial function; give it an empty stdin.
        proc = subprocess.run([*command, "-l", str(driver)], text=True, capture_output=True,
                              input="", timeout=timeout)
    lines = re.findall(r"@@ (.*)$", proc.stdout, re.MULTILINE)
    if proc.returncode or len(lines) != len(expressions):
        raise RuntimeError(f"shen-jit: {len(lines)} of {len(expressions)} results\n"
                           f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    return [read(line) for line in lines]
