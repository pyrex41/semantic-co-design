"""Lower a validated residual expression directly to a native function."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


HEADER = """\
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

static inline int64_t checked(__int128 value) {
    if (value < INT64_MIN || value > INT64_MAX) abort();
    return (int64_t)value;
}

static inline int64_t get(const int64_t *args, size_t length, int64_t index) {
    return index >= 0 && (uint64_t)index < length ? args[index] : 0;
}
"""
ARITHMETIC = {"add": "+", "sub": "-", "mul": "*"}
COMPARISONS = {"eqv": "==", "ltv": "<"}
QUOTIENTS = {"divv": "/", "modv": "%"}
LOGICAL = {"andv": "&", "orv": "|"}


def literal(value):
    return "INT64_MIN" if value == -(1 << 63) else f"INT64_C({value})"


class Function:
    def __init__(self, name):
        self.lines = [f"int64_t {name}(const int64_t *args, size_t length) {{"]
        self.depth = 1
        self.next_temp = 0

    def line(self, statement):
        self.lines.append("    " * self.depth + statement)

    def temp(self, expression):
        name = f"v{self.next_temp}"
        self.next_temp += 1
        self.line(f"int64_t {name} = {expression};")
        return name

    def bind(self, statement):
        self.line(statement)
        self.depth += 1

    def close(self):
        self.depth -= 1
        self.line("}")

    def lower(self, tree, bindings):
        op, *children = tree
        if op == "lit":
            return literal(children[0])
        if op == "arg":
            return self.temp(f"get(args, length, {literal(children[0])})")
        if op == "var":
            index = children[0]
            return bindings[index] if 0 <= index < len(bindings) else "0"
        if op == "lenv":
            return self.temp("checked(length)")
        if op == "getv":
            index = self.lower(children[0], bindings)
            return self.temp(f"get(args, length, {index})")
        if op == "letv":
            value = self.lower(children[0], bindings)
            return self.lower(children[1], (value, *bindings))
        if op == "ifv":
            condition = self.lower(children[0], bindings)
            result = self.temp("0")
            self.bind(f"if ({condition}) {{")
            value = self.lower(children[1], bindings)
            self.line(f"{result} = {value};")
            self.close()
            self.bind("else {")
            value = self.lower(children[2], bindings)
            self.line(f"{result} = {value};")
            self.close()
            return result
        if op == "foldv":
            count = self.lower(children[0], bindings)
            initial = self.lower(children[1], bindings)
            accumulator = self.temp(initial)
            index = self.temp("0")
            self.bind(f"for (; {index} < {count}; ++{index}) {{")
            result = self.lower(children[2], (accumulator, index, *bindings))
            self.line(f"{accumulator} = {result};")
            self.close()
            return accumulator

        left = self.lower(children[0], bindings)
        right = self.lower(children[1], bindings)
        if op in ARITHMETIC:
            return self.temp(f"checked((__int128)({left}) {ARITHMETIC[op]} ({right}))")
        if op in COMPARISONS:
            return self.temp(f"({left}) {COMPARISONS[op]} ({right})")
        if op in LOGICAL:
            return self.temp(f"(({left}) != 0) {LOGICAL[op]} (({right}) != 0)")
        if op in QUOTIENTS:
            return self.temp(f"({right}) ? checked((__int128)({left}) "
                             f"{QUOTIENTS[op]} ({right})) : 0")
        if op in ("minv", "maxv"):
            comparison = "<" if op == "minv" else ">"
            return self.temp(f"({left}) {comparison} ({right}) ? ({left}) : ({right})")
        raise ValueError(f"cannot lower operator {op!r}")

    def finish(self, tree):
        result = self.lower(tree, ())
        self.line(f"return {result};")
        self.close()
        return "\n".join(self.lines)


def compile_residuals(work, language, trees):
    source = work / f"{language}.c"
    library = work / f"{language}.so"
    functions = [Function(f"residual_{index}").finish(tree) for index, tree in enumerate(trees)]
    source.write_text(HEADER + "\n\n".join(functions) + "\n")
    command = [os.environ.get("CC", "cc"), "-std=c11", "-O2", "-shared", "-fPIC",
               str(source), "-o", str(library)]
    subprocess.run(command, check=True, timeout=60)
    return library, command


def observations(library, sample_groups):
    native = ctypes.CDLL(str(Path(library).resolve()))
    for index, samples in enumerate(sample_groups):
        function = getattr(native, f"residual_{index}")
        function.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
        function.restype = ctypes.c_int64
        yield [function((ctypes.c_int64 * len(args))(*args), len(args)) for args in samples]


if __name__ == "__main__":
    print(json.dumps(list(observations(sys.argv[1], json.load(sys.stdin)))))
