# Compile executable behavior to native machine code

The developer authors an executable specification of behavior. Interpret it to inspect its meaning, then specialize the evaluator for that specification and its fixed policy choices to produce a standalone native executable. Runtime inputs remain dynamic. A developer should be able to change a rule, recompile, and observe the change without writing an application implementation.

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
