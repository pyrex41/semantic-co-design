# tool: one Shen specification, compiled, simulated and explored

A developer writes the behavior of a node as ordinary Shen. This package

1. specializes that Shen to a native step function ([compile.py](compile.py), [pe.py](pe.py)),
2. runs the same native code under a deterministic simulator that owns the clock, the network, the disks, crashes and the schedule ([sim/](sim/)),
3. checks every simulated step against the Shen source in shen-jit and evaluates invariants written in Shen over the traces ([check.py](check.py)),
4. translates the same Shen source to Maude and searches the same environment exhaustively within a bound ([maude.py](maude.py)).

```sh
nix develop -c python3 -m tool check kernel         # the compiler's own checks
nix develop -c python3 -m tool check accepted_job   # the first application
nix develop -c python3 -m tool check gossip         # three nodes: send, timers, partitions
nix develop -c python3 -m tool compile accepted_job /tmp/out   # IR, C, library, executable
```

`nix flake check` runs the three checks as `checks.<system>.sim-tool`.

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

`pe.py` is the partial evaluator that was `experiments/branching_vm/pe.py`, generalized. That file is now a short front for it and `experiments/branching_vm/check.py` passes unchanged against it. What it gained:

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

### Compiler limits

* **Values.** Runtime leaves are int64 numbers, declared symbols and booleans. No strings (except as the argument of `simple-error`), floats, vectors, tuples, closures or higher-order calls, no `<-` clauses, and type signatures are skipped, not checked. Supported forms: `define` with literal, variable, wildcard, list and repeated-variable patterns, `where`, `if`, `cases`, `let`, `and`, `or`, `not`, `=`, `+ - *`, `< > <= >=`, `shen.mod`, `simple-error`, and the system functions `append length element? reverse nth hd tl cons cons? empty?` (written in Shen in `pe.py` and specialized like user code).
* **Fixed shapes.** The state returned by a step must have the manifest's shape on every path. A list whose length depends on a runtime number cannot be built. Access to a bounded collection by a runtime index is a chain of tests, linear in the capacity at every access site, so capacities belong in the tens.
* **Recursion.** Recursion bounded by static structure is unfolded. Recursion controlled only by runtime data must be in tail position (it becomes a residual loop); elsewhere it exhausts the unfolding budget and compilation fails. There are no residual calls.
* **Splits multiply.** `k` independent runtime decisions about whether an effect is present give up to `2^k` output paths. A specification that needs many optional effects per step should say so with one conditional that picks among whole effect lists.
* **Widening** covers numbers at fixed argument shapes. A list that grows without bound under runtime control still exhausts the block budget.
* **Arithmetic.** Native arithmetic is unchecked int64. Division is accepted only as `(/ (- A (shen.mod A B)) B)`. `shen.mod` agrees with Shen for non-negative operands only (shen-jit returns `-7` for `(shen.mod -7 3)`). A zero divisor traps natively, while `(shen.mod X 0)` does not terminate in shen-jit, so that case cannot be compared.
* **Traps** are compared with Shen errors as "no result"; error messages are not compared. A symbol used where a number is needed is a compile error, not a residual trap.
* **Evidence is differential.** Nothing here proves that specialization preserves meaning. The kernel check compares 9,600 state/event rows of an exercise node; each application compares the pairs its simulation reaches.

## 3. Simulator

`tool.sim` is seeded, single-threaded, reads no clock and performs no I/O. One `World` holds a virtual clock, the node states (integer vectors, stepped through the ABI of one shared library), a keyed disk and a queue of pending writes per node, queued network events, queued local events, outputs in flight, fault budgets and the workload state.

`World.enabled()` lists every action that may happen next and `World.apply(action, draw)` performs one. All nondeterminism is the choice of action plus the integers drawn inside it.

