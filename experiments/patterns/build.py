"""Specialize a JSON profile to a reentrant, linkable C11 pattern library."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from specializer import specialize
from specializer.backend import lower_c_body

HERE = Path(__file__).resolve().parent
IDENT = re.compile(r"[a-z][a-z0-9_]{0,47}")
UINT32_MAX = (1 << 32) - 1


def word(x):
    if type(x) is not int or not 0 <= x <= UINT32_MAX:
        raise ValueError(f"expected uint32 constant, received {x!r}")
    return x


def sequence(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("expected a list of at most 32 steps")
    return value


def encode(entry):
    if not isinstance(entry, dict):
        raise ValueError("expected a pattern definition")
    if entry.keys() == {"handlers", "default"}:
        return [0, [[word(k), word(v)] for k, v in sequence(entry["handlers"])], word(entry["default"])]
    if entry.keys() == {"sum"}:
        ops = {"keep_ge": 0, "add": 1}
        steps = []
        for op, value in sequence(entry["sum"]):
            if op not in ops:
                raise ValueError(f"unsupported sequence stage {op!r}")
            steps.append([ops[op], word(value)])
        return [1, steps]
    if set(entry) in ({"match"}, {"match", "capture"}):
        names, pattern = {}, []
        for token in sequence(entry["match"]):
            if token == "_":
                pattern.append([2])
            elif isinstance(token, str) and token.startswith("?") and IDENT.fullmatch(token[1:]):
                pattern.append([1, names.setdefault(token, len(names))])
            else:
                pattern.append([0, word(token)])
        capture = entry.get("capture")
        if capture is not None and capture not in names:
            raise ValueError(f"capture {capture!r} has no binding")
        return [2, pattern, names.get(capture, -1)]
    raise ValueError(f"unknown pattern fields {list(entry)}")


def compile_library(profile, destination):
    if not isinstance(profile, dict) or not profile:
        raise ValueError("expected named patterns")
    prototypes, functions, programs = [], [], {}
    for name, entry in profile.items():
        if not IDENT.fullmatch(name):
            raise ValueError(f"invalid entry name {name!r}")
        program = specialize("patterns", "shen", encode(entry), directory=HERE)
        lines, banks = lower_c_body(program)
        if banks:
            raise ValueError("scalar pattern functions cannot allocate scratch banks")
        signature = f"int64_t pat_{name}(const uint32_t *input, size_t length)"
        prototypes.append(signature + ";")
        functions.extend([signature + " {",
                          "    if (length > UINT32_MAX || (length && !input)) return -1;",
                          "    int64_t count = (int64_t)length;",
                          "    int64_t slots[3] = {0};", *lines, "}", ""])
        programs[name] = program

    # Publish the complete header and archive together. Existing profiles are immutable.
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"choose a fresh output directory: {destination}")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        out = Path(temp) / "release"
        out.mkdir()
        header = ["#ifndef SC_PATTERNS_H", "#define SC_PATTERNS_H", "#include <stddef.h>",
                  "#include <stdint.h>", *prototypes, "#endif"]
        (out / "patterns.h").write_text("\n".join(header) + "\n")
        support = ['#include "patterns.h"', '#define emit(value) return (value)',
                   'static inline int64_t read_at(const uint32_t *p, int64_t n, int64_t i) {',
                   '    return i >= 0 && i < n ? p[i] : 0;', '}']
        (out / "patterns.c").write_text("\n".join(support + functions) + "\n")
        obj = Path(temp) / "patterns.o"
        subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-O2", "-c",
                        out / "patterns.c", "-o", obj], check=True, timeout=60)
        subprocess.run([os.environ.get("AR", "ar"), "rcs", out / "libpatterns.a", obj], check=True, timeout=60)
        out.rename(destination)
    return programs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    compile_library(json.loads(args.profile.read_text()), args.out)
