# Compile executable behavior to native machine code

The developer authors an executable specification of behavior in ordinary Shen. Interpret it to inspect its meaning. Specialize it to a native step function. Run that function under a deterministic simulator that owns every source of nondeterminism, check invariants written in Shen over the traces, and explore the same rules within bounds in Maude. A developer should be able to change a rule, recompile, and see the change in the native code, in the simulation and in the search, without writing an application implementation.

Three shortcuts do not count as reaching this goal:

1. **Tabulation.** Enumerating inputs through the interpreter and emitting a table is not specialization.
2. **Hand staging.** A specification that returns code trees is a hand-written compiler.
3. **Semantics in the shim.** Guarantees must come from the Shen specification. Hand-written C or Python may only perform dumb effects.

## Where the goal stands

Each line is established by the command beside it, run with `nix develop -c`, and only to the extent the linked README states. [README.md](README.md) has the same commands with what each pass means and how long it took.

| Part of the goal | Standing | Command |
|---|---|---|
| Specialize ordinary Shen to native code | Done for a first-order fragment: integers, declared symbols and booleans in states of fixed shape. Compared with shen-jit row by row, traps and overflow included. [Limits](tool/README.md#compiler-limits) | `python3 -m tool check kernel` |
| A simulator that owns all nondeterminism | Done for one step function per world: clock, network, keyed disk, crashes between any two effects of a step, schedule. Seeded runs replay byte for byte. [Limits](tool/README.md#simulator-limits) | `python3 -m tool check sim` |
| Invariants in Shen over traces | Done. Every application below states its guarantees as Shen functions that do not call the node, and keeps at least one mutant that each invariant rejects. | the application checks below |
| The same rules explored in Maude | Done within small bounds. The Shen text is translated clause by clause; Maude and the simulator must reach the same observations. The translator is tested, not proved. | every check below |
| A first application | AcceptedJob with the commit boundary inside the specification. [README](tool/examples/accepted_job/README.md) | `python3 -m tool check accepted_job` |
| More than one node | Gossip on three nodes; Raft on three and five. | `python3 -m tool check gossip`, `python3 -m tool check raft` |
| An interpreter specialized to its program | A durable workflow engine in ordinary Shen, specialized per workflow definition. The check shows mechanically that the definition language does not survive into the residual code. [README](targets/workflow/README.md) | `python3 -m tool check workflow` |
| A protocol with published properties | Raft: the five properties of Figure 3, nine further rules and linearizability on seeded runs, with 22 mutants. [README](targets/raft/README.md) | `python3 -m tool check raft`, `python3 -m targets.raft.check mutants` |
| The native code outside the simulator | An effect shim on Solo5: 19 scripted runs under `solo5-spt`, each shown to be a simulator run. Three Raft servers as three unikernels, the test harness as the network between them: 35 runs of seven scenarios, each shown to be a simulator run on which Raft's invariants and linearizability hold. [README](tool/solo5/README.md) | `python3 -m tool check solo5`, `python3 -m tool check solo5-cluster` (both need a Linux container on macOS); `python3 -m tool check solo5-host`, `python3 -m tool check solo5-cluster-host` |
| Change a rule, see the change | Shown by the mutants: 8 for AcceptedJob, 2 for gossip, 21 for the workflow engine, 22 for Raft, 21 images for Solo5 and 7 for the Raft cluster on it (one of which, `local-get`, is NOT found there). Each is one edit, recompiled, and rejected by a named check on a retained seed or scenario. | the checks above |

## What is not established

* That specialization preserves meaning. The evidence is differential: native against shen-jit on the rows and pairs each check reaches.
* That the simulator and the Maude environment are the same model beyond the observation sets at the bounds compared. One author wrote both.
* Safety beyond the seeds and bounds run. For Raft, the two best-known failures (Figure 8, the lost vote) are found only under fault profiles written to produce them.
* Liveness, except as a measurement on seeds and as the absence of cycles in the workflow engine's bounded searches.
* That every run of the Solo5 shim is a simulator run. It is shown for 19 scripted single-node runs and 35 cluster runs under `solo5-spt` and argued for the rest. `solo5-hvt` was not run. Durability on Solo5 means surviving the death of the unikernel, not loss of power on the host.
* Raft on Solo5 beyond seven scenarios. Three servers run as unikernels under `solo5-spt` with the harness as their only network; a run is recorded, not reproducible from its seed; clocks are not skewed and writes are not lost; of three Raft mutants tried on real images one is not found ([tool/solo5/README.md](tool/solo5/README.md#what-the-cluster-runs-do-not-establish)). The workflow engine was not built as a unikernel.
* What specialization buys in speed. The one measurement is the workflow engine's step at about 1.8 times shen-jit ([receipt](targets/workflow/bench.json)); no unspecialized native engine exists to compare with.
* Anything about real time, real networks, more than one disk per node, disk corruption, or a node with a source of randomness.

## Earlier claims that the newer work corrects

* **AcceptedJob in [logic/](logic/build.py) is tabulation, not specialization.** `logic/build.py` evaluates all 28 pairs of 7 states and 4 events in shen-jit or Maude and emits the answers as branches. This file used to call it "the source-language specializer". `python3 logic/check.py` establishes that the Shen and Maude definitions agree on those pairs and that the table follows an edit; it establishes nothing about specialization.
* **The JSON job specifications in [native/](native/spec.py) keep the commit outside the specification.** The rules are a table; the SQLite transaction is hand-written C in [native/store.c](native/store.c). The guarantee that an accepted job is durable therefore comes from the shim. [tool/examples/accepted_job](tool/examples/accepted_job/README.md) replaces it: the write, the wait for its acknowledgement and the reload after a crash are in the Shen, and a specification edited to reply before the acknowledgement is rejected.
* **The four examples in [examples/](examples/) are hand staged.** Their Shen and Maude build the residual program themselves. They test the shared backend.
* **"Compile a second independently chosen application", the follow-on this file ends with, is done.** After AcceptedJob and gossip, the workflow engine and Raft were each written by a separate lane against the finished compiler, and neither changed `tool/pe.py` or `tool/compile.py`. What they needed from the rest of the kernel is listed in their READMEs: a workload that reacts to outputs and a cheaper trap (workflow), optional simulator profile keys and a timer budget in both environments (Raft).

The sections below are the record of the earlier cycle. They are kept as written, except where a note says otherwise.

## The first vertical slice, as it was described

*Note: "specializer" and "specializes" in this section describe tabulation and a rule table emitted as branches; see the corrections above.*

Author behavior directly in [Shen](logic/accepted-job.shen) or [Maude](logic/accepted-job.maude). The [source-language specializer](logic/build.py) executes either definition over a fixed interface and produces a native binary. The [differential check](logic/check.py) compares both source languages, tests each native transition and rebuilds after independent edits to each source. Current source-language coverage is the finite two-digest job profile.

The first vertical slice is [the job specification](native/job.json) and [the native backend](native/spec.py). The backend specializes a finite transition interpreter into C branches and invokes Clang. Cancellation is a static policy binding. The default withdraws pending work at a workflow boundary. The alternate binding preserves the pending obligation. Both leave a completed delivery intact. The [durable variant](native/durable-job.json) now declares a uint32 instance key and a keyed-state storage effect. Native and interpreted executions share SQLite state across restarts. The [receipt specification](native/receipt.json) adds enum-valued state, inputs and receipts. Equality and difference guards specialize to native branches. The [SHA-256 receipt spec](native/sha256-receipt.json) supports full runtime digests. Its equality-only rules compile using representatives for the stored value and a distinct incoming value, then return the actual selected digest.

The [combined AcceptedJob specification](native/accepted-job.json) retains the payload digest through cancellation and delivery. Its cancellation target is bound at compilation. Run `python3 native/check_accepted_job.py` to compile both policies and replay duplicate, conflict, cancellation, delivery and terminal retry across fresh processes.

The learner is an optional proposer of reusable spec fragments and laws from prior specs and counterexamples. The earlier pilot compares their usefulness. It is a separate research question. The compiler works from a developer-authored spec without a learner.

## Prior experiments and supporting evidence

## First review

Deliver a one-screen, replayable answer to this question: **When a client retries after losing its reply, what does the system owe it?**

- Pin Fenrir's retry control at `da55d43`, the toolchain and the observation adapter. Start with one tenant, one key, two possible payload digests, at most two submissions, one crash and one lost reply. Declare the scheduling choices covered.
- Show a required history. The first submit commits, its reply is lost, the worker restarts, and the same request returns the stored receipt. Inspect storage after restart and retry to establish that exactly one durable job exists.
- Show the nearest forbidden history. Give the client the same receipt but leave two durable jobs. Preserve both rows when translating storage into contract observations. Record the schedule, the differing observation, the rejection reason and the implementation decision it changes.
- Replay the rollback neighbor too. After rollback the retry may create the job. Check that success is reachable once faults cease within a declared recovery budget. Report the tested bounds and any observations unavailable to a production monitor.

Replay the [three retained histories](experiments/accepted_job/witnesses.json) with `python3 experiments/accepted_job/witness.py`. The first result is a harness observation captured before transmission. Under rollback it is a provisional result. The implementation decision is whether the retry reuses the committed row and its receipt.

Use this witness immediately to test the first Futamura projection. Fix the submit program and transaction schema, generate a readable function, then replay the same storage outcomes and delivery choices against the interpreter, function and independent contract. Repeat with a changed conflict branch and a frozen generator. The witness sets the acceptance test for the first coding experiment.

## Decisions the rest of the cycle must earn

| Work | Evidence to bring to review | Decision |
|---|---|---|
| Change the requirement | Exercise overlapping submissions and separate tenants as labeled suites. Present rival cancellation policies on the same history. Record the explicit choice, revise the contract and strategy, retain the rejected alternative, and replay affected evidence. | Can a designer change a guarantee and see its consequences in the worker? |
| Specialize the interpreter | On macOS ARM64, freeze the generator and change the interpreted program. Compare interpreter and generated code under matched choices, including a fault after commit. Inspect the output. Time both against direct code with an injected handler and with SQLite. Report generation cost, size, cold and warm timings, dispersion and break-even when savings are positive. | Where, if anywhere, does specialization repay generation and maintenance effort? |
| Transfer to an outbox | Pin Capcov's fixtures at `c519f43`. Compare atomic local publication, split remote publication, and provider deduplication through a declared retention window. Count actual effects. Compare adaptation effort and human decisions against writing the second monitor directly. | Which parts can a second developer reuse, and which assumptions must they supply? |
| Test learning | Freeze held-out tasks and evaluation criteria. Under matched budgets, compare examples plus replay, a hand-authored library and a learned library. Include key expiry and ambiguous remote completion. Record false guarantees, repairs, time and human decisions. | Does learning improve transfer enough to justify its cost? Disqualify a candidate that repeats an unsafe guarantee. |

Keep the application contract independently authored. Check strategy, observation mapping and environment assumptions separately when replay disagrees. Preserve unknown verdicts where the available observations cannot distinguish outcomes.

The cycle is complete when the evidence supports a decision on each claim, or records precisely why it remains open. Recommend the smallest follow-on the evidence warrants. A requirements-and-replay tool is a viable outcome even if staging or library learning yields no measured benefit. The [research plan](RESEARCH.md) gives the experimental details.

## Evidence and open decisions

| Claim | Current evidence and decision |
|---|---|
| Receipt replay and application correspondence | [Three required histories](experiments/accepted_job/witnesses.json) distinguish replay, duplicate insertion and rollback. [Pinned Fenrir replay](experiments/accepted_job/fenrir_control.sh) independently distinguishes one durable job from two. |
| Contract change | Both [cancellation policies](experiments/accepted_job/cancellation.py) run through restart and disagree on delivery. The native spec now supports both choices at specialization time. |
| Interpretation and specialization | The [bounded explorer](experiments/accepted_job/explorer.py) preserves schedules; the [prefix monitor](experiments/accepted_job/monitor.py) preserves bounded verdicts. The [paired timings](experiments/accepted_job/timings-matched.json) support evaluating staging for repeated in-memory execution. SQLite payoff and maintenance cost remain open. |
| Outbox transfer | The [direct comparison](experiments/transfer.py) finds identical witness groups with four adapter lines versus sixteen direct lines. The second domain still supplies the identity, retention and observation boundary. Independent developer time remains unmeasured. |
| Learning | The [frozen pilot](experiments/learning/PROTOCOL.md) has its tasks, evaluator, feedback interface, common training examples and a pinned pre-existing reference library. Nine independent runs passed both tasks, including all three examples-only runs. This pilot found no correctness advantage from either library. [Runs and usage](experiments/learning/runs/results.json). |

The pilot reached a ceiling on correctness. Keep direct compilation of developer-authored specifications as the primary path. The smallest follow-on is to compile a second independently chosen application and record which behavior its author can express without changing the backend. The existing timing estimates measure Python staging. Native performance and long-term compiler maintenance cost remain unmeasured.
