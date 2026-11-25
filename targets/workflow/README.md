# A durable workflow engine, specialized per workflow definition

[engine.shen](engine.shen) is an interpreter, written in ordinary Shen, of a small workflow-definition language. A definition is a Shen list. The kernel compiler in [tool/](../../tool/README.md) specializes the engine to one definition at a time and produces a native step function; the same binary then runs under the kernel's deterministic simulator, against invariants written in Shen, and the same Shen text is searched exhaustively in Maude within bounds.

```sh
nix develop -c python3 -m tool check workflow       # everything below except the timing, 5 to 6 minutes
nix develop -c python3 -m tool record workflow      # the same, regenerating mutants.json and mutants/
nix develop -c python3 -m targets.workflow.check simulation mutants   # only some sections
nix develop -c python3 -m targets.workflow.bench    # step time against shen-jit, about 15 seconds
```

The sections are `specialization`, `simulation`, `bounded`, `reliance` and `mutants`. Here they took about 3, 14, 218, 89 and 22 seconds.

## The language and the engine

```shen
(define order.definition ->
  [2 45                           \\ undo: attempts, timeout
   [[act reserve 101 2 4 60]      \\ activity, idempotency key, attempts, backoff, timeout
    [act charge 102 3 4 30]
    [branch 1 4]                  \\ if the last result is 1, go on at position 4
    [act review 103 1 4 30]
    [act ship 104 2 4 30]
    [end]]])
```

Instructions: `act` runs an activity under an idempotency key, `each` runs one activity once per key of a list, `sleep` waits for a timer, `branch` jumps forward on the last activity result, `end` completes. An attempt ends with a result (0 is a failure) or its timeout; the next attempt starts `Backoff * Attempt` later. When the attempts are used up, or an external `cancel` is accepted, every activity that was ever started is undone, newest first. Each undo is tried up to `UndoAttempts` times; if none of them is acknowledged in time the workflow ends `stuck`, with compensation abandoned. When the workflow ends, the engine sends the client one `report` with the outcome: `completed`, `compensated` or `stuck`.

**A definition is validated before it is compiled.** [definition.shen](definition.shen) is ordinary Shen, evaluated by shen-jit when a definition is loaded ([model.py](model.py)): keys are positive and belong to one activity instance each, branches go forward and stay inside the program, the last instruction is `end`, and counts and times are positive. Four fixtures under [defs/rejected/](defs/rejected/) are each refused for one reason. Two of them used to compile: one never executed its second activity, because the workers answered it from the first one's record.

Five definitions are compiled, all by the same engine text and the same compiler: [order](defs/order.shen) (a saga with a branch), [docs](defs/docs.shen) (`each` over three documents, a `sleep`, a final activity), and three small ones for exhaustive search, [small](defs/small.shen), [single](defs/single.shen) and [mix](defs/mix.shen) (an `each`, a branch and a sleep). A definition file holds the constant and two one-line entry points that apply the engine to it. The manifest of a definition is generated; it differs between definitions only in the activity names and the number of keys, both read from the definition by shen-jit.

Durability is event-sourced. The engine decides nothing in memory alone: an event that changes the position becomes a history row `[Kind A B]`, the row is written with a disk-write effect, and only the acknowledgement applies it (`wf.apply`) and performs what the new position asks for (`wf.plan`): send a request, arm a timer, write the next row. After a `restart` the engine reads the rows back one at a time, applies each with the same `wf.apply`, and resumes with the same `wf.plan`. There is no code outside the specification that orders, retries, deduplicates or recovers anything; the simulator's effects are the kernel's dumb ones (disk write and read, output, timer).

**Timers carry the history length.** A timer is armed with `Seq`, the length of the history at the position it belongs to, as its id, and an expiry counts only while the node is still at that `Seq`. The engine assumes nothing about a timer source replacing or cancelling timers, and the simulator and Maude deliver old expiries at every later moment (1,479 and 1,843 are ignored in the seeded runs). A timed-out attempt is recorded as a `late` row and a reported failure as a `fail` row.

