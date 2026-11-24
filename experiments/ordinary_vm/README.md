# Specializing an ordinary interpreter

The Shen interpreter uses numbers and lists. Its add rule pops two numbers, adds them, reduces the result modulo 2³² and pushes the answer. It has no staging primitives, expression constructors or specializer import. Run the experiment from the repository root inside the locked Nix shell.

```sh
python3 -m experiments.ordinary_vm.check
python3 -m experiments.ordinary_vm.derive experiments/ordinary_vm/interpreter.shen \
  vm.fold '[[[0],[2]],0,null]' --emit c > /tmp/derived-sum.c
clang -O2 /tmp/derived-sum.c -o /tmp/derived-sum
printf '5 8 13\n' | /tmp/derived-sum
```

The caller fixes the bytecode and initial accumulator, and marks the input list dynamic. The partial evaluator reads the ordinary source definitions. It recognizes an empty-list base case and a recursive cons case, turns the dynamic list tail into a cursor increment and the head into an input read. For a changed scalar argument in the self-call it allocates a loop-carried slot. Unchanged arguments retain their known values.

For sum, matching the helper's first instruction creates a known stack cons cell whose head is a dynamic item. Matching the second instruction pops that cell and the accumulator. Numeric addition and modulo on those symbolic scalars become residual operations. The program tail reaches the empty bytecode clause during specialization. The [result](sum.ir.json) retains one input loop and contains no program, opcode test or runtime operand stack. The existing structured IR backend lowers that result to C.

The check executes the source in Shen JIT, compiles and runs the derived program, and checks independent numeric expectations for sum, multiply/add checksum and a changed addition rule. Corpus values are unsigned 32-bit integers. The checksum multiplier is 31, so every intermediate integer fits exactly in Shen JIT's numeric representation.

**Scope.** The new partial evaluator implements a first-order Shen fragment in Python. It handles two-clause structural list folds with direct tail recursion, finite static helper recursion, list patterns with known shape, and four arithmetic primitives. Dynamic branches inside helpers and arbitrary recursion need further compiler work. The fold-to-loop transformation is built into the evaluator. The opcode rules and operand-stack behavior come entirely from the input source.

**C control.** The same experiment also supplies an ordinary C switch interpreter to Clang with a fixed constant instruction array. It inspects LLVM IR for the public and private linkage variants and executes both on the same sum corpus. Private linkage lets the optimizer see every caller. Whether a public wrapper also gets inlined depends on compiler heuristics. The gate requires the private variant to eliminate the interpreter, fixed array and switch, while retaining input loads and addition.
