# Building an application and its semantics together

## The interpreter hypothesis

Can a developer extend an interpreter with an application operation, specialize it into executable code and an exploration tool, and still check what both artifacts mean against an independently accepted contract? Start in this repository with a Fenrir-compatible subset. The first engineering investment is the interpreter prototype. Its result should inform any future investment in the Fenrir compiler.

Treat `AcceptedJob` as the first versioned semantic module. Record its typed operations, logical and attempt identities, assumptions about storage and identity lifetime, safety and progress obligations, and the mappings and evidence for each realization. Give each witness the contract, handler and mapping revisions, the inputs and environmental choices, and the observations by actor. A changed handler invalidates evidence that depended on its guarantees. A counterexample can motivate a requirement change, but only an explicit decision authorizes one.

### What specialization must preserve

| Artifact | Fix during specialization | Check |
|---|---|---|
| Submit function | Program, schema and concrete handler interface | Under matched inputs and environment choices, interpreted and residual executions have the same projected observations. Check these independently against the contract. |
| Bounded explorer | Program and exploration rules | Compare sets of observable histories over the same declared bounds. Retain the crash-between-commit-and-reply path and a replayable schedule for each distinguishing outcome. |
| Monitor | Application contract and observation vocabulary | Compare generic and specialized verdicts on accepted, forbidden and inconclusive prefixes. |
| Later abstract analyzer | Program and abstract transfer functions | Its approximation must include reachable concrete observations. State the soundness argument and report precision separately. |