| Action | Meaning |
|---|---|
| `deliver node event`, `local node event` | The node handles a network or a local event. With crash budget left, a draw decides whether it dies after some proper prefix of its effects. A network event for a down node is lost. |
| `sync node` | The oldest pending write becomes durable and its acknowledgement is queued. |
| `output payload` | An output reaches the outside world. |
| `drop`, `dup` | Lose or duplicate one network event or output (budgeted). |
| `crash node` | Memory and queued local events are lost. A draw picks how many of the oldest pending writes reach the disk anyway; the rest are gone and no acknowledgement is ever delivered. |
| `restart node` | The state becomes `<init>` and the node handles `restart`. |
| `partition mask`, `heal` | Timed schedules only: sends across the cut are discarded. |
| `client ...` | The workload acts, for example by injecting a request. |

Two ways to schedule:

* `sim.run(native, workload, profile, inits, seed)` is the timed, seeded schedule. Delays are drawn for every message and disk operation, the earliest due item runs next, and faults, restarts and client actions are interleaved by seeded coin flips within the profile's budgets. Reordering comes from the delays.
* `sim.explore(native, workload, profile, inits)` visits every reachable state of an untimed profile (no delays, clock fixed at 0, any enabled action next) and returns the set of reachable observations. It raises rather than truncate when `max_states` is exceeded.

`sim.replay(native, workload, inits, trace)` re-executes a recorded trace from its actions and draws alone and raises `Divergence` unless every record reappears. `sim.script` runs an arbitrary list of actions, which is how traces are shrunk.

### Trace format (`sim-trace/1`)

`sim.dumps(trace)` is the canonical encoding (sorted keys, no whitespace); `sim.digest` is its SHA-256.

```json
{"format": "sim-trace/1", "schema": "accepted_job", "seed": 0, "profile": {...}, "end": "quiescent",
 "steps": [{"i": 4, "t": 16, "kind": "deliver", "action": ["deliver", 0, ["submit", 0, 7, 22]],
            "draws": [0, 2], "node": 0, "event": ["submit", 0, 7, 22],
            "pre": [...], "post": [...], "effects": [["write", 0, 0, 7, 22]], "applied": 1,
            "world": {"up": [true], "states": [[...]], "disks": [[]]}}]}
```

Every step has `i`, `t` (virtual time), `kind`, `action`, `draws` and the `world` after it (`states` holds `null` for a down node; a disk is its rows `[key, value...]`). A step in which a node ran also has `pre`, `post`, all `effects` and `applied`, the number that took effect; `crashed`, `persisted` and `lost_writes` appear when the node died. `kind` is one of `boot restart deliver local lost sync output drop dup crash partition heal client`.

### Simulator limits

* One step function per world. Nodes differ only by state.
* The disk is a keyed row store with in-order sync. A row is written whole or not at all. There is no corruption, no full disk and no failed read, and a read never sees a pending write.
* Local events (acknowledgements, read results, timers, clock readings) are never dropped, duplicated or delayed past a crash. A partition discards at send time; messages already in flight still arrive.
* There is no source of randomness for a node. A specification that needs a random number needs a new effect role for it (see extension points).
* The seeded schedule is one random policy. What it covers is whatever the seeds reach; the check reports how many seeds show each required history.
* Trace bytes depend on the pinned Python's `random` module.

## 4. Checkers

`tool.check` holds the application-independent parts.

* **`refinement(schema, traces, inits)`**: every distinct `(state, event)` a node handled in the traces is evaluated by the same Shen file in one shen-jit process and compared with the native result the trace recorded, and `<init>` is compared too.
* **`invariants(schema, traces)`**: invariants are Shen functions in the file the manifest names. A state invariant takes a world `[Up States Disks]`; a trace invariant takes the whole list of steps `[Kind Node Event Effects World]`, where `Effects` are the effects that took effect. Each holds when it returns `true`; anything else is reported. `predicates` evaluates witness predicates the same way.
* **`maude_refinement(modules, schema, traces)`** and **`maude_agreement(...)`**: see below.
* **`shrink(native, workload, inits, trace, still_fails)`**: removes one step at a time while the failure remains, giving a 1-minimal replayable trace.

