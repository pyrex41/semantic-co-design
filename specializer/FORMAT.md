# Explicit staging interface

An example directory supplies `manifest.json`, `spec.shen` and `spec.maude`. The manifest declares the `specialize` entry and describes the static argument, dynamic argument and result types. The static fixture is one JSON value composed of integers and lists. The Shen entry takes the fixture as a list term. The Maude module `SPEC` protects `STAGE` and defines `specialize : T -> T`. For example, the JSON value `[1,[2,3],[]]` is `box(1 box(2 3) box(nil))` in Maude.

The language runtime executes the static traversal and returns a statement tree. In concrete mode the same language's evaluator executes the tree on a dynamic row. In build mode the shared Python pass validates the vocabulary, folds constant arithmetic and conditions, flattens sequences, and lowers the residual program to C. Clang produces the standalone executable. The emitted IR is the normalized JSON tree.

| Expression | Meaning |
|---|---|
| integer | Signed 64-bit literal |
| `["var", slot]`, `["len"]`, `["at", index]` | Scalar slot, input length, indexed input value |
| `["get", bank, index]` | Indexed scratch value |
| `[op, a, b]` | add, sub, mul, div, mod, eq, lt, le, and, or |
| `["u32", a]` | Low unsigned 32 bits |

Signed intermediate results must fit in 64 bits. Division truncates toward zero and requires a nonzero divisor. Booleans are integers with zero false and comparisons returning zero or one. The VM splits its modular multiplication to keep intermediate products in range. The Shen adapter uses two exact limbs for large integers because its host number type is a double.

| Statement | Effect |
|---|---|
| `["seq", ...statements]` | Run in order |
| `["set", slot, expr]`, `["put", bank, index, expr]` | Write a scalar or scratch element |
| `["emit", expr]` | Append an output integer |
| `["if", cond, then, else]`, `["while", cond, body]` | Structured control flow |
| `["trap"]` | Abandon the row: the source has no defined result here |

Slot and bank IDs are integer literals in 0–1023, and scratch indexes are in 0–65535. Each new input row starts with zeroed storage. Source programs must terminate and keep input reads within the row. The native driver reads a line of up to 65,536 whitespace-separated integers and emits up to 65,536 integers on a line. An empty line represents an empty vector. Input framing, widths and other domain preconditions are described by each example.

The Python entry points are `build(example, source, static, binary, *, emit_ir=None, emit_c=None, directory=None)` and `concrete(example, source, static, rows, *, directory=None)`. An explicit directory lets tests rebuild edited sources with the same compiler. The fifth-example test supplies a new directory and verifies both runtimes and native executables without registering the example.
