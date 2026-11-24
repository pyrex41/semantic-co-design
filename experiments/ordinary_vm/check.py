"""Run from the checkout with nix develop -c python3 -m experiments.ordinary_vm.check."""
import ctypes
import json
import os
from pathlib import Path
import random
import re
import subprocess
from tempfile import TemporaryDirectory

from specializer import ROOT, _term
from specializer.backend import generate, normalize
from specializer.replay import native
from .derive import INPUT, derive, static

HERE = Path(__file__).parent
SHEN = HERE / "interpreter.shen"
CLANG = os.environ.get("CLANG", "clang")


def source_results(source, program, seed, rows):
    command = [os.environ.get("SHEN_JIT", "shen-jit"), "eval", "-l", source]
    for row in rows:
        expression = f"(vm.fold {_term(program,'shen')} {seed} {_term(row,'shen')})"
        command.extend(["-e", expression])
    proc = subprocess.run(command, text=True, capture_output=True, check=True, timeout=30)
    return [int(line) for line in proc.stdout.splitlines() if re.fullmatch(r"-?\d+",line)]


def check_derived(work, source, fixed, rows, expected):
    seed, policy, program = fixed
    assert policy == 0
    assert source_results(source,program,seed,rows) == expected
    tree = normalize(derive(source, "vm.fold", [static(program), seed, INPUT]))
    assert str(tree).count("'while'") == 1 and "'if'" not in str(tree)
    binary = work / "derived"
    subprocess.run([CLANG, "-std=c11", "-O2", "-x", "c", "-", "-o", binary],
                   input=generate(tree), text=True, check=True, timeout=30)
    assert native(binary,rows) == [[n] for n in expected]
    return tree


def clang_control(work, rows):
    source = (HERE / "interpreter.c").read_text()
    wrapper = """
static const struct instruction fixed[] = {{PUSH_INPUT, 0}, {ADD, 0}};
uint32_t run(const uint32_t *input, size_t count) {
    return interpret(fixed, 2, 0, input, count);
}
"""
    for linkage in ("public", "private"):
        unit = source + wrapper
        if linkage == "private":
            unit = unit.replace("uint32_t interpret(", "static uint32_t interpret(")
        path = work / f"{linkage}.c"
        path.write_text(unit)
        cmd = [CLANG,"-std=c11","-O3",path]
        llvm = subprocess.check_output([*cmd,"-S","-emit-llvm","-o","-"],text=True,timeout=30)
        entry = re.search(r"^define [^\n]*@run\(.*?^}", llvm, re.S | re.M).group()
        if linkage == "private":
            assert "@interpret(" not in llvm and "@fixed" not in llvm and "switch " not in llvm
            assert "load " in entry and "add " in entry and " phi " in entry
        dylib = work / f"{linkage}.so"
        subprocess.run([*cmd,"-shared","-fPIC","-o",dylib],check=True,timeout=30)
        fn = ctypes.CDLL(str(dylib)).run
        fn.argtypes = [ctypes.POINTER(ctypes.c_uint32),ctypes.c_size_t]
        fn.restype = ctypes.c_uint32
        for row in rows:
            assert fn((ctypes.c_uint32 * len(row))(*row),len(row)) == sum(row) % 2**32
        print(f"C {linkage}: wrapper calls generic interpreter = {'@interpret(' in entry}")


def main():
    rng = random.Random(314)
    rows = [[],[5,8,13],[2**32-1,1]] + [
        [rng.randrange(2**32) for _ in range(n)] for n in range(1,19)]
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        def fixture(name):
            return json.loads((ROOT.parent / "examples/vm" / f"{name}.json").read_text())
        results = [sum(row) % 2**32 for row in rows]
        tree = check_derived(work, SHEN, fixture("sum"), rows, results)
        print("Derived sum:", json.dumps(tree))
        assert tree == json.loads((HERE / "sum.ir.json").read_text())

        def checksum(row):
            acc = 17
            for x in row:
                acc = (31*acc+x) % 2**32
            return acc
        check_derived(work, SHEN, fixture("checksum"), rows, [checksum(row) for row in rows])
        changed = work / "change.shen"
        changed.write_text(SHEN.read_text().replace("(+ A B)", "(+ (+ A B) 1)"))
        check_derived(work, changed, fixture("sum"), rows,
                      [(sum(row)+len(row)) % 2**32 for row in rows])
        clang_control(work, rows)
    print(f"Ordinary Shen and derived native: {len(rows)} rows each for sum, checksum and changed addition.")


if __name__ == "__main__":
    main()