Start with the submit function and explorer. A monitor can specialize the separate contract interpreter. Staged abstract interpretation and staged symbolic execution provide precedents for deriving different artifacts. Their measurements concern their workloads. Presto offers a further experiment if equational simplification becomes expensive during Maude search. Compiler generation through the second and third Futamura projections remains a later question requiring suitable self-application. [Staged Abstract Interpreters](https://www.cs.purdue.edu/homes/rompf/papers/wei-oopsla19.pdf) · [Compiling Symbolic Execution with Staging and Algebraic Effects](https://www.cs.purdue.edu/homes/rompf/papers/wei-oopsla20.pdf) · [Presto](https://arxiv.org/abs/2112.10201)

## Start with a decision someone must actually make

Suppose a client loses the response to a job submission and the worker restarts. The client has the same evidence in two worlds. In one the transaction committed. In the other it rolled back. Asking the user to “define retry semantics” is hard. Showing those two histories and asking *what should the second submit return in each?* gives them a tractable design decision.

A proposed requirement is that within one tenant the same key and payload digest denote the same submission. A retry after commit returns the original receipt. A retry after rollback may create the job. Reusing the key with a different digest yields a conflict after commit. The contract assumes that the digest adequately identifies the payload for this application. Making that assumption explicit lets a later application choose byte equality instead.

Now change the application. Add a “cancel” button. Does cancelling after commit stop the caller waiting, withdraw a pending job, or undo work already performed by another party? All three can look like a successful HTTP response. Show the designer a trace with a commit, cancellation, restart and delivery. Preserve rival policies until someone chooses. Record the chosen witness and a forbidden alternative alongside the version of the requirement. This is the first real test of parallel design. A fixed model and a permanently broken implementation would test only conformance.

## Three artifacts with different jobs

| Artifact | Example | What would falsify it? |
|---|---|---|
| Accepted contract | One durable obligation per tenant and request key, stable receipt on replay | A stakeholder accepts a concrete history the contract excludes, or rejects one it admits |
| Executable strategy | Build a guarded write set and install it in one transaction | A permitted crash schedule produces two durable obligations |
| Correspondence | Map committed storage rows to logical obligations and delivered replies to client observations | A faulty implementation passes because the adapter collapses duplicate rows or treats an emitted reply as a delivered one |

A trace needs an ordered sequence of actions, independent observations, actor and operation identities, and the environment choices required for replay. The observation mapping may hide local instructions. It must preserve multiplicity where the contract counts jobs. Reconstruct storage observations from durable state after restart. Logging what the worker intended to write would make the checker blind to a failed write. Record which observations require test instrumentation and which a deployed monitor can actually obtain.

For each allowed environment, the safety question is whether every projected implementation history is admitted by the contract under the corresponding inputs. A production implementation can legitimately resolve an internal choice. An explorer that claims complete bounded coverage must account for *every* choice in its bounds. A monitor receiving a prefix may have to answer “still possible.” Infinite silence can satisfy a prefix safety check. Track supported inputs, reachable success cases and progress separately. For a bounded progress test, restore the worker, allow delivery, stop injecting faults and give it a declared number of steps. An unbounded eventual-response claim requires its own fairness assumptions and argument. [Abadi and Lamport on refinement mappings](https://lamport.azurewebsites.net/pubs/abadi-existence.pdf)

## Where specialization buys something

Write a tiny interpreter whose programs can inspect a request, construct a transaction, invoke a storage handler and branch on its reply. Fix the program and the transaction schema. The request, stored rows, storage result, crash point and reply delivery remain dynamic. A first residual program should contain the specific guard, write construction and reply branches, with no dispatch over application syntax. Ordinary generated functions targeting the host compiler suffice. Inspect this output before optimizing it.

The operator who chooses SQLite has supplied an execution strategy and an atomicity assumption. Specializing the interpreter cannot supply either. The production handler executes the transaction. A finite exploration handler can fork at its outcome and at subsequent reply delivery. A separately authored application contract can also be specialized into a trace monitor with its transition dispatch precomputed. Each output has its own check: residual execution agrees with the original interpreter under matched choices, bounded exploration preserves the set of observable histories, and monitor specialization preserves verdicts on both admitted and rejected prefixes. Sharing the interpreter between two outputs creates correlated errors. Retain independent domain witnesses and replay against an actual storage handler.

This makes the first Futamura projection testable on a small program. A hand-coded transformation for one submit operation would demonstrate a useful optimization. To demonstrate interpreter specialization, freeze the specializer and change the interpreted program, for example by changing the conflict branch. The residual behavior must follow without editing the generator. A hand-written generator is a useful cost and performance baseline. Self-application and compiler generation can wait until producing residual programs itself pays off. Staged abstract interpretation is a precedent for specializing multiple interpretations. Its benchmark speedups describe its analyses and provide no Fenrir performance target. [Wei, Chen and Rompf](https://www.cs.purdue.edu/homes/rompf/papers/wei-oopsla19.pdf)

## The composition experiment

Give each component the smallest obligation it can discharge. Stable identity connects attempts. Atomic storage couples the job, digest and receipt. A replay rule reads the stored result. The composed argument relies on the guard and the writes sharing a serialization boundary, tenant scope and key lifetime. Run two overlapping submissions with the same key. A lookup followed by a separately atomic insert or append can let both callers observe absence. Also exercise distinct tenants using identical keys. These histories distinguish plausible implementations of the individual operations.

Now move publication to a remote service. If it publishes and the worker crashes before recording success, a retry can publish twice. Writing the local mark first admits the dual failure where nothing is published. Both cases are compatible with perfect local transaction durability. Try three concrete interpretations: locally atomic publication, independent remote publication, and a remote provider that durably deduplicates a stable operation identity. For the third, make retention explicit. A retry after the provider forgets the identity is a distinguishing history. Report the guarantee each interpretation earns and the assumptions the caller still owes. Merely attaching an “idempotent” label to all three would be a failed experiment.

Two starting points already exist in neighboring checkouts:

- Fenrir's worker constructs the scoped transaction, and its Maude comparison script already runs a Shen comparison and replays a witness. Use those as existing controls. Sources inspected at HEAD da55d43. [Worker](https://github.com/pyrex41/fenrir/blob/da55d436f40bc15aedd16eed2305f4694d2bc6e7/examples/service/accepted-job.fen) · [Maude comparison runner](https://github.com/pyrex41/fenrir/blob/da55d436f40bc15aedd16eed2305f4694d2bc6e7/verification/maude/run-accepted-job-retry.sh)
- Capcov's intended outbox explicitly assumes publication and the attempted mark commit together. Its legacy fixture can republish. Compare abstract identities, observations and fault boundaries before trying to replay a shared witness. A local job insertion and a remote publication are different observations. An adapter that legitimately rejects the mapping is a valuable result. Sources inspected at HEAD c519f43. [Intended outbox](https://github.com/pyrex41/capcov/blob/23f7841ca7369bf793c633e682fb00c77afc644c/tests/claim_semantics/fixtures/systems/outbox-intended.ops) · [Legacy outbox](https://github.com/pyrex41/capcov/blob/23f7841ca7369bf793c633e682fb00c77afc644c/tests/claim_semantics/fixtures/systems/outbox.ops)

### What the Capcov adapter should establish

Try Capcov's existing operations, state, outcomes and effect records as the behavioral vocabulary for the first module. Its R02 bridge has code and tests for invocation identities and indeterminate outcomes. The broader typed-behavior architecture describes a design. The R10 probe explicitly identifies atomic publication as an assumption and a separate external delivery attempt as a gap. [Behavior architecture](https://github.com/pyrex41/capcov/blob/23f7841ca7369bf793c633e682fb00c77afc644c/docs/typed-behavior-architecture.md) · [Bridge tests](https://github.com/pyrex41/capcov/blob/23f7841ca7369bf793c633e682fb00c77afc644c/tests/claim_semantics/test_r02_bridge.py) · [R10 probe](https://github.com/pyrex41/capcov/blob/23f7841ca7369bf793c633e682fb00c77afc644c/tests/claim_semantics/README.md#r10-probe-outbox-migration-on-the-current-system-ir)

Use one counterexample format for Fenrir and Capcov. For each adapter, name the logical operation, the event being counted, who observes it and where it commits. Try to replay the same violation through both paths where those meanings match. A duplicate durable job and two delivery attempts can share a retry schedule while violating different clauses. In that case report the unmapped clause and keep both witnesses. This is the first test of whether the project needs a new behavioral representation at all.

## Which project might emerge?

| Direction | Cheapest honest prototype | Decision it earns |
|---|---|---|
| Development tool for existing languages | Independent monitor plus replay adapter for one native worker | Keep it small if most value comes from discovering ambiguous requirements and retaining witnesses |
| Library of semantic operations | Reuse the submit contract on a second application with a different failure boundary | Introduce a module interface when the second application reveals which assumptions actually vary |
| Application-specific language with a specializable interpreter | Freeze a tiny evaluator and stage two genuinely different programs | Invest in staging if generated artifacts track semantic changes and recover enough execution cost to justify the generator |
| Fenrir language extension | Express an operation as an effect with alternative handlers | Change Fenrir once a repeated guarantee is awkward or unenforceable at the library boundary and a language feature would discharge it |
| Learning system | Give an agent two solved cases and an unseen third with weaker storage assumptions | Retain inferred concepts only if they improve transfer and identify cases where their assumptions fail |

These can yield different successful projects. An excellent requirements and replay tool can survive a disappointing compiler experiment. A fast partial evaluator can coexist with a poor vocabulary of application contracts. Evaluate these outcomes separately before combining infrastructure.

## First implementation cycle

Begin with the witness below and use it immediately for the residual program. Once the baseline exists, the cancellation experiment and staging can proceed independently. Probe the Capcov mapping as soon as the first trace is available. Full outbox transfer and the held-out learning trial depend on the resulting module obligations.

1. **Baseline and witness.** Pin the source revisions, toolchain and adapter versions. Reproduce the existing retry witness and a valid restart history. Declare one tenant, one key, two digests, at most two submissions, one crash and one lost reply for the sequential slice. Record schedules, terminal observations and rejection reasons. Extend to overlapping submissions and two tenants as independently labeled suites. Keep schedules with equal observations available for replay.
2. **Parallel change.** Add the cancellation decision above in a separate contract revision. Implement one chosen policy and keep the rejected policy as a negative control. Classify disagreements as implementation, observation mapping, environment assumption or requirement. Only an explicit requirement decision can change an accepted history. Rerun dependent evidence when a contract, handler or mapping changes.
3. **Residual program.** Independent of the cancellation decision, implement the staged subset on macOS ARM64. Use a pure in-memory handler with injected replies for paired timings, followed by a real SQLite run for relevance. Measure interpreter, residual and direct hand-written implementation on identical histories. Record generation time, residual size, cold and warm execution, inputs and dispersion. Compute break-even executions from generation cost divided by time saved per run when positive. Include a held-out program change and a fault after commit. Solo5 becomes a portability test after host behavior agrees.
4. **Transfer.** Adapt the outbox fixture at its declared boundary. Then run the split-publication counterexample. Count new adapter code, new assumptions and obligations that needed human judgment. A second monitor copied from the first with domain names changed provides a strong baseline. Keep a semantic module only if it removes duplicated reasoning while still rejecting an invalid instantiation.
5. **Learning trial.** Freeze approved requirements, evaluator and held-out tasks before exposing the training examples. Compare equivalent agent and compute budgets with examples plus tests and replay, a hand-authored semantic library, and an inferred library. Score correct solutions, false guarantees, repairs, total time and human decisions. Include key expiry and an ambiguous remote completion. A repeated unsafe guarantee disqualifies that candidate. [DreamCoder's library-learning precedent](https://arxiv.org/abs/2006.08381)

The first review should fit on one screen: a required history, a forbidden neighbor, the minimal difference between them, and the code or contract decision it changes. The second review adds a readable residual program and its measured costs. If specialization only wins inside the interpreter and storage dominates end-to-end, keep it for fast exploration if that benefit measures out. If an external adapter consumes more effort than expressing the second application directly, narrow the domain before designing a common IR. If a hand-written library transfers just as well as a learned one, distribute the library.

## What remains open

A finite test can find a bad mapping but cannot establish the adequacy of every observation. A model proof inherits its environment assumptions. Production histories may expose too little to decide a property. Keep unknown verdicts and unmet assumptions visible. Consider Maude when an independent rewrite model or symbolic search can distinguish two candidates, and a proof assistant when one stable, repeatedly used law merits a universal argument. Neither is a prerequisite for the first review.

The larger question is economic as much as mathematical. On the second application, did the prior semantic work let someone make a better decision sooner? Measure the cost of writing and maintaining the model alongside the code. A shorter generated program or a larger collection of passing histories alone cannot answer that question.
