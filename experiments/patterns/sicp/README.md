# SICP specialization patterns

A fixed profile describes a computation, representation, or network. Bind it once to obtain a reusable callable or factory. Calls supply fresh data and carry out any still-dynamic calculation, effects, search, or scheduling. The chapter catalogs account for all 94 numbered subsections of Abelson and Sussman with Julie Sussman's *Structure and Interpretation of Computer Programs*, second edition. Each records which ideas provide a distinct staging boundary, which share a mechanism, and where the profile ends. [MIT Press second-edition text](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-4.html) · [Original Scheme sources](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/code/index.html)

| Chapters | Read and reuse |
|---|---|
| 1–2. Procedures and data | [Numerical methods, representations, data-directed dispatch, tree schedules, pictures and coding](PROCEDURES-DATA.md) |
| 3. State and streams | [Local state, serialized transfers, table aliases, circuit agendas, connector propagation and delayed streams](STATE.md) |
| 4–5. Evaluators and machines | [Strict and lazy execution, backtracking, logic queries, register assembly and preservation, and copying collection](LANGUAGES.md) |

The new APIs return Python execution procedures or generate Python functions from the profile. The [linked C sequence pipeline](../README.md) is the existing native implementation of §2.2.3. It uses Shen to generate a fused traversal and the shared backend to build a C11 static library.

```python
from operator import add, mul
from experiments.patterns.sicp.evaluator import compile_evaluator

expression = ("call", "combine", "units", 10)
price = compile_evaluator(expression, ("units",), primitives={"combine": mul})
surcharge = compile_evaluator(expression, ("units",), primitives={"combine": add})
assert [price(n) for n in (4, 7)] == [40, 70]
assert [surcharge(n) for n in (4, 7)] == [14, 17]
```

From the repository root, run both checkers inside the development shell:

```sh
nix develop -c python3 -m experiments.patterns.sicp.check
nix develop -c python3 -m experiments.patterns.check
```

The chapter checker exercises changed static profiles against independent outcomes. It checks dynamic cases including rounding-free algebra, ordered agenda traces, thread serialization, aliasing, conflict and retraction, shared lazy demand, backtracking undo, recursive query proof multiplicity, and machine stack behavior. The linked C checker verifies the existing native pipeline along with its sibling examples. The reported counts are correctness and operation counts for the fixtures.