The **activity workers and the client are part of the simulated environment** ([workload.py](workload.py), and the same rules in Maude). The workers see every delivery of a request, duplicates included, and keep a ledger of what they really did: `commit`, `fault` (ran and failed), `reverse` (undid a committed effect) and `void` (an undo for a key that never committed). The client keeps the outcome of every report that reaches it. The invariants read both; the engine never does.

What the engine relies on, and what the check shows about it:

* **From the workers, the idempotency contract in two halves.** A declared key commits at most once: with workers that ignore the key, the unedited engine violates `one-effect-per-activity` on 41 of 100 seeds. A key that was undone never commits afterwards: with workers that deduplicate commits but forget an undo that found nothing committed, the unedited engine violates `compensation-matches` in 109 of the 986 situations of an exhaustive search of small that has no fault and no cancel in it (a timeout is enough). The standard workers give both halves by construction, so no engine can make a declared key commit twice against them. The engine's own contribution is a declared key that stays the same across retries and recovery.
* **From the environment:** a row is written whole, in order. It does *not* rely on the kernel's rule that no completion crosses a crash: it checks the number of an acknowledgement and of a read result itself. In a simulator that delivers acknowledgements, read results and timers across a crash (`tool.sim.mutants.KeepsCompletions`) the engine still satisfies every state invariant in an exhaustive one-crash search (223,570 states). An engine without the acknowledgement check is indistinguishable in the standard simulator (102,535 states, no violation) and violates `history-is-true`, `memory-is-replay` and `reports-are-true` in that one.

## Invariants

All are Shen, in [invariants.shen](invariants.shen). A *situation* is what the world shows at one moment: the node's memory (or `down`), the durable rows, the pending writes, the workers' ledger, and what the client was told.

**The invariants do not call the engine.** [invariants.shen](invariants.shen) reads the definition a second time on its own and says what a history means: `inv.meaning` gives the position a list of rows leads to, or `illegal` when a row could not have been written where it stands. An engine that misreads a definition or gives a row another meaning disagrees with it. The earlier version compared memory with the engine's own `wf.apply`, so an engine with a wrong `wf.apply` validated itself.

| Asked for | State invariants (every step, every searched state) | Also, over whole runs |
|---|---|---|
| at most one committed effect per idempotency key | `one-effect-per-activity`: counted over what the workers did, whatever key a request carried: no key commits twice, and no activity has more commits than the definition has instances of it. `history-is-true`: a worker only ran an activity whose intent row was already durable and only undid a key whose undo row was durable; an `ok` row is a commit with that result; a `fail` row is a failure of that attempt; an `undone` row is an undo a worker did | |
| history is append-only; replay after restart reproduces the pre-crash state | `history-is-a-log`: rows are numbered from 0 without a gap and a pending write only continues them. `history-follows-definition`: every row, durable or pending, is one the definition allows where it stands. `memory-is-replay`: a node's position equals `inv.meaning` of its first `Seq` durable rows, and a live node has seen every durable row except at most the one in flight | `inv.append-only`: a durable row never changes or disappears. `inv.recovery-reproduces`: a node that comes back live has the position a live node last had at the same history length |
| a completed or compensated workflow never regresses | `terminal-is-final`: nothing is written after the row that completes, compensates or gives up | `inv.never-regresses`: once a live node shows a finished workflow, every later live state shows the same, across any number of crashes |
| compensation runs exactly for the steps that completed | `compensation-matches`: effects are reversed newest first with none skipped; a compensated workflow has reversed exactly the committed effects; a completed one never undid anything. `completed-is-whole`: walking the definition from the start, taking at each branch the result the workers really committed, meets exactly the committed keys, in order, and reaches `end` | `inv.stuck-needs-a-fault`: in a run with no crash, loss or duplication, no workflow ends `stuck` |
| the client is told the truth | `reports-are-true`: every report the client received names a durable terminal row | `inv.client-is-told`: a run that ended without a fault left the client told the outcome the node shows, and nothing else |
| retries follow the definition | (`history-follows-definition`: at most `Attempts` attempts, at most `UndoAttempts` tries of an undo) | `inv.backs-off`: attempt `A + 1` is not written earlier than `Backoff * A` after the row that ended attempt `A` became durable |
| once faults stop, every workflow reaches a terminal state within a declared step bound | in the searches: the graph has no cycle, and a world where nothing but a fault can still happen is at rest (`inv.resting?`) and, where no report can have been lost, has told the client (`inv.told-at-rest?`) | `inv.settles`: the node is at rest within `<name>.settles-within` simulator steps of the last crash, loss or duplication |

