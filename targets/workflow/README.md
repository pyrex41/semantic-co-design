# A durable workflow engine, specialized per workflow definition

[engine.shen](engine.shen) is an interpreter, written in ordinary Shen, of a small workflow-definition language. A definition is a Shen list. The kernel compiler in [tool/](../../tool/README.md) specializes the engine to one definition at a time and produces a native step function; the same binary then runs under the kernel's deterministic simulator, against invariants written in Shen, and the same Shen text is searched exhaustively in Maude within bounds.

```sh
nix develop -c python3 -m tool check workflow       # everything below except the timing, 4 to 5 minutes
nix develop -c python3 -m tool record workflow      # the same, regenerating mutants.json and mutants/
nix develop -c python3 -m targets.workflow.bench    # step time against shen-jit, about 15 seconds
```

## The language and the engine

```shen
(define order.definition ->
  [2 25                           \\ undo: attempts, timeout
   [[act reserve 101 2 4 60]      \\ activity, idempotency key, attempts, backoff, timeout
    [act charge 102 3 4 30]
    [branch 1 4]                  \\ if the last result is 1, go on at position 4
    [act review 103 1 4 30]
    [act ship 104 2 4 30]
    [end]]])
```

Instructions: `act` runs an activity under an idempotency key, `each` runs one activity once per key of a list, `sleep` waits for a timer, `branch` jumps forward on the last activity result, `end` completes. An attempt ends with a result (0 is a failure) or its timeout; the next attempt starts `Backoff * Attempt` later. When the attempts are used up, or an external `cancel` is accepted, every activity that was ever started is undone, newest first.

Four definitions are compiled, all by the same engine text and the same compiler: [order](defs/order.shen) (a saga with a branch), [docs](defs/docs.shen) (`each` over three documents, a `sleep`, a final activity), and two small ones for exhaustive search, [small](defs/small.shen) and [single](defs/single.shen). A definition file holds the constant and two one-line entry points that apply the engine to it. The manifest of a definition is generated ([model.py](model.py)); it differs between definitions only in the activity names and the number of keys, both read from the definition by shen-jit.

Durability is event-sourced. The engine decides nothing in memory alone: an event that changes the position becomes a history row `[Kind A B]`, the row is written with a disk-write effect, and only the acknowledgement applies it (`wf.apply`) and performs what the new position asks for (`wf.plan`): send a request, arm a timer, write the next row. After a `restart` the engine reads the rows back one at a time, applies each with the same `wf.apply`, and resumes with the same `wf.plan`. There is no code outside the specification that orders, retries, deduplicates or recovers anything; the simulator's effects are the kernel's dumb ones (disk write and read, output, timer).

The **activity workers are part of the simulated environment** ([workload.py](workload.py), and the same rules in Maude). They see every delivery of a request, duplicates included, and keep a ledger of what they really did: `commit`, `fault` (ran and failed), `reverse` (undid a committed effect) and `void` (an undo for a key that never committed). The invariants read that ledger; the engine never does.

What the engine relies on, and what the check shows about it:

* **From the workers:** a key commits at most once, and a key that was undone never commits afterwards. With workers that ignore the key, the unedited engine violates `one-effect-per-key` on 39 of 100 seeds.
* **From the environment:** a row is written whole, in order; setting a timer replaces the pending timer of the same id. It does *not* rely on the kernel's rule that no completion crosses a crash: it checks the number of an acknowledgement and of a read result itself. In a simulator that delivers acknowledgements, read results and timers across a crash (`tool.sim.mutants.KeepsCompletions`) the engine still satisfies every state invariant in an exhaustive one-crash search (145,530 states). An engine without the acknowledgement check is indistinguishable in the standard simulator (83,135 states, no violation) and violates `history-is-true` and `memory-is-replay` in that one.

## Invariants

All are Shen, in [invariants.shen](invariants.shen). A *situation* is what the world shows at one moment: the node's memory (or `down`), the durable rows, the pending writes, and the workers' ledger.

