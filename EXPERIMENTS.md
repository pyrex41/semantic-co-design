# Research experiments and evidence

How small can a problem be before it makes sense to build a language for it? One way to find out is to build an application and its vocabulary of guarantees together. When someone changes what a request means, the environment should show an execution where the decision matters. When the decision settles, the same work should help implement the application and explain what its implementation owes the caller.

Consider an API that accepts a job. You hit submit, the connection drops, and you hit submit again. Getting the same receipt twice sounds like success. But what if two jobs ran? To answer, we have to decide whose observations count. A client can see receipts. A storage inspector can count durable jobs. A remote provider can count effects that neither the client nor the database sees. Choosing the observer changes what we can promise. That decision belongs in the design of the application.

The research bet is that a small application-specific interpreter can make these decisions cheap to explore, and that specializing it can make the chosen behavior cheap to execute. An independently written contract gives both the interpreter and the generated program something to answer to. The most interesting output may be a new operation that another programmer can use, together with an explanation of precisely when they can use it.

Fenrir supplies the independent execution control. The [research plan](RESEARCH.md) works through the first experiment, the alternatives it distinguishes and the results that would change direction.

## Optional external controls

The compiler quick start needs only the main checkout. For the Fenrir control, Capcov transfer experiment, and learning reference-library reconstruction, clone the public source repositories next to this one. Run the commands from the semantic-co-design repository root.

```sh
git clone https://github.com/pyrex41/fenrir.git ../fenrir
git clone https://github.com/pyrex41/capcov.git ../capcov-fork
git -C ../capcov-fork checkout --detach 23f7841ca7369bf793c633e682fb00c77afc644c
```

The public Capcov revision contains the same two outbox fixture blobs as the original local research revision. The reference-library tool keeps the original revision label to reproduce the frozen packet digest.

## Replay the first AcceptedJob experiment

Run `python3 experiments/accepted_job/replay.py --source` to see the generated submit functions and compare them with the interpreter. Each run starts a fresh SQLite database, submits digest A, discards the reply, closes the connection before or after commit, reopens it and retries with A or B. Each output row shows the durable jobs at restart, the retry response and the durable jobs after retry. One tenant and one key are implicit. Recovery has one fault-free submit. Closing the connection models transaction loss at restart. This experiment does not exercise power loss.

On macOS ARM64 with Python 3.9.6 and SQLite 3.54.0, all twelve interpreter/generated comparisons agreed. The independent contract rejected two histories. Reapplying a committed request returned the original receipt while appending a second job. Changing the conflict branch returned the wrong response for digest B. The caller can see the latter failure directly. Detecting the duplicate required reading both durable rows.

Run `sh experiments/accepted_job/fenrir_control.sh ../fenrir` to reproduce the independent control. It extracts a clean archive of da55d436f40bc15aedd16eed2305f4694d2bc6e7 and selects both Shen/JIT 0.7 and Maude 3.5.1 from that revision's Nix flake. Gleipnir and Maude agreed on 2,928 model histories and 8,460 retry-mutant histories. The retained replay first failed on step three, the retry. Filtering the actual Shen histories to the submit/crash/submit prefix gave 96 terminal schedules with one durable job in the model and 216 with two jobs in the mutant. The mapping preserves every job identity through later acknowledgments and completions. The SQLite control adds a harder observation case where even the receipt is unchanged. Fenrir reports qualification UNKNOWN.

## Replay the Capcov transfer experiment

Run `nix develop .#research -c python3 experiments/outbox/replay.py ../capcov-fork`. The adapter reads both fixture blobs directly from Capcov commit c519f431 and uses the installed Capcov evaluator. Starting with one pending job, it explores an unknown send followed by a committed retry. The legacy fixture permits one modeled publication on each call. The intended fixture permits publication on either call and suppresses the second publication when the first committed.

A second run separates the worker and provider into two SQLite databases. The provider commits an effect, the worker loses its reply, and the restarted worker retries before marking the job. With no provider deduplication this produced two committed provider effects. With a stable job identity and a ten-tick receipt lifetime it produced one effect at tick nine and two at tick ten, the first expired tick. These are local simulated-provider rows. Capcov fixture emissions alone cannot establish a remote effect count. A transfer that promises one external effect must supply the provider's identity, retention and atomic effect-plus-receipt assumptions.

## Replay overlapping submissions

Run `python3 experiments/accepted_job/overlap.py`. Two connections submit to one SQLite file. The first reserves the writer slot. The second either retries its transaction after the first commits or reads before commit and uses that stale decision later. The schema deliberately permits duplicate rows so the experiment isolates the handler's serialization obligation. Each run checks the caller receipts against the stored tenant, key and digest.