What `history-follows-definition` allows is one clause of `inv.row` per thing a workflow may do: the activities of the path taken, in order; after an accepted cancel or a last failed attempt nothing but compensation, newest first; `completed` only at `end`; `compensated` only when everything started was undone; `stuck` only after `UndoAttempts` tries of one undo.

**Compensation may be abandoned, and this is when.** `stuck` is a legal outcome: `inv.settles` is satisfied by it. It is allowed only after the last permitted try of an undo (`history-follows-definition`) and never in a fault-free run (`inv.stuck-needs-a-fault`). With the undo timeouts of 25 and 30 the definitions first had, shorter than a round trip to a worker (up to 40), the unedited engine ended 3 of 400 order seeds `stuck` with no fault at all. They are 45 now.

## What the check establishes

`python3 -m tool check workflow` prints one line per item; the numbers below are from that output.

* **Specialization.** One engine file and one compiler give five different residual programs (order: 5,856 IR nodes, 434 slots; docs: 5,958 and 378; no residual loops). Mechanically:
  * The words of the language (`act each sleep branch end`) are not in any manifest, so they have no integer code, and the compiler refuses a program that would compare, store or emit one at run time.
  * When those words *are* given codes, a specializer subclass that records every symbol turned into an integer shows none of them is used, and the residual program is byte-identical.
  * What is left of a definition's structure is its positions: the residual program compares the runtime `Pc` with exactly the indices of the program, and contains the definition's own keys as constants and no other definition's.
  * Teeth: [leak/](leak/spec.shen), a three-instruction interpreter that fetches an instruction as a value and then asks what it is, stays free of its opcodes while they are undeclared, residualizes both once they are declared, and is refused when it stores one.
* **Simulation** of order and docs, 400 seeds each with up to two crashes, two losses, two duplications and one cancel (20,832 and 26,474 steps). All runs end with nothing left to happen; a seed reruns to identical bytes; every trace replays. Native equals shen-jit running the unspecialized engine, and the Maude translation of the engine, on all 1,279 and 1,539 distinct `(state, event)` pairs. Every state invariant holds in every step and every run invariant on every run. Required histories are counted and must each occur: completions and requests lost and duplicated in flight; attempts timing out; late timers ignored; a timer firing before one armed earlier; completions ignored as late or duplicate; cancel accepted while an activity is in flight with its completion arriving afterwards (55 and 58); cancel arriving while the completion is being written; cancel after the workflow finished; runs ending completed and compensated. Order's workers received 1,577 run requests, really executed 1,378 times and committed 1,083 effects.
  * 62 and 35 of those runs had no fault. A further 200 seeds per definition have nothing in the fault budget: every invariant holds, 93 and 87 of them end compensated (after a cancel, a failure or a timeout), none stuck.