| Asked for | State invariant (every step, every searched state) | Also, over whole runs |
|---|---|---|
| at most one committed effect per idempotency key | `one-effect-per-key`: every commit in the ledger carries a declared key and no key commits twice. `history-is-true`: a worker only ran an activity whose intent row was already durable; a row that says an activity finished or was undone matches something a worker did; a row that ends an attempt is about the attempt in flight | |
| history is append-only; replay after restart reproduces the pre-crash state | `history-is-a-log`: rows are numbered from 0 without a gap and a pending write only continues them. `memory-is-replay`: a node's position equals `wf.apply` folded over the first `Seq` durable rows, and a live node has seen every durable row except at most the one in flight | `inv.append-only`: a durable row never changes or disappears. `inv.recovery-reproduces`: a node that comes back live has the position a live node last had at the same history length |
| a completed or compensated workflow never regresses | `terminal-is-final`: nothing is written after the row that completes, compensates or gives up | `inv.never-regresses`: once a live node shows a finished workflow, every later live state shows the same |
| compensation runs exactly for the steps that completed | `compensation-matches`: effects are reversed newest first with none skipped; a compensated workflow has reversed exactly the committed effects; a completed one never undid anything | |
| once faults stop, every workflow reaches a terminal state within a declared step bound | in the searches: the graph has no cycle, and a world where nothing but a fault can still happen is at rest (`inv.resting?`) | `inv.settles`: the node is at rest within `<name>.settles-within` simulator steps of the last crash, loss or duplication |

`memory-is-replay` uses the engine's own `wf.apply` as its reference: it checks the protocol (which rows are applied when, on the live path and on the recovery path), not what a row means. `inv.recovery-reproduces` compares recorded states only and needs no reference, but says nothing when the row in flight at the crash turned out durable, because no earlier live state had that history.

## What the check establishes

`python3 -m tool check workflow` prints one line per item; the numbers below are from that output.

* **Specialization.** One engine file and one compiler give four different residual programs (order: 5,629 IR nodes, 398 slots; docs: 5,741 and 350; no residual loops). Mechanically:
  * The words of the language (`act each sleep branch end`) are not in any manifest, so they have no integer code, and the compiler refuses a program that would compare, store or emit one at run time.
  * When those words *are* given codes, a specializer subclass that records every symbol turned into an integer shows none of them is used, and the residual program is byte-identical.
  * What is left of a definition's structure is its positions: the residual program compares the runtime `Pc` with exactly the indices of the program, and contains the definition's own keys as constants and no other definition's.
  * Teeth: [leak/](leak/spec.shen), a three-instruction interpreter that fetches an instruction as a value and then asks what it is, stays free of its opcodes while they are undeclared, residualizes both once they are declared, and is refused when it stores one.