The tenant-scoped, serialized handler admitted identical requests, conflicting payloads and matching keys in distinct tenants, with either client first. The split lookup created two rows in the identical-request case and accepted both conflicting payloads. A globally keyed lookup passed both single-tenant suites but gave the second tenant the first tenant's receipt. These are scheduled two-client interleavings with one lock retry. The handwritten schedules exercise lock placement and include two broken handlers. The same runner also specializes each submit program against a tenant-and-key schema and invokes it inside the contended transaction. Across 24 paired executions, interpreter and residual agreed on every receipt and durable row. The independent contract admitted the correct program for identical requests, conflicting payloads, separate tenants and separate keys. It rejected the duplicate and wrong-conflict variants in their targeted schedules. The harness supplies the writer transaction and retries acquisition once.

## Cost of specializing the submit program

Run `python3 experiments/accepted_job/benchmark.py` on the host to generate a fresh timing report. The [recorded ARM64 run](experiments/accepted_job/timings.json) timed generation 101 times, then compared the interpreter, its 376-byte residual function and a handwritten submit on the same four histories. Each history contains two submits and a lost first reply. Before timing batches, the harness checks receipts and durable rows against the contract and the handwritten SQLite control. Method order varies across 21 paired rounds.

| Median time per two-submit history | Interpreter | Generated | Handwritten |
|---|---:|---:|---:|
| Injected in-memory storage | 1.29 µs | 1.48 µs | 1.15 µs |
| Fresh file-backed SQLite database, including setup, close and reopen | 6.13 ms | 7.65 ms | 6.73 ms |

Generation took 33.1 µs at the median. Neither workload had positive median paired savings. The middle half of the paired differences crossed zero for both, so this run establishes no repeatable speed advantage. The generated create branch executes an extra statement to retrieve the row ID. The interpreter and direct code take it from the insert cursor. That known difference makes handler-call count a useful control for the next generator revision. The report includes quartiles and first-history timings; the latter measure first calls within a running Python process, with OS caches uncontrolled.

The [matched-call rerun](experiments/accepted_job/timings-matched.json) uses the insert cursor in all three implementations and repeats all twelve conformance checks before measurement via the separate replay command. The generated function shrank to 351 bytes. With injected storage the median paired saving was 185 ns per history (middle half 15–308 ns), covering the 30.2 µs generation cost after an estimated 163 histories. With SQLite the median paired saving was 0.11 ms but the middle half spanned −3.30 to +2.69 ms. This sample supports trying specialization on repeated in-memory exploration. It gives no dependable break-even for the file-backed worker. These estimates cover generation time only.

## Configurable cancellation

Run `python3 experiments/accepted_job/cancellation.py`. Each candidate uses the generated v1 submit, then commits a cancellation, restarts on the same SQLite file, retries the original submission and gives the worker one delivery opportunity. Both candidates acknowledge cancellation and return the original submit receipt on retry.

| Proposed meaning of cancel | Durable job at restart | Deliveries after one worker step |
|---|---|---|
| Stop waiting for the result | Pending | One |
| Withdraw a pending obligation | Withdrawn; receipt retained | Zero |

Each candidate contract rejects the other strategy's committed history. If the worker restarts before the cancellation commits, both models admit one delivery. A single local transaction records the simulated delivery and terminal state. This experiment covers cancellation before delivery begins. The combined native specification binds this choice when compiled. Its default withdraws pending work.

## What a monitor can decide

Run `python3 experiments/visibility.py` to replay each pair of histories and group them by the information visible to the observer. The classifier retains the names of the admitted and violating witnesses. An observation shared by both has verdict `unknown`. A positive verdict is scoped to the enumerated corpus.

The AcceptedJob client sees receipt 1 from both the correct worker and the duplicate-job mutant. Reading the durable rows distinguishes them. In the split-provider experiment the worker makes the same two attempts for job j at ticks 0 and 9. Provider retention of ten ticks yields one effect; retention of nine ticks yields two. Counting attempts cannot decide whether the logical effect happened twice. Counting provider commits can decide this pair. Both experiments use the same classifier with separate contract checks and observation mappings.

## Held-out recovery policy pilot

The [trial protocol](experiments/learning/PROTOCOL.md) and [grader](experiments/learning/trial.py) are frozen by SHA-256 before recruiting participants. The two tasks expose 18 controller inputs over 24 admissible worlds. A feasibility check found a safe, progress-preserving choice at every input. Always retry, always settle and always hold each failed a different obligation. These are evaluator controls. Nine independent participant sessions completed the pilot. The results below include each submission and retry.

The [shared training packet](experiments/learning/EXAMPLES.md) contains the same concrete histories for all three arms. The operator runs `python3 experiments/learning/feedback.py first.json [second.json [third.json]]` with one participant's submissions in order and returns its single JSON result. The full grader output stays with the evaluator. The wrapper detects recurrence of a disclosed unsafe action at the same public input, rejects submissions after a terminal result and enforces the three-submission limit. Semantic recurrence across different inputs requires evaluator review. Time and token budgets require independent usage records.

## Direct monitor comparison

Run `python3 experiments/transfer.py`. For nine worker/provider executions, a four-line adapter to the existing classifier and a sixteen-line direct outbox monitor returned identical verdicts and witness lists. Counts include each function definition and exclude shared replay, test harness and pre-existing classifier code. Both paths needed the same domain decisions: use the stable job identity, count committed provider effects and choose which records the observer can inspect. Reuse removes the repeated grouping and verdict logic. This single-author comparison measures incremental code size. Independent developer time and decision counts remain unmeasured.

