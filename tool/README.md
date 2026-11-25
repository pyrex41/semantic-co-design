# tool: one Shen specification, compiled, simulated and explored

A developer writes the behavior of a node as ordinary Shen. This package

1. specializes that Shen to a native step function ([compile.py](compile.py), [pe.py](pe.py)),
2. runs the same native code under a deterministic simulator that owns the clock, the network, the disks, crashes and the schedule ([sim/](sim/)),
3. checks every simulated step against the Shen source in shen-jit and evaluates invariants written in Shen over the traces ([check.py](check.py)),
4. translates the same Shen source to Maude and searches the same environment exhaustively within a bound ([maude.py](maude.py)).

```sh
nix develop -c python3 -m tool check kernel         # the compiler's own checks
nix develop -c python3 -m tool check accepted_job   # the first application
```

## 1. Specification convention

A node is one pure Shen function and one initial value.

```shen
(define <init>  NodeId NodeCount -> State)
(define <step>  State Event -> [State' Effects])
```

* **State** is a list of fixed shape whose leaves are numbers, symbols or booleans. The shape never changes at run time. A bounded collection is a list with a declared number of slots; the specification keeps its own occupancy marks (a status symbol or a count) inside the slots.
* **Event** is a tagged list `[tag field ...]`. Everything nondeterministic reaches the node only as an event: a client request, a message arrival, the acknowledgement of a disk write, the result of a disk read, a timer, a clock reading, a restart.
* **Effects** is a list of tagged lists `[tag field ...]`, at most `max_effects` long. Every external action leaves the node only as an effect: send, reply, disk write, disk read, set or cancel a timer, read the clock.
* **Durable state** is what the node asked the disk to write and the disk has made durable. An acknowledgement event tells the node a write is durable; before that event the node may not rely on it. Memory is lost at a crash: the simulator replaces the state with `<init>` and delivers the `restart` event, and the specification says there what it reads back.
* The specification is ordinary Shen that computes values. It never mentions residual code, the simulator, or integers standing for symbols. `shen-jit` runs the same file unchanged, and that run is the reference for the native code.

The step must be total on every state and event the system can reach. A Shen error (no clause matches, `simple-error`, division by zero) is a *trap*: the native step returns an error code, and the simulator reports it as a failure of the run.

### The manifest

`node.json` sits next to the specification. The compiler and the simulator both read it, so the integer meaning of a symbol, an event or a state field is declared once.

```json
{
  "name": "accepted_job",
  "spec": "spec.shen", "step": "job.step", "init": "job.init",
  "symbols": ["unknown", "free", "stored", "idle", "writing", "ok", "conflict", "full"],
  "state": [{"capacity": 2, "of": ["sym", "num", "num"]},
            ["sym", "num", "num", "num", "num", "num"],
            "num"],
  "events":  {"submit":  {"role": "request",    "fields": ["num", "num", "num"]},
              "written": {"role": "disk-ack",   "fields": ["num"]}},
  "effects": {"write":   {"role": "disk-write", "fields": ["num", "num", "num", "num"]},
              "reply":   {"role": "output",     "fields": ["num", "sym", "num"]}},
  "max_effects": 2,
  "invariants": {"file": "invariants.shen", "state": ["inv.a"], "trace": ["inv.b"]}
}
```

A shape is `"num"`, `"sym"`, `"bool"`, a list of shapes, or `{"capacity": n, "of": shape}` for `n` slots. `symbols` lists every symbol that can be a runtime value; event and effect tags are appended automatically. Symbol codes are the 1-based positions in that table, so zero padding is never a symbol.

### Roles

The simulator knows nothing about an application. It acts on the *role* of each effect and produces events by role. The leading fields of an effect or event with a role are fixed; anything after them is the application's.

| Effect role | Fields | What the simulator does |
|---|---|---|
| `output` | any | A message to the outside world. It may be delayed, dropped or duplicated before the client sees it. |
| `send` (`"deliver": tag`) | `Dest`, payload | Queues the `recv` event `[tag Src payload]` for node `Dest`. May be delayed, dropped, duplicated, reordered or cut by a partition. |
| `disk-write` | `Token`, `Key`, value | Appends to the node's pending writes. A later *sync* makes the row durable and queues `[ack Token]`. |
| `disk-read` | `Token`, `Key` | Queues `[result Token Key Found value]` from the durable rows (`Found` is 1 or 0; a missing row reads as zeros). Pending writes are not visible to reads. |
| `timer-set` | `Id`, `Delay` | Replaces any pending timer `Id` and queues `[timer Id]` at clock + `Delay`. |
| `timer-cancel` | `Id` | Removes pending timer `Id`. |
| `clock-read` | `Token` | Queues `[clock Token Now]`. |

