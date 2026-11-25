# Chapters 4 and 5. Evaluators, logic, and register machines

The examples use original Python programs and terminology from Abelson and Sussman with Julie Sussman, *Structure and Interpretation of Computer Programs*, second edition. [MIT Press text](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-4.html) · [Original Scheme code](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/code/index.html)

| Mechanism | Public entry point | Bound once | Per invocation |
|---|---|---|---|
| Syntax analysis and lexical addressing | [`compile_evaluator`](evaluator.py) | Expression forms, `(frame, slot)` addresses, direct primitive calls, lexical initialization order | Input values, mutable frames, closure identity, branches and higher-order procedure selection |
| Call-by-need | `compile_evaluator(..., lazy=True)` | Which applications can delay arguments and where values are demanded | Memoized thunks over caller frames, demand chains, and effects in force order |
| Explicit control and procedure linkage | `compile_evaluator`, `apply_procedure` | Last expression and branch targets, primitive entries, compound body entries | Runtime continuation stack, tail jumps, and host or compound procedures passed as values |
| Backtracking with reversible assignment | [`compile_amb`](amb.py) | Choice and continuation graph, primitive callables, variable slots | Trial values, ordered answers, failure and undo; `permanent-set!` records an update across failures |
| Rule-based query | [`compile_query`](query.py) | Relation arities and rule buckets, syntax and nested term templates, query variable positions and host guards | Facts, fresh rule instances, unification frames, proof search and answer rows |
| Register-machine assembly | [`compile_machine`](machine.py) | Register indices, label addresses, operation objects and instruction execution functions | Register contents, flag, continuation addresses, shared stack and effects |
| Register preservation | `InstructionSequence`, `preserving` | Declared register effects and live values that need save/restore | Assembled code size and dynamic stack behavior follow from the chosen controller |
| Copying collection | [`compile_collector`](memory.py) | Root slots and each record's atom/reference/ambiguous fields | Root values, reachable objects, cycle and alias resolution through forwarding addresses |

The evaluator covers literals, quote, lambda, `call`, `if`, `begin`, `set!` and `letrec`. Only `False` is false. `letrec` initializes cells left to right and diagnoses an early read of an unassigned cell. Names declared in the fixed primitive map can be shadowed by lexical bindings. Host callables receive evaluated values. The dispatch loop transfers control to the final body or branch without pushing a return continuation for that transfer. Deferred values and non-tail calls can require storage proportional to their pending work. Unused operands in lazy compound calls stay unevaluated, while primitive arguments and predicates force values. Forcing memoizes successful results and reports recursive demand for the same unresolved thunk.

The `amb` language uses a single indexed frame and calls supplied primitives. Its generators implement depth-first, left-to-right success and failure continuations. `amb-from` takes alternatives from a live iterable as they are requested. An infinite depth-first branch can keep later alternatives from running. Assignment changes are visible along the successful branch and reversed when that branch is left. Host-side effects and permanent assignments persist for the duration of the search. Queries use tuples as first-order terms, snapshot finite fact relations, and enumerate proofs through a breadth-first worklist. For finite branching, every finite positive proof is reachable, including when a recursive alternative never succeeds. Proof multiplicity survives. Breadth-first scheduling can change answer order relative to SICP's interleaved streams. Filters require ground inputs, and a negative query must finish or find a witness to decide. Use the optional `budget=` to bound goal expansions. Relation names, query syntax, and rules are fixed per profile. Rule activation standardizes variables apart, and unification rejects cycles in terms.

The register simulator accepts the book's assign, test, branch, goto, save, restore and perform operations with tuple syntax, plus halt. It returns selected registers and executed-instruction, push and maximum-depth counters. A computed goto takes an address from the same assembled machine. `preserving` composes fall-through fragments from caller-supplied conservative register effects. It wraps only modified registers the second fragment reads and whose original values the caller asks to preserve. Collection copies live records into a new heap and uses a forwarding map. References are explicit `Ref` values at scanned record positions. Application-specific atoms are opaque, and validation visits reachable rows. Collection leaves the caller's heap available as an independent input for another call.