At tick 11 every originally tested provider had lost its receipt, so the worker-only verdict within that corpus was violated. Adding a provider with twelve-tick retention yielded the same two attempts and one effect. Both monitors revised that verdict to unknown. A deployment monitor using the earlier classification would need evidence limiting the provider environments to the earlier corpus.

## Specialized prefix monitor

Run `python3 experiments/accepted_job/monitor.py`. The contract enumerates legal histories for one key and job IDs 1 or 2. Specialization compiles these histories into a prefix transition table. The generated checker reports a partial observation as possible while some legal completion remains. It can reject a prefix before the final storage read, for example when the reply contradicts the observed committed row. The replay compares the table with a direct trace-language interpreter at every tested prefix and with the independent AcceptedJob checker on complete traces. These checks assume ordered, instrumented storage observations. The receipt-only observer has a different evidence boundary.

## Bounded explorer

Run `python3 experiments/accepted_job/explorer.py`. The generic explorer traverses enabled choices. The specialized explorer fixes the program and enumerates its schedule table once, then runs the residual submit against fresh SQLite stores. Delivery of the first response ends the history. Loss causes a restart and exactly one retry, with either digest. A delivered rollback reports an abort. The harness compares the entire schedule-to-observation mapping and separately checks the six expected schedules for each program. The duplicate mutant violates only commit → lose reply and restart → retry A. This exploration profile couples reply loss and restart; it covers no independent network retries or concurrent crashes.

The pilot's reference-library arm receives two pre-existing semantic definitions from the training projects: Fenrir's guarded storage contract and Capcov's intended publication model. Run `python3 experiments/learning/reference_library.py ../fenrir ../capcov-fork` to emit the digest-checked packet. These are source references for the participant to read; the trial runner does not execute them. The evaluator selected the two files after seeing the task, so baseline selection bias remains possible. No held-out solution was added to their contents. Repository provenance establishes that the definitions predate the pilot; individual human authorship has not been verified.

The [cold-start run](experiments/accepted_job/timings-cold.json) measures a fresh Python process, imports, one validated commit/lost-reply/retry history and exit. The residual arm also generates the function in that process. Run `python3 -B experiments/accepted_job/cold.py` to reproduce the design. Each backend has eleven randomized paired rounds and retains every wall-clock measurement. OS filesystem caches are uncontrolled. These measurements apply to one-shot invocation; the warm break-even estimate assumes reuse within a process.

### Typed payloads and receipts

[receipt.json](native/receipt.json) defines a two-member Digest enum, a pending state carrying that digest, a submit event carrying an input digest and a receipt carrying the stored digest. Rules can compare the stored and input fields. For finite enums the compiler expands constructors and guards at build time. The wire spelling of a unary constructor is its name and member joined by a hyphen.

```sh
python3 native/spec.py native/receipt.json --compile /tmp/receipt
/tmp/receipt receipts.sqlite 42 submit-a
/tmp/receipt receipts.sqlite 42 submit-b
```

On a fresh database the first command to the binary reports `pending-a receipt-a`. The second reports `pending-a conflict`. Each invocation reads the stored state for key 42. A receipt here carries the digest; the instance identity is supplied separately. Finite expansion bounds the field domain and increases generated code size with the number of members.

For runtime payload digests, use [sha256-receipt.json](native/sha256-receipt.json). Its Digest declaration is `"sha256"`. Supply a canonical 64-character lowercase hex digest after `submit-`. The executable validates the input, compares it with the stored field and returns the actual digest on an accepted or replayed submission. The client computes the digest. This profile supports equality and copying between unary fields. Its transition count depends on constructor and rule counts, independently of the number of possible SHA-256 values.

## Recovery pilot results

Three fresh sessions per arm, all gpt-5.6-luna at medium effort, graded against the frozen tasks. The independent [builder output](experiments/learning/inferred-library.md) was frozen before participants received the tasks. Each final policy passed 24 worlds across both tasks. Counts of format repairs by repetition were 1/1/2 with examples, 1/1/1 with the reference library and 1/0/1 with the inferred library. No participant needed semantic counterexample feedback. The feedback on malformed submissions disclosed the required JSON encoding. No human intervened during the trials.

[Per-run usage, elapsed time and submission filenames](experiments/learning/runs/results.json) include time waiting for evaluator feedback. Elapsed differences therefore have limited value for comparing construction efficiency. All sessions received the same declared budgets. The builder took about 28 seconds and 1,220 output tokens. Correctness differences between arms were zero in every repetition. The examples-only arm already achieved full task success. This pilot provides no evidence that the compiler needs a learner.

## Ordinary interpreter specialization

An unstaged Shen stack interpreter and a separate Clang control test whether fixed bytecode disappears without writing the interpreter over residual expressions. The experiment includes the source, derived sum IR, mutation checks and replay commands. [Procedure and limits](experiments/ordinary_vm/README.md)
