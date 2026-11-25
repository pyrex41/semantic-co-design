# Specializing the Gang of Four catalog

These C11 generators cover all 23 patterns from *Design Patterns* by Erich Gamma, Richard Helm, Ralph Johnson and John Vlissides. Each row identifies a configuration that can be fixed for a release and the work the generated program still does on fresh inputs. The code and examples are original. [Addison-Wesley catalog and contents](https://www.informit.com/store/design-patterns-elements-of-reusable-object-oriented-9780201633610) · [Publisher excerpt](https://www.informit.com/articles/article.aspx?p=1398601) · [O’Reilly edition with subscription](https://www.oreilly.com/library/view/design-patterns-elements/0201633612/)

The examples specialize a particular use of each pattern. A valid candidate removes repeated configuration lookup, routing, object construction, interpretation, or reflective traversal, while preserving the relationship the pattern defines. The generic implementations discussed below assume the equivalent configuration was previously resolved or traversed on each call. If the original already uses direct calls, immutable static data, or precomputed tables, some specializations mainly give operators a rebuildable configuration interface. This experiment measures behavior and inspects generated source. It makes no throughput claim.

## Creational patterns

| Pattern and profile entry | Configuration consumed at build time | Runtime behavior and cost |
|---|---|---|
| Abstract Factory · `cr_offset` | Select the family and bind one key for its reader and writer. Emit each creation operation and the corresponding operations with the family branching resolved. | Callers receive distinct reader and writer products. A value encoded by the writer can be decoded by the matching reader. Each product carries its key and performs its own arithmetic. |
| Builder · `cr_sequence` | Bind the common construction process to a sequence or numeric summary and embed its capacity. Remove the choice of representation from construction. | The director still begins, processes each input up to capacity and finishes. The sequence stores its values. The summary computes count, sum, maximum and integer mean. |
| Factory Method · `cr_adjust` | Resolve the concrete method and its constants while retaining the common creator's preparation and finalization. Emit just the selected constructor path. | The creator applies the same sequence to each caller's value and returns a fresh product. Its concrete offset or scale computation remains live. |
| Prototype · `cr_prototype` | Emit a fixed exemplar with inline storage. Remove exemplar registration, type lookup and size discovery. | The caller receives a by-value clone with an independently mutable array. Copying the fixed-size product still costs work. Heap graphs and mutable prototype registries need a different clone contract. |
| Singleton · `cr_limits` | Materialize an eagerly initialized immutable object in the linked image. There is no first-use allocation, configuration parse or initialization guard. | Every getter call returns the same opaque address for that entry in the linked image. Construction with first-use effects, per-process coordination across separately linked copies and mutation require a different implementation. This example applies where eager immutable construction is equivalent. |

[Creational profile](creational_demo.json) · [Generators](creational.py) · [Independent C consumer](creational_check.py)

## Structural patterns

| Pattern and profile entry | Configuration consumed at build time | Runtime behavior and cost |
|---|---|---|
| Adapter · `st_adapter` | Select the packed field width and linked target. Emit the appropriate shifts and masks without a format dispatch. | The adapter unpacks the caller's current word and invokes the target with two live values and the caller's context. |
| Bridge · `st_bridge` | Bind an independently selected backend hook and channel to two abstract operations. Embed the abstraction's adjustment and readiness threshold. | Use the same backend through either the adjusted value or readiness interface. Every call obtains a fresh reading and applies its abstraction's operation. |
| Composite · `st_tree` | Expand the fixed sum/max tree into ordered leaf reads and reductions. Remove branch objects, recursion and node-type dispatch. | Group and leaf ordering survive. Each occurrence reads its leaf at runtime, including a leaf that appears twice. Sum wraps at 32 bits. |
| Decorator · `st_decorated` | Flatten the configured wrappers into ordered transformations and bind the component. Remove dynamic layer traversal. | Live data undergoes outer-to-inner preprocessing, one component call and inner-to-outer postprocessing. The operations themselves still run. |
| Facade · `st_submit` | Bind three subsystem symbols to the common submit workflow. Remove per-call selection of validator, encoder and sender. | Clients use a single entry. Failure to validate prevents encoding and sending. Each reached stage receives live data, and the send status becomes the result. |
| Flyweight · `st_marks` | Intern duplicate scale/offset descriptors at generation and resolve named aliases to addresses. Remove runtime allocation and intern lookups. | Alias getters return equal addresses for equal intrinsic descriptors. Applying one to a caller's position uses the same shared data with live extrinsic input. Supply a descriptor obtained from that entry. |
| Proxy · `st_guard` | Embed the role mask and bind the target. Remove policy parsing and runtime subject selection. | Check each caller's role before invocation. Denial leaves the output untouched and prevents any target effects. An empty allowed set emits unconditional denial and needs no linked target. |

[Structural profile](structural_demo.json) · [Generators](structural.py) · [Independent C consumer](structural_check.py)

## Behavioral patterns

| Pattern and profile entry | Configuration consumed at build time | Runtime behavior and cost |
|---|---|---|
| Chain of Responsibility · `bh_dispatch` | Reuse the existing Shen generator to emit comparisons in handler order. Remove handler links and successor calls. | The first matching handler still wins. Overlapping thresholds, the fallback and malformed input remain observable. |
| Command · `bh_credit`, `bh_audit` | Bind the implementation of a request and its optional inverse. Remove opcode lookup and receiver-method selection during command construction. | Each value carries a live receiver and argument. A caller can enqueue mixed commands, invoke them later and request compensation where supported. Heterogeneous use still calls a stored function pointer. The caller owns ordering and receiver lifetime. |
| Interpreter · `bh_invoice`, `bh_constant` | Walk the expression at generation, fold constant subexpressions modulo 2³² and emit indexed operands and arithmetic. Remove AST allocation by the consumer, recursion and operator dispatch. | The compiled expression reads fresh operands, validates the input shape and evaluates the remaining arithmetic. Fully constant expressions require no operands. |
| Iterator · `bh_reading` | Embed record stride and projection offset. Remove runtime layout selection and indirect accessors. | Callers create independent cursors over live buffers. Each `next` advances one whole record. Incomplete trailing records are skipped. Buffers must outlive their cursors. |
| Mediator · `bh_checkout` | Resolve named routes into ordered calls to colleagues. Remove event-name lookup and participant dispatch. | One colleague's runtime result feeds the next. A route with no participants returns the input. Colleagues can keep independent behavior without knowing the other participants. |
| Memento · `bh_checkpoint` | Choose snapshot fields and emit matching storage, copies and restores. Remove reflective field enumeration and runtime field-name lookup. | Capture records the originator's current values. A caretaker can hold multiple opaque snapshots, restore any of them, and destroy them. Allocation and copying remain. Unselected fields stay untouched at restore. |
| Observer · `bh_publish` | Resolve known callbacks, notification order and tag masks. Remove linked-list and callback lookup. | Callers attach and detach subscribers on each registry. Notifications call all matching subscribers active at the start of dispatch. Changes during a callback affect the next notification. Registries are single-threaded. |
| State · `bh_door` | Compile one state switch per named event and bind transition actions. Remove event-name lookup and state-object dispatch. | Current state, event payload and action effects are live. A valid transition updates the state before calling its action. Invalid transitions leave it untouched and return false. |
| Strategy · `bh_sort` | Select insertion or selection sort and sort direction. Remove strategy object selection and indirect comparator calls. | The chosen algorithm still makes comparisons and mutates the caller's array. Both strategies produce the same numeric ordering for a fixed direction. |
| Template Method · `bh_job` | Bind the three overridable operations and the admission threshold to one fixed sequence. Remove hook lookup and virtual calls. | All calls with an output pointer prepare and finish. The admitted path also calls compute and writes the result. The finish callback receives the admission outcome. |
| Visitor · `bh_visit` | Bind the visitor's method for each concrete element kind. Remove indirect visitor-method calls. | Different configured visitors can use the same elements. Dispatch on the live tag remains, and each method receives the fields of its concrete element kind. |

[Behavioral profile](behavioral_demo.json) · [Generators](behavioral.py) · [Independent C consumer](behavioral_check.py)

## Build and inspect an ordered review queue

An application could keep records of the form `{minimum, queue, next}` and traverse `next` until the current amount meets `minimum`. For a fixed chain of two handlers, the generated C has a length check and two ordered comparisons. Omitting casts and bounds guards, the generated function is equivalent to the snippet below. A value of 125 satisfies both policies and selects queue 2. Reversing the two static handlers and rebuilding selects queue 1 for the same value. [Shen handler definitions](../spec.shen) · [Both linked policies](behavioral_check.py)

```c
if (amount >= 100u) return 2;
if (amount >= 10u) return 1;
return 0;
```

From the repository root inside `nix develop`, build that entry and link a C caller. The caller can use the same integration when profile thresholds or order change.

```sh
work=$(mktemp -d)
cat > "$work/review.json" <<'JSON'
{"review": {"pattern": "chain_of_responsibility", "handlers": [[100, 2], [10, 1]], "default": 0}}
JSON
python3 -m experiments.patterns.gof.build "$work/review.json" --out "$work/review"
cat > "$work/client.c" <<'C'
#include "gof.h"
#include <inttypes.h>
#include <stdio.h>

int main(void) {
    uint32_t amount = 125;
    printf("queue=%" PRId64 "\n", gof_review(&amount, 1));
    return 0;
}
C
clang -std=c11 -I"$work/review" "$work/client.c" "$work/review/libgof.a" -o "$work/client"
"$work/client"
# queue=2
```

The build writes `gof.h`, `gof.c` and `libgof.a` to a fresh directory. Use any combination of the profile entries, with new names as needed. Profile keys become public C prefixes, so `review` becomes `gof_review`. Keep generated C names distinct. For entries that call external code, `gof.c` declares the exact C hook signatures. The archive contains one object, so link every hook referenced by the chosen profile or build a smaller profile. A changed profile can change generated types as well as behavior. Recompile consumers against the matching header before linking. [Build command](build.py)

## Reproduce the checks

```sh
nix develop --command python3 -m experiments.patterns.gof.check
```

The checker compiles base and changed profiles for all three families and one mixed profile. It links independent C consumers against the generated headers and archives. It checks live effects, including two builder representations, clone independence, singleton and flyweight identities, validation and authorization failures, ordered hooks, command inversion, observer mutation during dispatch and multiple snapshots. It also checks source fragments such as folded literals, distinct selected sort implementations and direct subsystem calls. The check is bounded to the supported C interfaces and configuration model. [Checker](check.py)