```python
from operator import add, mul
from experiments.patterns.sicp.evaluator import compile_evaluator
from experiments.patterns.sicp.query import compile_query

calculate = ("call", "combine", "quantity", 10)
price = compile_evaluator(calculate, ("quantity",), primitives={"combine": mul})
surcharge = compile_evaluator(calculate, ("quantity",), primitives={"combine": add})
assert (price(4), surcharge(4), price(6)) == (40, 14, 60)

orders = compile_query(("and", ("order", "?id", "?sku"), ("stock", "?sku")),
                       {"order": 2, "stock": 1}, project=("?id",))
assert list(orders({"order": [(7, "bolt"), (8, "nut")], "stock": [("bolt",)]})) == [(7,)]
```

## Section coverage

| Section | Treatment and rationale |
|---|---|
| [4.1 The Metacircular Evaluator][41] | The common expression fragment supplies the evaluator and compilation examples. |
| [4.1.1 Eval and apply][411] | Bind the form dispatch at analysis and preserve value-dependent application and environments. |
| [4.1.2 Expression syntax][412] | Analyze quoted, conditional, sequential, lambda, assignment and call forms once. Derived-form expansion beyond the fixed tuple syntax shares the same staging boundary. |
| [4.1.3 Evaluator data structures][413] | Executable closures capture mutable frames and preserve aliasing. Procedure representation is shared with explicit control. |
| [4.1.4 Running the evaluator][414] | Primitives are captured from a supplied dictionary. A read-eval-print loop accepts code dynamically and has no fixed program at this boundary. |
| [4.1.5 Data as programs][415] | A fixed program is traversed and becomes execution procedures. Runtime generation and evaluation of new code require a new analysis phase. |
| [4.1.6 Internal definitions][416] | `letrec` allocates the whole recursive frame before evaluating initializers, so mutually recursive procedures can close over their forward references. |
| [4.1.7 Analysis before execution][417] | `compile_evaluator` builds a reusable procedure that has already resolved syntax and names. |
| [4.2 Lazy Evaluation][42] | Staging preserves the force policy and defers value-dependent work. |
| [4.2.1 Normal and applicative order][421] | The same expression can be built in strict and lazy modes, with different observable effects. |
| [4.2.2 Lazy evaluator][422] | A memoized thunk retains its code and environment until forced; the explicit evaluator stack resolves chains. |
| [4.2.3 Streams as lazy lists][423] | Lazy constructor composition can use compound calls. The separate [stream library](STATE.md) gives fixed delayed list operations and feedback. |
| [4.3 Nondeterministic Computing][43] | Compile an ordered choice program and enumerate its successful results. |
| [4.3.1 Amb and search][431] | Choice order is static. Failure and successive answers depend on each invocation's values. |
| [4.3.2 Nondeterministic programs][432] | Puzzles and parsing supply different choices and predicates to the same search mechanism. A fixed `amb-from` expression can enumerate dynamic, potentially infinite alternatives. |
| [4.3.3 Amb evaluator][433] | Generate search procedures from syntax and restore assignment on backtracking. Host primitives provide application-specific predicates. |
| [4.4 Logic Programming][44] | Stage a first-order query and reusable rule base while accepting fresh relation contents. |
| [4.4.1 Deductive retrieval][441] | Compile relation lookup, conjunction, disjunction, host guards, and ground negation. |
| [4.4.2 Query execution][442] | Retain runtime matching, frame joins, freshening on rule application, and unification. |
| [4.4.3 Query semantics][443] | Cycles, duplicate proofs and negation still have operational consequences. Budget exhaustion is an error, and a known proof can arrive while other branches diverge. |
| [4.4.4 Query implementation][444] | Captured rule buckets and predicate positions replace dispatch on query syntax. Breadth-first proof scheduling is this profile's fairness policy. |
| [5.1 Designing Register Machines][51] | Assemble a fixed controller and reuse it with fresh register contents. |
| [5.1.1 Controller language][511] | Controller labels, operations and registers become addresses, execution closures and indexed slots. |
| [5.1.2 Machine design abstraction][512] | An operation is a supplied callable bound during assembly; its results and effects remain dynamic. |
| [5.1.3 Subroutines][513] | The same subroutine entry can be reached with different continuation addresses in a register. |
| [5.1.4 Stack and recursion][514] | The runtime stack preserves register contents and continuation addresses before a recursive call. |
| [5.1.5 Instruction summary][515] | One changed-controller fixture exercises all the listed instruction families. |
| [5.2 Register-Machine Simulator][52] | Generate execution procedures for each instruction. |
| [5.2.1 Machine model][521] | Program counter, flag, registers and stack remain live state for the residual machine. |
| [5.2.2 Assembler][522] | Resolve forward and backward labels and reject duplicate labels at setup. |
| [5.2.3 Instruction procedures][523] | Pre-bind operation operands and instruction dispatch; indirect jumps still inspect the continuation register. |
| [5.2.4 Monitoring][524] | Collect dynamic step, push and maximum stack-depth counts after assembling a fixed program. |
| [5.3 Storage Allocation and Garbage Collection][53] | Bind root and record layouts for copying. |
| [5.3.1 Memory as vectors][531] | The reference index plays the role of a Scheme vector address. Fixed fields become indexed readers. |
| [5.3.2 Stop-and-copy][532] | Breadth-first scanning forwards each live object once and preserves cycles and shared references. Heap pressure and repeated runtime heap management would be separate policy inputs. |
| [5.4 Explicit-Control Evaluator][54] | Run compiled expression procedures in an explicit continuation loop. |
| [5.4.1 Core of explicit control][541] | Make demand, application, return and continuation operations explicit while specializing syntactic dispatch. |
| [5.4.2 Sequence and tail recursion][542] | Execute prefixes with pending continuations and jump directly to final expressions. |
| [5.4.3 Special forms][543] | Fix conditional branches, mutation addresses and recursive binding layout. |
| [5.4.4 Running the evaluator][544] | Repeated calls and deep recursion exercise explicit control. The register model separately exposes runtime stack counts. |
| [5.5 Compilation][55] | The evaluator emits Python execution functions. The machine composes and assembles fixed register fragments. |
| [5.5.1 Compiler structure][551] | Analysis recursively selects a target operation for each syntax node. Control is made explicit by jumps and suspended continuations. |
| [5.5.2 Compiling expressions][552] | Capture constants, addresses, body entries and pending continuations for core forms. |
| [5.5.3 Compiling application][553] | Direct primitive calls bind once, while first-class procedures retain value-dependent dispatch. |
| [5.5.4 Combining instruction sequences][554] | `preserving` uses static register effects to insert the required saves and restores before assembly. |
| [5.5.5 Compiled factorial][555] | The simulator checker builds factorial and a changed arithmetic controller, observing their outputs and stack counts. |
| [5.5.6 Lexical addressing][556] | Compile each local variable to a depth and slot while retaining mutable values. |
| [5.5.7 Interfacing compiled and interpreted code][557] | Host callables and compiled closures can compose through the value application interface. The book's Scheme REPL and compiler linkage protocol are outside this Python profile. |

