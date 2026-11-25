"""Run from the checkout with nix develop -c python3 -m experiments.branching_vm.check."""
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
from tempfile import TemporaryDirectory

from specializer.backend import generate, normalize
from specializer.replay import native
from .pe import Unsupported, derive

HERE = Path(__file__).parent
SHEN = HERE / "interpreter.shen"
CLANG = os.environ.get("CLANG", "clang")


def assemble(text):
    """Resolve `name:` labels to instruction indexes."""
    items = [line.split() for line in text.strip().splitlines()]
    labels, code = {}, []
    for item in items:
        if item[0].endswith(":"):
            labels[item[0][:-1]] = len(code)
        else:
            code.append(item)
    return [[op, *(labels[a] if a in labels else int(a) for a in args)] for op, *args in code]


GCD = assemble("""
loop:
  load 1
  jz done
  load 0
  load 1
  mod
  load 1
  store 0
  store 1
  jmp loop
done:
  load 0
  halt
""")

COLLATZ = assemble("""
loop:
  load 0
  push 1
  eq
  jz step
  load 1
  halt
step:
  load 1
  push 1
  add
  store 1
  load 0
  push 2
  mod
  jz even
  load 0
  push 3
  mul
  push 1
  add
  store 0
  jmp loop
even:
  load 0
  push 2
  div
  store 0
  jmp loop
""")

PRIMES = assemble("""
  push 2
  store 1
outer:
  load 1
  load 0
  lt
  jz done
  push 2
  store 3
inner:
  load 1
  load 3
  load 3
  mul
  lt
  jz test
  load 2
  push 1
  add
  store 2
  jmp next
test:
  load 1
  load 3
  mod
  jz next
  load 3
  push 1
  add
  store 3
  jmp inner
next:
  load 1
  push 1
  add
  store 1
  jmp outer
done:
  load 2
  halt
""")

# The accumulator lives on the operand stack across the conditional jump.
DIGITS = assemble("""
  push 0
loop:
  load 0
  jz done
  load 0
  push 10
  mod
  add
  load 0
  push 10
  div
  store 0
  jmp loop
done:
  halt
""")

# A stack-only counter stays static while a runtime test controls the loop.
RUNAWAY = assemble("""
  push 0
loop:
  push 1
  add
  load 0
  jz done
  jmp loop
done:
  halt
""")


def collatz(n):
    steps = 0
    while n != 1:
        n, steps = (n // 2 if n % 2 == 0 else 3 * n + 1), steps + 1
    return steps


def primes(n):
    return sum(all(i % d for d in range(2, math.isqrt(i) + 1)) for i in range(2, n))


def digits(n):
    return sum(map(int, str(n)))


def shen_term(value):
    return f"[{' '.join(map(shen_term, value))}]" if isinstance(value, list) else str(value)


def source_results(source, code, locals_of, rows):
    with TemporaryDirectory() as tmp:
        driver = Path(tmp) / "run.shen"
        driver.write_text("".join(
            f"(vm.run {shen_term(code)} 0 [] {shen_term(locals_of(row))})\n" for row in rows))
        proc = subprocess.run([os.environ.get("SHEN_JIT", "shen-jit"), "eval", "-l", str(source),
                               "-l", str(driver)], text=True, capture_output=True, check=True,
                              timeout=120)
    return [int(line) for line in proc.stdout.splitlines() if re.fullmatch(r"-?\d+", line)]


def check(work, name, source, code, extra, rows, expected, blocks):
    """Interpret in Shen, specialize, compile, and compare both against the oracle."""
    arity = len(rows[0])
    assert source_results(source, code, lambda row: [*row, *extra], rows) == expected, name
    arguments = [code, 0, [], [*({"input": n} for n in range(arity)), *extra]]
    tree, count = derive(source, "vm.run", arguments)
    tree = normalize(tree)
    assert count == blocks, (name, count)
    binary = work / name
    subprocess.run([CLANG, "-std=c11", "-O2", "-x", "c", "-", "-o", binary],
                   input=generate(tree), text=True, check=True, timeout=30)
    assert native(binary, rows) == [[n] for n in expected], name
    return tree


def main():
    rng = random.Random(20261124)
    pairs = [[0, 0], [0, 7], [7, 0], [1, 1], [48, 18], [2**31 - 1, 2**31 - 2], [2**32, 2**20]]
    pairs += [[rng.randrange(2**32), rng.randrange(2**32)] for _ in range(40)]
    starts = [[n] for n in [1, 2, 3, 6, 7, 27, 97, 871, 6171, 837799]]
    starts += [[rng.randrange(1, 10**6)] for _ in range(30)]
    limits = [[n] for n in [0, 1, 2, 3, 4, 10, 30, 100, 101, 257]]
    numbers = [[n] for n in [0, 5, 10, 99, 1000, 2**40]] + [[rng.randrange(2**40)] for _ in range(30)]

    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        gcd = check(work, "gcd", SHEN, GCD, [], pairs, [math.gcd(a, b) for a, b in pairs], 3)
        assert gcd == json.loads((HERE / "gcd.ir.json").read_text())
        check(work, "collatz", SHEN, COLLATZ, [0], starts, [collatz(n) for n, in starts], 5)
        check(work, "primes", SHEN, PRIMES, [0, 0, 0], limits, [primes(n) for n, in limits], 7)
        check(work, "digits", SHEN, DIGITS, [], numbers, [digits(n) for n, in numbers], 5)

        # Change one interpreter rule. The same evaluator must follow it.
        body = SHEN.read_text()
        old = "[(+ A B) | Stack]"
        assert body.count(old) == 1
        edited = work / "edited.shen"
        edited.write_text(body.replace(old, "[(+ (+ A B) 1) | Stack]"))
        counted = [digits(n) + len(str(n)) * (n > 0) for n, in numbers]
        check(work, "digits-edited", edited, DIGITS, [], numbers, counted, 5)

        # Known limit: unbounded static variation under runtime control needs generalization.
        try:
            derive(SHEN, "vm.run", [RUNAWAY, 0, [], [{"input": 0}]], max_blocks=64)
        except Unsupported as error:
            assert "block budget" in str(error)
        else:
            raise AssertionError("expected the block budget to stop specialization")


if __name__ == "__main__":
    main()
