# Specializing an interpreter whose programs branch on runtime data

The [Shen interpreter](interpreter.shen) is an ordinary stack machine with locals, `jz` and `jmp` to any instruction index. It has no staging primitives. The partial evaluator knows nothing about it: no function names, clause shapes or loop forms. It now lives in [tool/pe.py](../../tool/pe.py), generalized; [pe.py](pe.py) here is a front for it and this check runs unchanged against it.

```sh
nix develop -c python3 -m experiments.branching_vm.check
```

## What the evaluator does

It unfolds calls until an `if` tests a runtime value. Every call in a branch of that `if` becomes a specialization point. Its arguments are generalized, and the pair of function and static skeleton names a residual block. Blocks are generated until no new pair appears. A backward jump in the object program therefore becomes a jump to an existing block. The result is lowered to the existing structured IR as one loop over a block number, so the C backend is unchanged.

Generalization is a heuristic. An argument holding no runtime value stays static (the code and the program counter). An argument holding any runtime value has all its numbers lifted, while its list shape stays static (the operand stack and the locals).

## What the check establishes

| Object program | Runtime control | Residual blocks |
|---|---|---|
| gcd | one loop | 3 |
| Collatz step count | loop with a data-dependent branch | 5 |
| prime count by trial division | nested loops | 7 |
| digit sum | accumulator on the operand stack across the jump | 5 |

Each program runs in Shen JIT, is specialized and compiled, and both answers are compared with an independent Python oracle on fixed and seeded random rows. One interpreter rule is then edited and the same evaluator must follow it. The retained [gcd residual](gcd.ir.json) is regenerated and compared.

## Limits

These are the limits of the default division this check uses. [tool/README.md](../../tool/README.md) lists what the generalized evaluator lifted (runtime pattern matching, sharing, binding-time annotations and widening) and what remains.

- **Unbounded static variation fails.** A counter kept only on the operand stack stays static while a runtime test controls the loop, so each iteration is a new block. The check asserts that this stops at the block budget. Fixing it needs binding-time analysis or widening.
- **Numbers only.** Residual values are integers. A clause pattern that inspects a runtime value is rejected, so runtime data structures, closures and strings are out.
- **Tail-recursive interpreters only.** A runtime conditional outside tail position must have total scalar arms. There are no residual calls or join points.
- **Arithmetic preconditions are unchecked.** Shen numbers are doubles and the residual is int64. They agree for non-negative values below 2^53. Division is accepted only in the form `(A - A mod B) / B`.
- **No sharing.** A value used twice is duplicated in the residual expression, so code size can grow exponentially on long straight-line runs.
- **Evidence is differential testing** on about 40 rows per program, not a proof that specialization preserves meaning.