* **Bounded search**, six bounds, each run in Maude and in the simulator on the native code: small with a cancel, with a lost message, with a duplicated message, with a crash; single with two crashes; mix with a cancel, a failing activity and a branch taken both ways (22,312 to 286,147 simulator states). An untimed search lets a timer fire at any moment, so timeouts, old timers and every order of timers and completions are in each bound. In each:
  * Maude and the simulator reach the same observations (node state, durable rows, delivered and dropped messages, the workers' ledger, what the client was told), and no reachable step traps.
  * The state invariants, **the same Shen text translated by the kernel's translator**, are searched in Maude for a reachable state that violates one: none. shen-jit evaluates them on every distinct situation of the simulator's graph: none. Every bound shows finished workflows of all three kinds (in `cancel`: 8 situations completed, 124 compensated, 360 stuck).
  * `inv.append-only` holds on every edge of the simulator's graph, which is every path, because being a prefix is transitive. `inv.never-regresses` is judged **on every path**: for each state, the finished nodes some path to it has shown are collected, and the invariant is evaluated on every pair of an earlier finished node and a later node (97 pairs in `crash`, 29 in `two crashes`, none where there is no crash, because a finished node without a crash never changes).
  * The graph has no cycle, so every schedule ends; its longest schedule is 40 to 55 steps (declared 60 and 80); every situation where only a fault could still happen is at rest and, in the five bounds without a lost message, has told the client the outcome. Maude is asked the same question independently.
  * Where crashes are in the budget, the node died at every effect boundary of every step shape (9 shapes in `crash`; all 15, recovery included, in `two crashes`).
* **Mutants.** Twenty-one one-edit engines ([check.py](check.py) `MUTANTS`). Every invariant above is the target of at least one; the check fails otherwise.

| Mutant | Violates | Seed | Minimal trace |
|---|---|---|---|
| schedules the activity in the step that writes the intent | `history-is-true` | 2 | 3 steps |
| sends a retry without the idempotency key | `one-effect-per-activity` | 5 | 21 |
| undoes the oldest started activity first | `compensation-matches` | 1 | 39 |
| takes the completion of any key for the one it is waiting for | `history-follows-definition` | 28 | 29 |
| takes the completion of an earlier attempt for the one in flight | `history-is-true` | 277 | 32 |
| undoes only activities known complete, not the one in flight | `compensation-matches` | 1 | 39 |
| accepts a cancel after the workflow finished | `terminal-is-final`, `inv.never-regresses` | 2 | 24 |
| treats an accepted cancel as the end and undoes nothing | `memory-is-replay` | 3 | 36 |
| abandons compensation without trying a single undo | `history-follows-definition` | 1 | 35 |
| tries an activity once more than the definition allows | `history-follows-definition` | 1 | 36 |
| carries on past an activity whose attempts are used up | `completed-is-whole` | 13 | 61 |
| takes a branch exactly when the result is not the one it names | `completed-is-whole` | 3 | 43 |
| tells the client `completed` whatever the outcome | `reports-are-true` | 1 | 50 |
| tells the client `completed` in the step that writes the row | `reports-are-true` | 16 | 27 |
| finishes the workflow and tells the client nothing | `inv.client-is-told` | 2 | 27 |
| reads the history back but does not apply it | `memory-is-replay`, `inv.recovery-reproduces` | 0 | 15 |
| numbers its rows from 0 again after a restart | `history-is-a-log`, `inv.append-only` | 0 | 20 |
| comes back from a restart without resuming | `inv.settles` | 1 | 11 |
| retries at once instead of after the backoff | `inv.backs-off` | 0 | 9 |
| takes the expiry of any timer for the one of its position | `inv.backs-off` | 4 | 15 |
| gives an undo no time to be acknowledged | `inv.stuck-needs-a-fault` | 15 | 25 |

  For each, [mutants.json](mutants.json) retains the seed and the SHA-256 of its trace on the order definition, and [mutants/](mutants/) a 1-minimal trace. The check rebuilds the mutant, reproduces the seed's trace byte for byte, confirms the unedited engine passes the same seed, replays the minimal trace, confirms it still fails and that removing any one step loses the failure, confirms the refinement check rejects the mutant binary against the unedited source and the Maude step check rejects its translation. For the eighteen whose invariant is not about clock times it then searches the same mutant on a small definition in Maude: a violating state is reachable, and shen-jit confirms the verdict on that state.

  * The witness for the dropped key has two commits of one activity in its ledger (`reserve` under key 101 and under key 0). The earlier witness had a single commit and failed only because key 0 was undeclared.
  * The runs of `restart-without-replay` are the teeth of the path-sensitive regression check: of 120 seeds 5 regress across a crash; judged as graphs, `inv.never-regresses` fails along the path in all 5 and on a single transition in none.
  * A mutant is judged by invariants that do not call it. Three engines a reviewer found passing every check (inverted branch, one retry too many, exhausted failure treated as success) each fail now.

## Step time, and whether specialization bought anything

`python3 -m targets.workflow.bench` times the steps of the 400 seeded runs above (same trace digests), in order, in three lanes: `node_step` on the recorded input vectors, and the unchanged `engine.shen` applied to the same states and events by `shen-jit eval` and by a sealed `shen-jit build` executable. Process CPU time, 11 trials of 50 passes per lane after one warm-up pass; all lanes must emit the same number of effects. Retained in [bench.json](bench.json): Apple arm64, macOS 27.2.1.

| ns per step | order (12,788 steps) | docs (16,561 steps) |
|---|---|---|
| native, specialized | median 24.9, quartiles 24.6 to 25.1, range 23.9 to 25.4 | 25.8, 25.5 to 26.5, 25.4 to 27.3 |
| shen-jit eval, interpreting | 44.9, 44.3 to 45.4, 44.3 to 52.7 | 45.7, 45.2 to 46.3, 44.8 to 55.5 |
| shen-jit build, interpreting | 56.8, 56.0 to 60.7, 55.5 to 79.1 | 55.3, 54.5 to 55.7, 54.2 to 59.9 |

* The specialized native step is about 1.8 times faster than shen-jit running the engine as an interpreter. shen-jit is a JIT compiler, and on six-instruction definitions the engine's interpretive overhead is small. Two more runs on the same day, with other work on the machine: medians of 25.5 (native) against 46.2 and 54.1 for order and 26.1 against 44.8 and 64.5 for docs; and 32.7 against 60.0 and 74.4, 32.2 against 64.9 and 69.5, with ranges up to 39 and 80. Ratios between 1.7 and 2.5. The retained receipt is the quietest of the three.
* The engine as it was before timers carried `Seq` and timeouts had their own row measured 23.8 and 23.3 ns native in its retained run. The residual programs are about 4% larger now (order: 5,856 IR nodes against 5,629); the two receipts are from different days and were not measured side by side.

* **As first measured it was six times slower:** 241 ns per step against 42. About 215 ns of that was `setjmp` saving the signal mask on every step, in the kernel's ABI template and unrelated to the workflow. That is fixed in a kernel commit (`sigsetjmp` without the mask). The earlier figure and two diagnostic builds (one with `_setjmp`, 23.5 ns; one that leaves the slot array uncleared, no faster) were one-off runs of the same benchmark and are not retained.
* **What cannot be said:** how much of that factor is specialization. There is no unspecialized native engine to compare with: the check tries to compile the engine with its definition as runtime data (leaves in the state, list structure static) and the compiler refuses it, because the shape of the engine's own state, one mark per key, depends on the definition. The lanes also differ in code generator and data representation (integer vectors against allocated lists). What specialization did buy is that the engine can be compiled by this compiler at all, to a step function with no Shen runtime and no trace of the definition language.
* One machine, CPU time of whole passes; not a latency distribution and not an end-to-end throughput.

## What is not established

* **No parallelism.** One activity is in flight at a time. `each` handles its keys in order; it is not a parallel fan-out. One workflow per node, one node per world.
* **A timeout counts as a failed attempt.** A workflow whose activity succeeded can still be compensated because the completion was slow.
* **Compensation can be abandoned.** After `UndoAttempts` unacknowledged tries of one undo the workflow ends `stuck` with committed effects not reversed, and that settles the run. No seeded run ends stuck any more (0 of the 800 runs with faults in the check, and 0 of 6,400 further seeds in a one-off run), so `stuck` is exercised only by the exhaustive bounds, where a timer may fire at any moment, and by one mutant.
* **The reference is a second reading by the same hand.** `inv.meaning` is independent of the engine's text and catches an engine that disagrees with it. A misunderstanding of the language shared by `engine.shen` and `invariants.shen` is not caught by either.
* **`completed-is-whole` is the only functional statement, and it is about completed workflows.** For a compensated or stuck workflow the invariants say which rows were legal and that the reversals match the commits, not that the right activities ran before the failure.
* **Clock times are checked for the backoff only,** and only in the seeded runs: `Timeout`, `UndoTimeout` and the `Delay` of a sleep are not held against the clock, so an engine that times out early or sleeps too briefly passes. A timer is not a durable deadline: after a restart the timeout, backoff or sleep of the position starts again from zero.
* **The attempt number of a completion is checked for failures only.** An engine that takes a *successful* completion of an earlier attempt for the one in flight passes every invariant, and no harm from it was found: the effect is committed under the same key.
* **"No key commits twice" is a property of the simulated workers,** not of the engine: against them it cannot fail for a declared key. Both halves of the worker contract are assumptions on real workers; the second (a key that was undone never commits later) is more than ordinary idempotency-key APIs give.
* **A cancel can be lost.** One that arrives while a write is in flight or while the history is being read back is ignored, not remembered. **A report can be lost** too: the engine reports once per arrival at the outcome and asks for no receipt, so `inv.client-is-told` is about fault-free runs.
* **Writing the intent before scheduling was not shown to matter end to end.** The mutant that schedules first violates only `history-is-true` (a worker ran an activity whose intent was not durable). With workers that deduplicate and an engine whose decisions are a function of the durable history, no other invariant was seen to fail.
* **The stale-acknowledgement defence is invisible to the standard checks.** The mutant without it is caught only under `KeepsCompletions`, and that world is explored by the simulator alone, not by Maude.
* **Invariants in Maude are the state invariants.** The run invariants are evaluated on simulated traces; two of them also on the simulator's graph (`inv.append-only` on edges, `inv.never-regresses` on paths), not in Maude. The three that read clock times or fault-freedom (`inv.backs-off`, `inv.stuck-needs-a-fault`, `inv.client-is-told`) have no exhaustive counterpart except the idle-world question. Liveness is exhaustive only inside the six bounds; for order and docs it is the declared bounds (100 and 120 steps) holding on 400 seeds each, with observed worst cases 67 and 83. The bounds are declared constants, not derived from the definitions.
* **Maude meets `each`, `branch` and `sleep` in one bound,** mix with a cancel and no crash, loss or duplication, and in the step-by-step comparison on the seeded pairs of order and docs. The crash, loss and duplication bounds are on definitions with `act` and `end` only.
* **Crashes at every effect boundary** are exhaustive only in the two bounds with crashes. The seeded runs hit 16 and 17 of the 20 boundaries seen per definition; the ones not hit are in steps taken during recovery, and the check prints them.
* **The workers and the client exist twice**, in Python and as Maude rules, both written here by hand. Their agreement is the equality of observation sets at the six bounds.
* **Specialization is not complete unfolding.** The engine looks up "the activity at Pc" by scanning the program with a static index, so each arm sees its instruction statically, but the operands of same-kind instructions are then joined into temporaries. The residual program has no test on definition syntax; it does carry a decision chain on `Pc` that assigns those operands. The scan is a binding-time improvement, in ordinary Shen that shen-jit runs unchanged.
* **Validation is by shen-jit at build time,** before the compiler runs. It is not part of the compiled step, and a definition that never went through `model.py` is not checked.
* **Evidence is differential and bounded**, as for every kernel example: native against shen-jit on the pairs reached, Maude against the simulator on observation sets. "Minimal" traces are 1-minimal under step removal, up to 61 steps; a trace for an invariant that is only asked of finished runs (`inv.settles`, `inv.client-is-told`) can hardly shrink.
* `flake.nix` has a `workflow` check of its own, next to `sim-tool`. It is a separate gate because it takes about six minutes and other targets are being added to that line.
* Not attempted: Solo5, more than one node, real time.

## Kernel changes this target needed

* `Workload.deliver(world, payload, draw)` in the simulator and `workload["output"]` in the Maude environment: a workload can react to each delivered output, which is what makes the activity workers part of the environment. Defaults are the old behavior.
* `python3 -m tool check <name>` falls back to `targets/<name>/check.py`.
* The native trap is armed with `sigsetjmp(env, 0)` instead of `setjmp`.

The compiler itself (`tool/pe.py`, `tool/compile.py`) is unchanged. The second round of work on this target (the invariants' own reading of the definition, the mutants found by review, timers by `Seq`) changed nothing under `tool/`.