| Event role | Fields | Source |
|---|---|---|
| `request` | any | The workload. |
| `recv` | `Src`, payload | A `send` effect. |
| `disk-ack` | `Token` | A synced write. Dropped if the node crashed first. |
| `disk-read-result` | `Token`, `Key`, `Found`, value | A `disk-read` effect. |
| `timer` | `Id` | A `timer-set` effect. |
| `clock` | `Token`, `Now` | A `clock-read` effect. |
| `restart` | none | Delivered first after boot and after every crash. |

## 2. Compiler

`pe.py` is the partial evaluator that was `experiments/branching_vm/pe.py`, generalized. That file is now a nine-line front for it and `experiments/branching_vm/check.py` passes unchanged against it. What it gained:

* **Clause selection on runtime data.** A literal pattern, a repeated pattern variable or `=` that meets a runtime leaf becomes a residual test, and the clauses of the function become residual branches in source order. A `where` guard is evaluated only when its patterns match. When no clause is left the residual code traps, as Shen raises.
* **Typed leaves and one symbol table.** A runtime leaf is a number, a symbol or a boolean. A symbol is compared by its code from the manifest table; a number never equals a symbol. A symbol that meets runtime data and is not in the table is a compile error.
* **Joins instead of duplication.** A runtime conditional in value position evaluates both arms and joins them leaf by leaf into temporaries. Where the two arms have different shapes (two effect lists of different length) the join keeps a `Split`, resolved only where something inspects it or at the return, by re-testing the condition.
* **Sharing.** Residual expressions are hash-consed per control-flow scope and counted. A node used more than once is bound to a slot where it was created.
* **Binding times at specialization points.** A specialization point is a tail call under runtime control that re-enters a function already being unfolded. The default division is the old one (an argument holding runtime data has its numbers lifted, a fully static argument stays static). Two controls replace it where it is wrong: `division={"f": ["S", "D", None]}` fixes the binding time of an argument at the entry, and `widen=k` turns a static number into a runtime value once it has taken more than `k` distinct values at one position of one argument shape.
* **Event dispatch.** The entry of a compiled step tests the runtime event tag against each declared event kind and specializes the step once per kind with that tag static, so the specification can pattern match on events of different arity.

### Native ABI

```c
int64_t node_step(const int64_t *in, int64_t count, int64_t *out, int64_t capacity);
```

| Vector | Layout |
|---|---|
| `in` | state leaves in depth-first order, event tag code, event fields, zero padding to `state + 1 + widest event` |
| `out` | new state leaves, effect count `n`, then for each effect its tag code and its fields |

The return value is the number of integers written, or `-1` when the specification has no result (trap), `-2` for a read outside the input, `-3` when `capacity` is too small. `capacity` never needs to exceed `state + 1 + max_effects * (1 + widest effect)`. The function keeps nothing between calls, so any number of node instances share one library. `tool.compile.build` writes `node.ir.json`, `node.c`, the shared library and `node`, a line-oriented executable with the protocol of `specializer/driver.c.in` (one vector per line; a trap prints `trap <code>`).

## 3. Generated schedules (Hegel)

