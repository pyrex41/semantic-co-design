# App Shen version 1

Every application manifest declares `"language": "app-shen/1"`. The builder rejects an absent or unknown dialect and records the accepted dialect in the release ownership receipt. Keep the compiler, manifest and drivers from the originating release for historical replay.

App Shen is an application language with first-order Shen definition and clause syntax. Its reference implementation is the Python evaluator in `webapp/appc.py`; its compiler fixes static arguments and emits Cython functions linked against the CPython value ABI. The earlier VM experiment separately runs ordinary Shen through Shen JIT. Compare the guarantees of those two execution paths when interpreting correspondence results.

## Evaluation and values

* Definitions have fixed arity, ordered clauses, scalar literal patterns, single-occurrence variables and wildcards. The first matching clause wins. Function arguments and list items evaluate in order. `let` binds once, `do` sequences effects, and `if`, `and`, and `or` skip the unchosen expression. Conditions use Python value truthiness. Named effects are declared in the manifest and supplied by the host. Effect results and failures stay dynamic.
* Values include signed 64-bit integers, strings, booleans, null, lists and records. Arithmetic validates operand types and the result. Boolean operands and signed overflow raise the `integer-range` application error. Equality distinguishes types recursively in lists and records. Missing map keys or out-of-range indexed reads return null.
* `floor-div` takes the mathematical floor of the quotient and `floor-mod` returns the corresponding remainder. For example, `floor-div(-7,2) = -4` and `floor-mod(-7,2) = 1`. A zero divisor raises `zero-divisor`. Ordinary Shen's `(/ 7 2)` evaluates to 3.5, whereas App Shen's `(floor-div 7 2)` evaluates to 3. The compiler rejects the ordinary Shen division spelling. Functions requiring a fraction must choose a scale and rounding policy in their source.
* `values.py` declares the available primitives including JSON records, bytes, bounded input conversion, rendering and response constructors. Generated functions call this ABI directly. Every generated public entry accepts one dynamic argument plus its manifest-supplied static values. Most application tests execute both modes against the same primitive implementations.

## Independent evidence

`python3 example/check_language.py` extracts the actual approval decision and inventory-direction definitions from the two products and loads their text unchanged into the pinned Shen JIT. It checks six results against literal expectations and runs the same definitions through the reference evaluator and a freshly built native module. The remaining conformance probes exercise dialect-specific floor arithmetic, signed limits, nested value equality, short-circuiting and errors against independent expectations. The test also checks Shen JIT's fractional result to pin the division boundary. Full application semantics additionally require the product-specific external oracles, protocol peers and recovery histories.

## Product property checks

[Executable laws](properties.shen) take the generated input and the observed outputs as separate arguments. The [gate](check_language.py) runs the same finite domains through production definitions and through those law predicates in both App Shen execution modes. It reports the input and observation on failure. Three deliberately incorrect observations must fail. These are bounded checks of pure functions.

| Domain | Requirement projection | Additional property checked |
|---|---|---|
| Approvals | At a fixed quorum the approval status agrees with the vote count, and rejection takes precedence. | Raising quorum by one cannot turn a pending approval into an approved one. Counts 0–12, initial quorums 1–9. |
| Trading | Bid/ask rounding helpers return prices on the tick grid. | The two grid points enclose the input; each adjustment is strictly less than one tick. Integer prices 0–128 and ticks 1–16. |
| Router | A recovered NAT deadline covers the saved idle and reservation deadlines. | Recovery chooses an existing deadline, preserves all other fields and row order, and a second recovery leaves the result unchanged. The gate generates empty and two-flow lists, including legacy rows without reservations. |

The browser, exchange-process and packet-port suites separately exercise persistence and real driver boundaries. Their fixed histories establish concrete outcomes. A general state-machine invariant also needs an initial-state condition and preservation across reachable transitions. Comparing generated behavior with its interpreter alone cannot establish that both satisfy an independent requirement.
