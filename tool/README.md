# tool: one Shen specification, compiled, simulated and explored

A developer writes the behavior of a node as ordinary Shen. This package

1. specializes that Shen to a native step function ([compile.py](compile.py), [pe.py](pe.py)),
2. runs the same native code under a deterministic simulator that owns the clock, the network, the disks, crashes and the schedule ([sim/](sim/)),
3. checks every simulated step against the Shen source in shen-jit and evaluates invariants written in Shen over the traces ([check.py](check.py)),
4. translates the same Shen source to Maude and searches the same environment exhaustively within a bound ([maude.py](maude.py)).

```sh
nix develop -c python3 -m tool check kernel         # the compiler's and the translator's own checks
nix develop -c python3 -m tool check sim            # the simulator's own checks
nix develop -c python3 -m tool check accepted_job   # the first application
nix develop -c python3 -m tool check gossip         # three nodes: send, timers, partitions
nix develop -c python3 -m tool compile accepted_job /tmp/out   # IR, C, library, executable
```

`nix flake check` runs the four checks as `checks.<system>.sim-tool`.

## 1. Specification convention

A node is one pure Shen function and one initial value.

```shen
(define <init>  NodeId NodeCount -> State)
(define <step>  State Event -> [State' Effects])
```

* **State** is a list of fixed shape whose leaves are numbers, symbols or booleans. The shape never changes at run time. A bounded collection is a list with a declared number of slots; the specification keeps its own occupancy marks (a status symbol or a count) inside the slots.
* **Event** is a tagged list `[tag field ...]`. Everything nondeterministic reaches the node only as an event: a client request, a message arrival, the acknowledgement of a disk write, the result of a disk read, a timer, a clock reading, a restart.
* **Effects** is a list of tagged lists `[tag field ...]`, at most `max_effects` long. Every external action leaves the node only as an effect: send, reply, disk write, disk read, set or cancel a timer, read the clock.
* **Durable state** is what the node asked the disk to write and the disk has made durable. An acknowledgement event tells the node a write is durable; before that event the node may not rely on it. Memory is lost at a crash: the simulator replaces the state with `<init>` (computed by `node_init` in the native library) and delivers the `restart` event, and the specification says there what it reads back.
* **What a specification may assume of its environment** is listed under [Environment rules](#environment-rules). Everything else, including which acknowledgement belongs to which write, is the specification's business.
* The specification is ordinary Shen that computes values. It never mentions residual code, the simulator, or integers standing for symbols. `shen-jit` runs the same file unchanged, and that run is the reference for the native code.

The step must be total on every state and event the system can reach. A Shen error (no clause matches, `simple-error`, division by zero, integer overflow) is a *trap*: the native step returns an error code, the simulator records the failing action as a final step of kind `error`, and the run ends `error: ...`.

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
* **Typed leaves and one symbol table.** A runtime leaf is a number, a symbol or a boolean. A symbol is compared by its code from the manifest table; a number never equals a symbol. A symbol that meets runtime data and is not in the table is a compile error. A residual block is named by its function and its static arguments with their types, so a loop entered with `true` and one entered with `1` are two blocks.
* **Joins instead of duplication.** A runtime conditional in value position evaluates both arms and joins them leaf by leaf into temporaries. Where the two arms have different shapes (two effect lists of different length) the join keeps a `Split`, resolved only where something inspects it or at the return, by re-testing the condition.
* **Sharing.** Residual expressions are hash-consed per control-flow scope and counted. A node used more than once is bound to a slot where it was created.
* **Binding times at specialization points.** A specialization point is a tail call under runtime control that re-enters a function already being unfolded. The default division is the old one (an argument holding runtime data has its numbers lifted, a fully static argument stays static). Two controls replace it where it is wrong: `division={"f": ["S", "D", None]}` fixes the binding time of an argument at the entry, and `widen=k` turns a static number into a runtime value once it has taken more than `k` distinct values at one position of one argument shape.
* **Event dispatch.** The entry of a compiled step tests the runtime event tag against each declared event kind and specializes the step once per kind with that tag static, so the specification can pattern match on events of different arity.

### Native ABI

```c
int64_t node_init(int64_t node, int64_t nodes, int64_t *out, int64_t capacity);
int64_t node_step(const int64_t *in, int64_t count, int64_t *out, int64_t capacity);
```

| Vector | Layout |
|---|---|
| `node_init` `out` | the state leaves of `(<init> node nodes)` in depth-first order |
| `node_step` `in` | state leaves in depth-first order, event tag code, event fields, zero padding to `state + 1 + widest event` |
| `node_step` `out` | new state leaves, effect count `n`, then for each effect its tag code and its fields |

Both return the number of integers written, or `-1` when the specification has no result (a trap), `-2` for a read outside the input, `-3` when `capacity` is too small, `-4` when an input integer is outside `[-2^61, 2^61 - 1]`. `capacity` never needs to exceed `state + 1 + max_effects * (1 + widest effect)`. The functions keep nothing between calls, so any number of node instances share one library.

`<init>` is specialized with the node number and the node count as runtime integers, so the initial state, which is also a node's memory after every crash, is in the library and not computed beside it. The simulator calls `node_init`; `check.refinement` compares it with shen-jit for every node.

`tool.compile.build` writes `node.ir.json`, `node.init.ir.json`, `node.c`, the shared library and `node`, an executable: `node init <node> <nodes>` prints the initial state, and with no arguments it speaks the line protocol of `specializer/driver.c.in` (one vector per line; a failure prints `trap <code>`). The library is loaded through a copy named by its contents (`node.<hash>.dylib`), because the loader caches an image by path and would hand back stale code for a library rebuilt in place.

### Arithmetic

Native arithmetic has the meaning shen-jit gives it, traps included:

* Integers are exact in `[-2^61, 2^61 - 1]`. `+ - *` whose result leaves that range are a Shen error ("integer overflow") and a native trap; nothing wraps. An operation that can trap is evaluated where the specification evaluates it, even when its value is never used. The generated C has no signed overflow and no unchecked division: the kernel check runs the edge rows through a `-fsanitize=signed-integer-overflow,integer-divide-by-zero` build.
* `(shen.mod A B)` follows the kernel's definition: `A` when `B > A` (so `(shen.mod -7 3)` is `-7`); otherwise `B` must be positive and the first `B * 2^k` above `A` must be exact, and the result is the remainder; otherwise there is no answer and the native step traps. Where shen-jit does not terminate (`B = 0`, `A >= 0`) the native step traps too; that one case cannot be compared.
* Division is accepted only as `(/ (- A (shen.mod A B)) B)` and follows from the above: `0` when `B > A` and `B` is not zero, the floor quotient when `0 < B <= A`, a trap otherwise.
* A literal outside `(-2^61, 2^61)` or written as anything but a plain integer (`1.5`, `1e2`) is refused at compile time: shen-jit reads those as floats or as exact integers in exponent notation, and the compiler does neither.

### Compiler limits

* **Values.** Runtime leaves are integers in `[-2^61, 2^61 - 1]`, declared symbols and booleans. No strings (except as the argument of `simple-error`), floats, vectors, tuples, closures or higher-order calls, no `<-` clauses, and type signatures are skipped, not checked. Supported forms: `define` with literal, variable, wildcard, list and repeated-variable patterns, `where`, `if`, `cases`, `let`, `and`, `or`, `not`, `=`, `+ - *`, `< > <= >=`, `shen.mod`, `simple-error`, and the system functions `append length element? reverse nth hd tl cons cons? empty?` (written in Shen in `pe.py` and specialized like user code).
* **Fixed shapes.** The state returned by a step must have the manifest's shape on every path. A list whose length depends on a runtime number cannot be built. Access to a bounded collection by a runtime index is a chain of tests, linear in the capacity at every access site, so capacities belong in the tens. `<init>` is compiled with a runtime node number and node count, so it cannot recurse on either outside tail position.
* **Recursion.** Recursion bounded by static structure is unfolded. Recursion controlled only by runtime data must be in tail position (it becomes a residual loop). Anything else is refused with "unfolding too deep": recursion on a runtime value outside tail position, static recursion more than a few thousand calls deep, and a loop that never ends, even on a path no run takes, because a call with no runtime test before it is unfolded. There are no residual calls. A function of more than about 2,000 clauses is refused because the residual conditional nests too deeply to lower.
* **Splits multiply.** `k` independent runtime decisions about whether an effect is present give up to `2^k` output paths. A specification that needs many optional effects per step should say so with one conditional that picks among whole effect lists.
* **Tail-position decisions never join.** A runtime `if` or clause choice in tail position continues separately on each arm. `k` such decisions in a row, each followed by a tail call to a function not already being unfolded, give about `2^k` copies of the rest of the program and as many slots, since slots are not reused across arms (measured: 8 stages 4,605 IR nodes and 256 slots, 12 stages 73,725 and 4,096, 16 stages 1,179,645 and 65,536, against 351 nodes and 33 slots for the same 16 decisions as a `let` chain). The same decisions written as `let` bindings in value position join and stay linear.
* **Slots.** Every arithmetic operation takes a slot of its own, because it must be evaluated where it stands. The slot array is zeroed on every step.
* **Widening** covers numbers at fixed argument shapes. A list that grows without bound under runtime control still exhausts the block budget.
* **Traps** are compared with Shen errors as "no result"; error messages are not compared. A symbol used where a number is needed is a compile error, not a residual trap. A static computation that Shen would answer with an error (a constant expression that overflows, for example) is a trap on that path.
* **Evidence is differential.** Nothing here proves that specialization preserves meaning. The kernel check compares the rows listed in [section 6](#6-what-the-retained-checks-establish) and a fixed set of seeded random programs; each application compares the pairs its simulation and its bounded searches reach. An operator or a form that no retained row uses is untested.

## 3. Simulator

`tool.sim` is seeded, single-threaded, reads no clock and performs no I/O. One `World` holds a virtual clock, the node states (integer vectors, stepped through the ABI of one shared library, initialized by `node_init`), a keyed disk and a queue of pending writes per node, queued network events, queued local events, outputs in flight, fault budgets and the workload state.

`World.enabled()` lists every action that may happen next and `World.apply(action, draw)` performs one. All nondeterminism is the choice of action plus the integers drawn inside it.

| Action | Meaning |
|---|---|
| `deliver node event`, `local node event` | The node handles a network or a local event. With crash budget left, a draw decides whether it dies after some proper prefix of its effects. A network event for a down node is lost. |
| `sync node` | The oldest pending write becomes durable and its acknowledgement is queued. |
| `output payload` | An output reaches the outside world. |
| `drop`, `dup` | Lose or duplicate one network event or output (budgeted). Neither takes virtual time. |
| `crash node` | Memory and queued local events are lost. A draw picks how many of the oldest pending writes reach the disk anyway; the rest are gone and no acknowledgement is ever delivered. |
| `restart node` | The state becomes `<init>` and the node handles `restart`. |
| `partition mask`, `heal` | Timed schedules only: sends across the cut are discarded. |
| `client ...` | The workload acts, for example by injecting a request. |

Two ways to schedule:

* `sim.run(native, workload, profile, seed)` is the timed, seeded schedule. Delays are drawn for every message and disk operation, the earliest due item runs next, and faults, restarts and client actions are interleaved by seeded coin flips within the profile's budgets. Reordering comes from the delays. The run ends `quiescent` (nothing queued, no client or restart move left), `max_steps`, or `error: ...` when the specification failed.
* `sim.explore(native, workload, profile)` visits every reachable state of an untimed profile (no delays, clock fixed at 0, any enabled action next) and returns the set of reachable observations and facts about the search, among them every `(state, event)` the native step was run on. It raises rather than truncate when `max_states` is exceeded.

`sim.script(native, workload, profile, actions)` runs an arbitrary list of actions and draws, which is how traces are shrunk; its trace has no seed and ends `quiescent`, `open` or `error: ...`.

`sim.replay(native, workload, trace)` raises `Divergence` unless the trace is exactly what its contents claim. The steps are re-executed from their recorded actions and draws alone and every record must reappear, a final `error` step included. A scripted trace is then compared whole, with its end recomputed. A seeded trace is also run again from its seed and profile and compared byte for byte, which is what vouches for the seed, the rates, the step bound and the end: a cut-off prefix of a schedule is itself a valid script, so the steps alone cannot.

### Environment rules

These are the properties of the environment that a specification may rely on, and therefore the properties every real effect shim (for example on Solo5) has to provide for a checked guarantee to carry over. They are implemented in `World` as one small method each, and `tool check sim` shows for each that a simulator breaking it is told apart from the real one.

* A disk row is written whole or not at all, syncs happen in the order the writes were issued, and a crash keeps a prefix of the pending writes.
* **No completion crosses a crash.** An acknowledgement, a read result, a timer or a clock reading queued for a node is discarded when the node dies. AcceptedJob's safety depends on this rule and its check demonstrates the dependence.
* A node that dies during a step performed exactly the effects before the point of death, in order, and none after.
* A read sees durable rows only.

### Trace format (`sim-trace/2`)

`sim.dumps(trace)` is the canonical encoding (sorted keys, no whitespace); `sim.digest` is its SHA-256.

```json
{"format": "sim-trace/2", "schema": "accepted_job", "seed": 0, "profile": {...}, "end": "quiescent",
 "steps": [{"i": 4, "t": 16, "kind": "deliver", "action": ["deliver", 0, ["submit", 0, 8, 11]],
            "draws": [0, 2], "node": 0, "event": ["submit", 0, 8, 11],
            "pre": [...], "post": [...], "effects": [["write", 0, 0, 8, 11]], "applied": 1,
            "world": {"up": [true], "states": [[...]], "disks": [[]],
                      "pending": [[[0, 8, 11]]], "outputs": []}}]}
```

Every step has `i`, `t` (virtual time), `kind`, `action`, `draws` and the `world` after it: `states` holds `null` for a down node, a disk is its rows `[key, value...]`, `pending` is the writes not yet synced, per node and oldest first, and `outputs` is the outputs still in flight. A step in which a node ran also has `pre`, `post`, all `effects` and `applied`, the number that took effect. When the node died in the step, `before_crash` is the world as the applied effects left it, and `crashed`, `persisted` and `lost_writes` describe the crash. `kind` is one of `boot restart deliver local lost sync output drop dup crash partition heal client error`; an `error` step is the last one, carries the failing action and the message, and its `world` is the world at the failure.

### Simulator limits

* One step function per world. Nodes differ only by state.
* The disk is a keyed row store with in-order sync. There is no corruption, no torn row, no full disk and no failed read.
* Local events (acknowledgements, read results, timers, clock readings) are never dropped, duplicated or delayed past a crash. There is no profile in which a stale completion arrives, so a specification's own defence against one could not be tested here. A partition discards at send time; messages already in flight still arrive.
* There is no source of randomness for a node. A specification that needs a random number needs a new effect role for it (see extension points).
* The seeded schedule is one random policy. What it covers is whatever the seeds reach; the checks report how many seeds show each required history.
* `check.effects_applied` audits disk writes and outputs against the snapshots. Sends and timers are not in the snapshot and are not audited; the cut itself is the same code path for every role.
* Trace bytes depend on the pinned Python's `random` module.

## 4. Checkers

`tool.check` holds the application-independent parts.

* **`refinement(schema, native, pairs, nodes=1)`**: every `(state, event)` in `pairs` is evaluated by the same Shen file in one shen-jit process and compared with the native result, and `node_init` is compared with `<init>` for every node. `step_pairs(traces)` collects the pairs a simulation reached, and `sim.explore` returns the ones a bounded search executed.
* **`invariants(schema, traces)`**: invariants are Shen functions in the file the manifest names. A state invariant takes a world `[Up States Disks]`; a trace invariant takes the whole list of steps `[Kind Node Event Effects World]`, where `Effects` are the effects that took effect and `World` is the world they left. A step in which the node died is two steps there, the second of kind `crash`, so an effect is never judged against a disk that only the crash produced. Each invariant holds when it returns `true`; anything else is reported. `predicates` evaluates witness predicates the same way.
* **`effects_applied(schema, traces)`**: in every step the disk queue and the outputs in flight grew by exactly the disk writes and outputs recorded as applied, read from the snapshots before any crash. It returns how many effects were visible in each step a node died in.
* **`maude_refinement(modules, schema, pairs)`** and **`maude_agreement(...)`**: see below.
* **`shrink(native, workload, trace, still_fails)`**: removes one step at a time while the failure remains, giving a 1-minimal replayable trace.

### What is and is not checked between Shen and Maude

`maude.step_module` translates the Shen source text clause by clause into an equational module: one operator per function, one per clause, with an `owise` equation from each clause to the next for a pattern that does not match, a three-way guard operator for `where` (true takes the clause, false goes on, an error stays an error), and `let` as an auxiliary operator. A Shen error is a term with no normal form of sort `Val`, and every operator is strict in it. Arithmetic has shen-jit's range and `shen.mod` the kernel's definition. `maude.environment_module` instantiates a fixed Maude model of the untimed environment with the manifest's roles and the example's workload rules; `search` from the initial world to `done(O)` returns the reachable observations.

Checked:

1. The translator itself, in `tool check kernel`: 762 calls of [kernel/translate.shen](kernel/translate.shen) (wildcards beside a variable named `W1`, repeated variables, guards that are false or errors, strict `=`, unused bindings, every operator to the edge of the integer range) reduce to what shen-jit returns.
2. The translated step reduces every `(state, event)` pair reached in simulation and in the bounded searches to the result of the native step, which the refinement check ties to shen-jit.
3. On a few small bounds per example, Maude's reachable observation set equals the one `sim.explore` computes by running the native step, and no reachable delivery is stuck (a trap). The bounds include crash, drop and duplicate budgets; `tool check sim` adds a node with several effects per step and several writes pending.
4. Teeth: for a mutated specification, Maude on the mutant and the simulator on the original must differ, and the translation of a mutant must not pass the step check against the unedited native code. For the environment: five broken simulators and three edited Maude models must each differ from the other side.

Not checked:

* The translator is not proved. Agreement is known only on the calls and pairs tested.
* The Maude environment and `tool/sim` are two hand-written models of one design by one author. Their agreement is the observation-set equality at the bounds and nothing more: not timed behavior, not partitions, not larger bounds, not liveness.
* Observations are sets: how many times an output was delivered is not observed, in either model.
* The Shen invariants are evaluated on simulator traces. Over the Maude observation set the safety and witness conditions are Python predicates in the example's `check.py`, restated by hand.
* An inexact `(/ A B)` is a float in Shen and stuck in Maude; the compiler refuses such a program, so the difference cannot reach a compared pair.

## 5. Extension points

A new application is a directory `tool/examples/<name>/` with `node.json`, `spec.shen`, `invariants.shen`, `workload.py` and `check.py`. `python3 -m tool check <name>` imports `tool.examples.<name>.check` and calls `main()`; add the name to the `sim-tool` line in `flake.nix`.

* **Workload** (`tool.sim.Workload`): `initial(profile)` returns plain immutable data kept in `world.client`; `actions(world)` lists what the outside may do; `apply(world, action, draw)` injects requests with `world.request(node, event, draw)` and *replaces* `world.client`. For the bounded search the same workload is given as Maude rules over `{ ... R }` (see `examples/*/workload.py`). Give the workload enough variety to distinguish the specification's cases: with one key, a key comparison is unconstrained.
* **Profile** (`sim.profile(...)`): `nodes`, `timed`, `budget` (`crash`, `drop`, `dup`, `partition`), `max_steps`, rates and delay ranges for the seeded schedule, `observe_states` to put node states into observations.
* **A new role** (for example a random number, a second disk, a failed read) is three edits: the role name in `schema.py`, its behavior in `World.effect`, and its equation in `environment_module`. A role whose result is a nondeterministic choice should draw it with `draw.choose`, which seeded runs, replay and exploration already handle; in Maude it needs a rule, not an equation, because effects are applied equationally.
* **A new environment rule** is a method of `World` with a subclass in `tool/sim/mutants.py` that breaks it and a line in `tool/sim_check.py` that tells the two apart. `run`, `script`, `replay`, `explore` and `check.maude_agreement` take `world=`; `maude_agreement` also takes `edit=` to rewrite the Maude text.
* **Capacities** are declared in the manifest shape and built by `<init>`. The compiler rejects a step whose state does not fit the declared shape and names the path.
* **Checks** are composed in the example's `check.py` from `tool.check`. Keep at least one mutant per invariant, per witness and per checker; `examples/accepted_job/check.py` fails if a declared invariant has none.

## 6. What the retained checks establish

| Command | Establishes |
|---|---|
| `python3 -m experiments.branching_vm.check` | The four VM programs still specialize to the same block counts and answers through the generalized evaluator. Its retained gcd residual was regenerated because the modulus is now computed once. |
| `python3 -m tool check kernel` | Native equals shen-jit on 9,600 rows of [kernel/spec.shen](kernel/spec.shen), including traps, and `node_init` and the executable give the same initial state; on 1,780 rows of every comparison and arithmetic operator at small, negative and extreme operands (455 of them traps), also through a sanitizer build of the generated C; a step whose recursion is controlled by runtime data becomes a residual loop and agrees on 3,200 rows; a loop entered with `true`, `1`, `false` and `0` is four blocks; 7 sources outside the fragment are refused with a reason; 119 seeded random programs agree on 7,140 rows; the Maude translation agrees with shen-jit on 762 calls. Mutants: swapped clauses, an undeclared symbol, a compiler with `<=` and `>=` swapped (caught by the edge rows and by the random programs), a compiler whose block names ignore types (caught by the loop and by the random programs), a translation with one operator changed. Binding times and sharing as before. |
| `python3 -m tool check sim` | On a probe node with four effects in one step: 200 seeds deterministic and replayable; each step changed the disk queue and the outputs in flight by exactly its applied effects, with nodes dying at every cut; drop and dup take no time; replay rejects 10 edits of a trace, a cut script and three abuses of an error trace; Maude and the simulator reach the same observations with a crash, a drop and a duplicate and up to four writes pending; five broken simulators and three edited Maude models are each told apart. |
| `python3 -m tool check accepted_job` | See [examples/accepted_job](examples/accepted_job/README.md). |
| `python3 -m tool check gossip` | Three instances of one compiled step with sends, timers, crashes, drops, duplicates and partitions: deterministic, every trace replays, refining shen-jit on simulated and searched pairs; Maude agrees on two-node bounds with a drop, with a crash and with a duplicate; two mutants are caught. |

A one-off run, not retained: 895 further random programs (seeds 120 to 1019, 53,700 rows) also agreed with shen-jit.

Not attempted: Solo5. The generated code uses only `sigsetjmp` (without the signal mask), integer arithmetic and the compiler's overflow builtins, but nothing here builds or runs a unikernel, and no effect shim exists whose compliance with the environment rules could be checked.