[sim/hegel.py](sim/hegel.py) puts [Hegel](https://hegel.dev) where the seed was. The simulator already takes every nondeterministic decision from outside: the next action, and the integers an action draws through a `Draw`. A Hegel test case answers both, plus one question the seeded schedule never asks (go on or stop here), so Hegel generates the schedule, the faults and the workload, and its shrinker minimizes a failing run. The result is an ordinary `sim-trace/1` record that `sim.replay` reproduces byte for byte with no Hegel involved.

```python
from tool.sim import hegel
found = hegel.falsify(native, workload, profile, inits, invariants=["inv.ok-is-durable"],
                      cases=200, seed=0)
found["failure"]    # None, or the invariant (or "sim.error") that failed
found["trace"]      # None, or the shrunk failing trace
hegel.replay(native, workload, inits, found["trace"], found["trap"])
```

* **Where it comes from.** The official Python frontend, `hegeltest` 0.5.2 on PyPI, which bundles the libhegel 0.53.2 engine. [nix/hegel.nix](../nix/hegel.nix) fetches the published cp314 wheel by hash for each of the flake's systems; nothing is compiled. The core simulator does not import it.
* **Questions.** `pick` asks what `sim.pick` asks, at the same profile rates: a fault, a restart, the workload, otherwise whatever is due first. It adds a small chance that any queued item overtakes the one that is due, as it may in a scripted trace. `HegelDraw` answers `choose` and `delay`. Each step is one Hegel span that starts with the decision to go on, so the shrinker can delete a step whole.
* **Simplest answers.** Every question is put so that false, 0 or the lowest value is the quiet answer: stop, no fault, no crash inside the step, nothing persisted, the shortest delay. Shrinking moves a failure toward a short fault-free schedule in time order.
* **Failures.** A case fails when a named Shen invariant is violated or the run ends in a `SimError` (the failure `sim.error`; `traps=False` leaves those out and counts them in `trapped`). The first failing case fixes the failure that is pursued, and from then on a run fails only by that one. Otherwise Hegel sees one failure where there may be several and its shrinker moves between them: on a specification with two faults the first trace and the shrunk one were different bugs. Invariants see neither times nor draws, so verdicts are cached by step sequence: thousands of shrinking runs cost a few hundred shen-jit calls.
* **Traps.** A `sim-trace/1` record has no step for an action that raised a `SimError`, so a trace that ends `error: ...` holds only the steps before the trap, and `sim.replay` takes its `end` on trust. `falsify` therefore returns the trapping `[action, draws]` beside the trace, and `hegel.replay` applies it after the recorded steps and requires the recorded error.
* **Reproducible.** The result is a function of the arguments, wall time aside. `falsify` gives Hegel every one of its settings (`PINNED`, the seed, the case count, the phases) and refuses to run if a Hegel version has a setting it does not give, because Hegel takes an unset setting from `HEGEL_*` variables, a `hegel.toml` or CI detection. It uses no example database: nothing is saved and no earlier failure is replayed. The step under test may be the shared library or, with `NODE_TARGET=wasm`, the wasm module; on this machine both give the same traces.
* **Coverage.** A run that finds nothing and stops short of `cases`, because Hegel ran out of distinct schedules, returns `exhausted`. `cases` below 1 is an error.

`check accepted_job` ends with this section. 200 generated schedules of the specification hold every invariant. Each of the three mutants is found; the result must be shorter than the first failure, no longer than the length retained in `HEGEL_SHRUNK`, and reached by runs after the first failure; it replays byte for byte on the mutant, is rejected on the unedited binary, still violates the named invariant, and a second run gives the same bytes. Seed 0, 200 cases, aarch64-darwin (the Linux wheels are fetched by hash but the check has not been run on Linux):

| Mutant | Found at case | Hegel shrinks to | Hand shrinker, same failure | Hand shrinker, retained seed |
|---|---|---|---|---|
| `reply-before-ack` | 2 (28 steps) | 5 steps, 1164 runs | 5 | 5 |
| `insert-without-key-check` | 2 (31 steps) | 11 steps, 2153 runs | 10 | 10 |
| `reload-ignores-durable-row` | 4 (27 steps) | 12 steps, 4192 runs | 17 | 14 |

Neither shrinker dominates. `check.shrink` only deletes steps and stops at a 1-minimal trace; Hegel also changes choices, and stops where its passes find nothing smaller. Over seeds 0 to 5 Hegel's result is 5 steps every time for the first mutant, 10 to 17 for the second and 12 to 20 for the third.

A fourth mutant, used here only, deletes the conflict clause of `job.decide`, so a second digest for a stored key has no result. Hegel finds the trap and shrinks the schedule that reaches it from 18 steps to 9. Those 9 steps also replay on the unedited binary, so the check replays the trapping step: it must raise the recorded error on the mutant, and the unedited binary, a rewritten `end` and a missing trapping step must each be rejected.

Seeded faults the section must reject, all of which passed before they were retained:

* Hegel's shrink phase off (`shrink=False`): the first failure comes back unshrunk and the three shrinking conditions say so.
* `HEGEL_PHASES`, `HEGEL_BACKEND`, `HEGEL_STOP_ON_FIRST_FAILURE`, `HEGEL_REPORT_MULTIPLE_FAILURES`, `HEGEL_SEED`, `HEGEL_TEST_CASES`, `HEGEL_DERANDOMIZE`, `HEGEL_DATABASE` and `CI` set against the run: the same bytes in the same number of runs, and no database directory.
* Two faults in one binary. A trap with `reply-before-ack`, asked for `inv.ok-is-durable`: the trap comes first and stays the failure; with `traps=False` the first and the shrunk trace both violate the invariant. `reply-before-ack` with `insert-without-key-check`: the invariant pursued is violated by the first and by the shrunk trace.
* A profile with one schedule and 100 cases asked for returns `exhausted`; zero cases is refused.

Flake checks: `hegel-step` runs `check accepted_job` on the host target and `wasm-step` runs it on the wasm module.