### What is and is not checked between Shen and Maude

`maude.step_module` translates the Shen source text clause by clause into an equational module: one operator per function, one per clause, with an `owise` equation from each clause to the next so that clause order is kept, `where` as a condition, and `let` as an auxiliary operator. `maude.environment_module` instantiates a fixed Maude model of the untimed environment with the manifest's roles and the example's workload rules; `search` from the initial world to `done(O)` returns the reachable observations.

Checked:

1. The translated step reduces every `(state, event)` pair reached in simulation to the result of the native step, which the refinement check ties to shen-jit.
2. On one small bound per example, Maude's reachable observation set equals the one `sim.explore` computes by running the native step, and no reachable delivery is stuck (a trap).
3. Teeth: for a mutated specification, Maude on the mutant and the simulator on the original must differ.

Not checked:

* The translator is not proved. Agreement is known only on the pairs tested.
* The Maude environment and `tool/sim` are two hand-written models of one design by one author. Their agreement is the observation-set equality at the bound and nothing more: not timed behavior, not partitions, not larger bounds, not liveness.
* The Shen invariants are evaluated on simulator traces. Over the Maude observation set the safety and witness conditions are Python predicates in the example's `check.py`, restated by hand.

## 5. Extension points

A new application is a directory `tool/examples/<name>/` with `node.json`, `spec.shen`, `invariants.shen`, `workload.py` and `check.py`. `python3 -m tool check <name>` imports `tool.examples.<name>.check` and calls `main()`; add the name to the `sim-tool` line in `flake.nix`.

* **Workload** (`tool.sim.Workload`): `initial(profile)` returns plain immutable data kept in `world.client`; `actions(world)` lists what the outside may do; `apply(world, action, draw)` injects requests with `world.request(node, event, draw)` and *replaces* `world.client`. For the bounded search the same workload is given as Maude rules over `{ ... R }` (see `examples/*/workload.py`).
* **Profile** (`sim.profile(...)`): `nodes`, `timed`, `budget` (`crash`, `drop`, `dup`, `partition`), `max_steps`, rates and delay ranges for the seeded schedule, `observe_states` to put node states into observations.
* **A new role** (for example a random number, a second disk, a failed read) is three edits: the role name in `schema.py`, its behavior in `World.effect`, and its equation in `environment_module`. A role whose result is a nondeterministic choice should draw it with `draw.choose`, which seeded runs, replay and exploration already handle; in Maude it needs a rule, not an equation, because effects are applied equationally.
* **Capacities** are declared in the manifest shape and built by `<init>`. The compiler rejects a step whose state does not fit the declared shape and names the path.
* **Checks** are composed in the example's `check.py` from `tool.check`. Keep at least one mutant per checker, as the two examples do.

## 6. What the retained checks establish

| Command | Establishes |
|---|---|
| `python3 -m experiments.branching_vm.check` | The four VM programs still specialize to the same block counts and answers through the generalized evaluator. Its retained gcd residual was regenerated because the modulus is now computed once. |
| `python3 -m tool check kernel` | Native equals shen-jit on 9,600 rows of [kernel/spec.shen](kernel/spec.shen), including traps; a swapped-clause mutant is detected; an undeclared symbol is refused; a stack-only counter compiles with widening and with an annotated division and not without; 40 doublings stay linear and 14 doublings without sharing do not. |
| `python3 -m tool check accepted_job` | See [examples/accepted_job](examples/accepted_job/README.md). |
| `python3 -m tool check gossip` | Three instances of one compiled step with sends, timers, crashes, drops, duplicates and partitions: deterministic, replayable, refining shen-jit; Maude agrees on a two-node bound; two mutants are caught. |

Not attempted: Solo5. The generated step uses only `setjmp` and integer arithmetic, but nothing here builds or runs a unikernel.
