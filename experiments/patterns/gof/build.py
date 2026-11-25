"""Specialize a GoF JSON profile to a linkable C11 library."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .common import identifier

HERE = Path(__file__).resolve().parent
CFLAGS = ("-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic")
RESERVED_HOOKS = set("""
alignas alignof auto bool break case char const continue default do double else enum extern
false float for goto if inline int long register restrict return short signed sizeof static
static_assert struct switch thread_local true typedef union unsigned void volatile while
""".split())


class TranslationUnit:
    def __init__(self):
        self.declarations = {}
        self.private = {}
        self.functions = {}
        self.hooks = {}

    def declare(self, declaration):
        self.declarations[declaration.strip()] = None

    def source(self, definition):
        self.private[definition.strip()] = None

    def define(self, signature, body):
        function = re.search(r"\b([a-z][a-z0-9_]*)\(", signature).group(1)
        if function in self.functions:
            raise ValueError(f"duplicate C function {function}")
        self.declare(signature + ";")
        lines = body.splitlines() if isinstance(body, str) else body
        statements = ["    " + line if line else "" for line in lines]
        self.functions[function] = "\n".join([signature + " {", *statements, "}"])

    def hook(self, symbol, result, parameters):
        symbol = identifier(symbol)
        if symbol.startswith("gof_") or symbol in RESERVED_HOOKS:
            raise ValueError(f"reserved external hook name {symbol!r}")
        declaration = f"extern {result} {symbol}({parameters});"
        if self.hooks.setdefault(symbol, declaration) != declaration:
            raise ValueError(f"conflicting types for external hook {symbol!r}")
        types = parameters.split(", ")
        names = [f"gof_arg_{index}" for index in range(len(types))]
        arguments = ", ".join(f"{kind} {name}" for kind, name in zip(types, names))
        statement = f"{symbol}({', '.join(names)});"
        if result != "void":
            statement = "return " + statement
        wrapper = f"gof_hook_{symbol}"
        self.source(f"static inline {result} {wrapper}({arguments}) {{\n    {statement}\n}}")
        return wrapper

    def header(self):
        return ("#ifndef SEMANTIC_CODESIGN_GOF_H\n#define SEMANTIC_CODESIGN_GOF_H\n\n"
                "#include <stdbool.h>\n#include <stddef.h>\n#include <stdint.h>\n\n"
                + "\n\n".join(self.declarations) + "\n\n#endif\n")

    def implementation(self):
        sections = ['#include "gof.h"\n#include <stdlib.h>', "\n".join(self.hooks.values()),
                    *self.private, *self.functions.values()]
        return "\n\n".join(section for section in sections if section) + "\n"


def unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate profile key {key!r}")
        obj[key] = value
    return obj


def read_profile(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_keys)


def specialize_profile(profile):
    from . import behavioral, creational, structural

    generators = behavioral.GENERATORS | creational.GENERATORS | structural.GENERATORS
    if not isinstance(profile, dict) or not 1 <= len(profile) <= 64:
        raise ValueError("expected 1 to 64 named entries")
    reserved = {"hook", "chain_read_at"} | behavioral.SHARED_NAMES | creational.SHARED_NAMES | structural.SHARED_NAMES
    unit = TranslationUnit()
    for name, entry in profile.items():
        try:
            identifier(name)
            if name in reserved or name.startswith("hook_"):
                raise ValueError("entry overlaps a shared C name")
            if not isinstance(entry, dict):
                raise ValueError("expected a pattern object")
            pattern = entry.get("pattern")
            if not isinstance(pattern, str) or pattern not in generators:
                raise ValueError(f"unknown pattern {pattern!r}")
            generators[pattern](unit, f"gof_{name}", {k: v for k, v in entry.items() if k != "pattern"})
        except ValueError as error:
            raise ValueError(f"{name}: {error}") from error
    return unit


def compile_library(profile, destination):
    unit = specialize_profile(profile)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"choose a fresh output directory: {destination}")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        work = Path(temporary)
        release = work / "release"
        release.mkdir()
        (release / "gof.h").write_text(unit.header())
        (release / "gof.c").write_text(unit.implementation())
        obj = work / "gof.o"
        subprocess.run([os.environ.get("CLANG", "clang"), *CFLAGS, "-O2", "-c",
                        release / "gof.c", "-o", obj], check=True, timeout=60)
        subprocess.run([os.environ.get("AR", "ar"), "rcs", release / "libgof.a", obj],
                       check=True, timeout=60)
        release.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        compile_library(read_profile(args.profile), args.out)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