[41]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html
[411]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.1
[412]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.2
[413]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.3
[414]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.4
[415]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.5
[416]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.6
[417]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-26.html#%25_sec_4.1.7
[42]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-27.html
[421]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-27.html#%25_sec_4.2.1
[422]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-27.html#%25_sec_4.2.2
[423]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-27.html#%25_sec_4.2.3
[43]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-28.html
[431]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-28.html#%25_sec_4.3.1
[432]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-28.html#%25_sec_4.3.2
[433]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-28.html#%25_sec_4.3.3
[44]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-29.html
[441]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-29.html#%25_sec_4.4.1
[442]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-29.html#%25_sec_4.4.2
[443]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-29.html#%25_sec_4.4.3
[444]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-29.html#%25_sec_4.4.4
[51]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-31.html
[511]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-31.html#%25_sec_5.1.1
[512]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-31.html#%25_sec_5.1.2
[513]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-31.html#%25_sec_5.1.3
[514]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-31.html#%25_sec_5.1.4
[515]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-31.html#%25_sec_5.1.5
[52]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-32.html
[521]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-32.html#%25_sec_5.2.1
[522]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-32.html#%25_sec_5.2.2
[523]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-32.html#%25_sec_5.2.3
[524]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-32.html#%25_sec_5.2.4
[53]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-33.html
[531]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-33.html#%25_sec_5.3.1
[532]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-33.html#%25_sec_5.3.2
[54]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-34.html
[541]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-34.html#%25_sec_5.4.1
[542]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-34.html#%25_sec_5.4.2
[543]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-34.html#%25_sec_5.4.3
[544]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-34.html#%25_sec_5.4.4
[55]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html
[551]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.1
[552]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.2
[553]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.3
[554]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.4
[555]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.5
[556]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.6
[557]: https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/full-text/book/book-Z-H-35.html#%25_sec_5.5.7