* **Simulation** of order and docs, 400 seeds each with up to two crashes, two losses, two duplications and one cancel (20,672 and 26,670 steps). All runs end with nothing left to happen; a seed reruns to identical bytes; every trace replays. Native equals shen-jit running the unspecialized engine on all 1,489 and 1,707 distinct `(state, event)` pairs. Every state invariant holds in every step and every run invariant on every run. Required histories are counted and must each occur: completions and requests lost and duplicated in flight; attempts timing out; late timers ignored (1,218 and 1,559); a timer firing before one armed earlier; completions ignored as late or duplicate; cancel accepted while an activity is in flight with its completion arriving afterwards (59 and 56); cancel arriving while the completion is being written; cancel after the workflow finished; runs ending completed and compensated. Runs ending stuck are counted but not required (35 and 1). Order's workers received 1,585 run requests, really executed 1,394 times and committed 1,097 effects.
* **Bounded search**, five bounds, each run in Maude and in the simulator on the native code: small with a cancel, with a lost message, with a duplicated message, with a crash; single with two crashes (12,232 to 170,940 simulator states). An untimed search lets a timer fire at any moment, so timeouts, late timers and every order of timers and completions are in each bound. In each:
  * Maude and the simulator reach the same observations (node state, durable rows, delivered and dropped messages, the workers' ledger), and no reachable step traps.
  * The state invariants, **the same Shen text translated by the kernel's translator**, are searched in Maude for a reachable state that violates one: none. shen-jit evaluates them on every distinct situation of the simulator's graph: none.
  * `inv.append-only` and `inv.never-regresses` hold on every edge of the simulator's graph.
  * The graph has no cycle, so every schedule ends; its longest schedule is 40 to 55 steps (declared 60); every situation where only a fault could still happen is at rest. Maude is asked the same question independently.
  * Where crashes are in the budget, the node died at every effect boundary of every step shape (9 shapes in `crash`; all 15, recovery included, in `two crashes`).
* **Mutants.** Nine one-edit engines ([check.py](check.py) `MUTANTS`). Every invariant above is the target of at least one; the check fails otherwise.

| Mutant | Violates | Seed | Minimal trace |
|---|---|---|---|
| schedules the activity in the step that writes the intent | `history-is-true` | 2 | 3 steps |
| sends a retry without the idempotency key | `one-effect-per-key` | 0 | 12 |
| undoes the oldest started activity first | `compensation-matches` | 1 | 39 |
| takes any completion for the one it is waiting for | `history-is-true` | 5 | 28 |
| undoes only activities known complete, not the one in flight | `compensation-matches` | 1 | 39 |
| accepts a cancel after the workflow finished | `terminal-is-final`, `inv.never-regresses` | 2 | 24 |
| reads the history back but does not apply it | `memory-is-replay`, `inv.recovery-reproduces` | 0 | 15 |
| numbers its rows from 0 again after a restart | `history-is-a-log`, `inv.append-only` | 0 | 20 |
| comes back from a restart without resuming | `inv.settles` | 1 | 11 |

  For each, [mutants.json](mutants.json) retains the seed and the SHA-256 of its trace on the order definition, and [mutants/](mutants/) a 1-minimal trace. The check rebuilds the mutant, reproduces the seed's trace byte for byte, confirms the unedited engine passes the same seed, replays the minimal trace, confirms it still fails and that removing any one step loses the failure, confirms the refinement check rejects the mutant binary against the unedited source and the Maude step check rejects its translation, and then searches the same mutant on the small definition in Maude: a violating state is reachable, and shen-jit confirms the verdict on that state.

## Step time, and whether specialization bought anything

`python3 -m targets.workflow.bench` times the steps of the 400 seeded runs above (same trace digests), in order, in three lanes: `node_step` on the recorded input vectors, and the unchanged `engine.shen` applied to the same states and events by `shen-jit eval` and by a sealed `shen-jit build` executable. Process CPU time, 11 trials of 50 passes per lane after one warm-up pass; all lanes must emit the same number of effects. Retained in [bench.json](bench.json): Apple arm64, macOS 27.2.1.

| ns per step | order (12,550 steps) | docs (16,586 steps) |
|---|---|---|
| native, specialized | median 23.8, quartiles 23.7 to 23.9, range 23.6 to 23.9 | 23.3, 23.0 to 23.4, 23.0 to 26.7 |
| shen-jit eval, interpreting | 42.2, 42.0 to 42.4, 37.6 to 51.3 | 40.6, 40.4 to 43.7, 40.3 to 49.7 |
| shen-jit build, interpreting | 49.5, 49.3 to 49.7, 49.3 to 51.1 | 48.4, 48.3 to 48.6, 48.2 to 53.6 |

* The specialized native step is about 1.8 times faster than shen-jit running the engine as an interpreter. shen-jit is a JIT compiler, and on six-instruction definitions the engine's interpretive overhead is small. A second run, while other work was loading the machine, was noisier and gave medians of 25.1 (native) against 51.2 and 67.9 for order and 25.2 against 45.1 and 54.9 for docs, with ranges up to 37 and 95: ratios between 1.8 and 2.7. The retained receipt is the quieter run.
* **As first measured it was six times slower:** 241 ns per step against 42. About 215 ns of that was `setjmp` saving the signal mask on every step, in the kernel's ABI template and unrelated to the workflow. That is fixed in a kernel commit (`sigsetjmp` without the mask); the table is after the fix. The earlier figure and two diagnostic builds (one with `_setjmp`, 23.5 ns; one that leaves the slot array uncleared, no faster) were one-off runs of the same benchmark and are not retained.
* **What cannot be said:** how much of that factor is specialization. There is no unspecialized native engine to compare with: the check tries to compile the engine with its definition as runtime data (leaves in the state, list structure static) and the compiler refuses it, because the shape of the engine's own state, one mark per key, depends on the definition. The lanes also differ in code generator and data representation (integer vectors against allocated lists). What specialization did buy is that the engine can be compiled by this compiler at all, to a step function with no Shen runtime and no trace of the definition language.
* One machine, CPU time of whole passes; not a latency distribution and not an end-to-end throughput.

## What is not established

* **No parallelism.** One activity is in flight at a time. `each` handles its keys in order; it is not a parallel fan-out. One workflow per node, one node per world.
* **A timeout counts as a failed attempt.** A workflow whose activity succeeded can still be compensated because the completion was slow, and an undo that times out `UndoAttempts` times ends the workflow `stuck`, with committed effects not reversed. `compensation-matches` says nothing about `stuck`.
* **A cancel can be lost.** One that arrives while a write is in flight or while the history is being read back is ignored, not remembered.
* **Writing the intent before scheduling was not shown to matter end to end.** The mutant that schedules first violates only `history-is-true` (a worker ran an activity whose intent was not durable). With workers that deduplicate and an engine whose decisions are a function of the durable history, no other invariant was seen to fail.
* **The stale-acknowledgement defence is invisible to the standard checks.** The mutant without it is caught only under `KeepsCompletions`, and that world is explored by the simulator alone, not by Maude.
* **Invariants in Maude are the state invariants.** The run invariants are evaluated on simulated traces and, for two of them, on the edges of the simulator's graph. Liveness is exhaustive only inside the five bounds; for order and docs it is the declared bounds (100 and 120 steps) holding on 400 seeds each, with observed worst cases 68 and 85. The bounds are declared constants, not derived from the definitions.
* **Crashes at every effect boundary** are exhaustive only in the two bounds with crashes. The seeded runs hit 16 of the 20 boundaries seen per definition; the ones not hit are in steps taken during recovery, and the check prints them.
* **No functional specification of a workflow.** Nothing says that a completed order did every step on its path. An engine that skipped an instruction would be caught only if it also broke one of the invariants.
* **The workers exist twice**, in Python and as Maude rules, both written here by hand. Their agreement is the equality of observation sets at the five bounds.
* **Specialization is not complete unfolding.** The engine looks up "the activity at Pc" by scanning the program with a static index, so each arm sees its instruction statically, but the operands of same-kind instructions are then joined into temporaries. The residual program has no test on definition syntax; it does carry a decision chain on `Pc` that assigns those operands. The scan is a binding-time improvement, in ordinary Shen that shen-jit runs unchanged. Branch targets must be forward.
* **Evidence is differential and bounded**, as for every kernel example: native against shen-jit on the pairs reached, Maude against the simulator on observation sets. "Minimal" traces are 1-minimal under step removal, up to 39 steps.
* `nix flake check` does not run this check: `flake.nix` was left alone because other targets are being added to the same line in parallel.
* Not attempted: Solo5, more than one node, real time.

## Kernel changes this target needed

* `Workload.deliver(world, payload, draw)` in the simulator and `workload["output"]` in the Maude environment: a workload can react to each delivered output, which is what makes the activity workers part of the environment. Defaults are the old behavior.
* `python3 -m tool check <name>` falls back to `targets/<name>/check.py`.
* The native trap is armed with `sigsetjmp(env, 0)` instead of `setjmp`.

The compiler itself (`tool/pe.py`, `tool/compile.py`) is unchanged.
