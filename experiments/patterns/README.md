# Specializing patterns from three programming books

A pattern is a good specialization candidate when an application knows part of the answer before the next request arrives. A parser can know its grammar, an evaluator can know the program, and a router can know the order of its handlers. Fix that information once and the caller can reuse the resulting library on many inputs.

## Book catalogs

| Book | Implemented | What you can reuse |
|---|---:|---|
| [Gang of Four](gof/README.md) | 23 named patterns | A C11 static library generated from a JSON profile, with public declarations and hooks for application code. |
| [SICP](sicp/README.md) | 32 mechanism groups | Python procedures, factories and compiled schedules for fixed expressions, data representations, constraint networks and machines. The inventory covers all five chapters. |
| [Norvig’s PAIP](paip/README.md) | 20 mechanism families | Generated Python functions for rules, grammars, search, algebra, knowledge representation and language compilation. The inventory covers all 25 chapters. |

The table counts 75 catalog entries. GoF supplies its own named list. For SICP and PAIP we grouped related applications that share a specialization mechanism. The three original Shen-to-C examples below illustrate overlapping ideas.

Run `python3 -m experiments.patterns` from the repository root inside `nix develop` to exercise all three catalogs and the original C library. The individual book guides give smaller commands and identify the configuration each compiler consumes, the work it removes and the behavior the application must still supply.

[The invoice router](workflow.py) uses all three libraries. A PAIP rule accepts an invoice whose confirmation repeats its ID. A SICP expression prices the accepted units. A C client linked against the GoF archive sends the amount to the first review queue whose threshold it meets. Run `python3 -m experiments.patterns.workflow` inside the development shell to see two invoices routed and one rejected; the example also checks a second price and queue policy.

## Start with the three-pattern C library

A review service can let operators write ordered rules while keeping a small request path. For this library the operator edits a JSON profile. At build time Shen walks the rules and emits a C function for each named entry. A C caller passes fresh data to those functions. Changing the profile requires rebuilding and relinking the library.

### Books and examples

| Book and legal reading copy | Idea used here | What survives in the example |
|---|---|---|
| Gamma, Helm, Johnson and Vlissides, *Design Patterns*. [Full O’Reilly edition, access required](https://www.oreilly.com/library/view/design-patterns-elements/0201633612/) · [Free publisher excerpt](https://www.informit.com/articles/article.aspx?p=1398601) | Chain of Responsibility. The first handler that accepts an amount chooses its review queue. Handler order is significant when thresholds overlap. | Ordered comparisons against the incoming amount. The successor links disappear. |
| Abelson and Sussman with Julie Sussman, *Structure and Interpretation of Computer Programs*, 2nd ed. [Free MIT full text](https://mitp-content-server.mit.edu/books/content/sectbyfn/books_pres_0/6515/sicp.zip/index.html) · [Sequence pipelines, §2.2.3](https://sicp.sourceacademy.org/chapters/2.2.3.html) | Compose filtering, mapping and accumulation. The example keeps charges of at least 10, adds 2 to each, and sums them. | One loop over the charges. Filter and map execute in the configured order without allocating intermediate sequences. |
| Peter Norvig, *Paradigms of Artificial Intelligence Programming: Case Studies in Common Lisp*. [Author’s full text and code](https://github.com/norvig/paip-lisp) · [Pattern matcher, chapter 6](https://github.com/norvig/paip-lisp/blob/main/docs/chapter6.md) | Bind a variable once and require repeated occurrences to agree. The example recognizes a four-word transfer event and returns its ID. | A length check, a tag check, equality between two input positions, and the capture read. Variable names and the binding dictionary disappear. |

These are original, deliberately small adaptations. The matcher covers flat, fixed-length patterns with literals, wildcards and named variables. The generator implements each pattern in explicitly staged Shen. It uses the existing integer IR and C lowering. [Example profile](profile.json) · [Specialization definitions](spec.shen)

## Build and call

From the repository root inside `nix develop`:

```sh
work=$(mktemp -d)
python3 -m experiments.patterns.build experiments/patterns/profile.json --out "$work/lib"
clang -std=c11 -I"$work/lib" experiments/patterns/client.c "$work/lib/libpatterns.a" -o "$work/client"
"$work/client"
# queue=2 total=34 transfer=42
```

Each JSON key declares a function named `pat_<key>`. Keys use lowercase letters, digits and underscores, begin with a letter, and have a maximum length of 48. All functions accept `const uint32_t *input, size_t length` and return `int64_t`. Results include all unsigned 32-bit values and the sentinel `-1`. The caller owns the input. Empty input may use a null pointer. A null pointer with positive length or a length exceeding `UINT32_MAX` returns `-1`. Calls allocate no heap memory and share no mutable state. The build writes the header, generated C and static archive to a fresh directory.

* **handlers** takes up to 32 ordered `[minimum, result]` pairs and a `default`. Its input must be a single word. An amount of 125 matches both thresholds in the example. Swapping the handlers changes its result from 2 to 1.
* **sum** takes up to 32 `["keep_ge", n]` or `["add", n]` steps. Addition and the final sum wrap modulo 2³². An empty sequence sums to zero.
* **match** takes up to 32 tokens. An integer matches itself, `"_"` matches any word and `"?id"` names a binding. An optional `capture` selects a bound value. Success without a capture returns 1. Failure returns `-1`. Both `0` and `4294967295` are valid captures.

Run `python3 -m experiments.patterns.check` to link a C consumer against three profiles. The checker compares 3,150 results with independent implementations, including reordered handlers and pipeline stages, repeated variables, empty profiles for each family, and wraparound. The Nix staged-examples gate includes the check.

## Where this helps in a real application

The compiled-kernel benchmark found handwritten C fastest on all four fixtures. For the equality join, specialized Shen took 0.100 µs/row and handwritten C took 0.088. For the decoder the gap was much larger, and changing a single scratch reset accounted for most of it. These observations motivate inspecting the generated code before choosing a rewrite. [Compiled benchmark](../../benchmarks/README.md)

For a different measurement, the trading benchmark records wall time inside the generated delta transition and inside the enclosing durable call on the same event. Three independent runs each ingested 256 frames against local SQLite WAL with FULL synchronization. The transition accounted for 4.9–7.7% of total durable-call time. Holding everything else fixed, even eliminating the transition entirely would speed up that path by only 1.05–1.08×. The remaining time includes host work and persistence. A redesign of state representation or durability could change those costs. This experiment measures a single feed path and uses a simulated venue. [Raw runs](trading-feed-arm64.json) · [Benchmark code](../../example/trading-engine/benchmark.py)

Use this pattern library when rules are stable for the lifetime of a release, change often enough to justify a generator, and can be checked independently. If users must edit a rule and see it take effect immediately, versioned runtime configuration may fit better. For a fixed hot kernel under a hard latency budget, handwritten C is a reasonable choice. The same application can use all three approaches.
