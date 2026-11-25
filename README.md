# Semantic co-design

A developer writes the behavior of a node as ordinary Shen: one pure function from a state and an event to a new state and a list of effects. The tool in [tool/](tool/README.md) then does four things with that one text.

1. It specializes the Shen to a native step function.
2. It runs that native code under a deterministic simulator that owns the clock, the network, the disks, crashes and the schedule.
3. It evaluates invariants, also written in Shen, over the simulated traces, and compares every step the native code took with the same Shen source run by shen-jit.
4. It translates the same Shen to Maude and searches the same environment exhaustively within small bounds.

Everything is run inside the locked shell, from the repository root: `nix develop -c <command>`.

## What the repository establishes, and the command behind each claim

Every row is a command that was run on this branch and passed. Times are wall-clock on one Apple arm64 Mac with two or three of the commands running side by side: on commit `5b0b349`, the merge of the last of the three targets, except the Solo5 rows (`99ac8ef`, which added a check to them), the flake gates (`0f02021`) and the two cluster rows (`af38ebd`, with nothing else running). The commits after `5b0b349` change documentation and `tool/solo5/` only. A pass means what the linked README says and no more: all of the evidence is differential testing, seeded simulation and bounded search. Nothing here is a proof.

| Command | What a pass establishes | Time |
|---|---|---|
| `python3 -m tool check kernel` | The compiler on its own rows. Native code equals shen-jit on 9,600 state and event rows, 1,780 operator rows to the edge of the integer range, 3,200 rows of a residual loop and 7,140 rows of 119 seeded random programs, traps included. The Maude translation equals shen-jit on 762 calls. Seven sources outside the supported fragment are refused. Retained mutants: swapped clauses, an undeclared symbol, two broken compilers, one broken translation. [tool/README.md](tool/README.md#6-what-the-retained-checks-establish) | 88 s for these four together |
| `python3 -m tool check sim` | The simulator's own rules on a probe node: runs are deterministic and replay; a step changes the world by exactly its applied effects; replay rejects edited traces; Maude and the simulator reach the same observations at two bounds. Retained mutants: eleven broken simulators and four edited Maude models, each told apart from the real one. | |
| `python3 -m tool check accepted_job` | One node that accepts a job only after its disk write is acknowledged. Five Shen invariants hold on 500 seeded runs with crashes, drops and duplicates; four required histories occur; three bounded searches agree between Maude and the simulator and are all safe. Eight one-line mutants are each rejected. [README](tool/examples/accepted_job/README.md) | |
| `python3 -m tool check gossip` | Three instances of one compiled step with sends, timers and partitions. Two mutants. | |
| `python3 -m tool check workflow` | A durable workflow engine written as an interpreter in ordinary Shen and specialized to one workflow definition at a time. Nine state invariants and seven run invariants hold on 800 seeded runs with faults; the state invariants also hold in six bounded searches, in Maude and in the simulator; the words of the definition language do not survive into the residual program. Twenty-one mutants are each rejected. [README](targets/workflow/README.md) | 314 s |
| `python3 -m tool check raft` | One Raft server as a Shen step function, compiled whole, in clusters of 3 and 5. Fourteen Shen invariants (the five properties of Figure 3 of the Raft paper among them) and linearizability hold on 330 seeded runs under five fault profiles; five bounded searches agree between Maude and the simulator. [README](targets/raft/README.md) | 329 s |
| `python3 -m targets.raft.check mutants` | The teeth of the Raft check: 22 mutants are each rejected on a retained seed, every invariant rejects at least one, and Figure 8 and the lost vote each break a published property on a seed of a profile written to produce them. | 252 s |
| `python3 -m tool check solo5-host` | An effect shim in C, written against the Solo5 API and run here as a host process: 19 scripted runs with kills are each shown to be a run of the simulator, and 21 broken drivers or specifications are each rejected. It also reads each generated `node.c` against the header a unikernel is compiled with. [README](tool/solo5/README.md) | part of the next row; 92 s as a flake gate |
| `python3 -m tool check solo5` | The same, and the same scenarios on real Solo5: Solo5 v0.9.3 and 27 unikernels built by a Nix derivation and run under `solo5-spt` in a Linux container (Linux 7.2 aarch64 under OrbStack, started through `docker`). The unikernel, the macOS host process and the Linux host process reconstruct to byte-identical simulator traces for the 15 scenarios without a clock. | 238 s |
| `python3 -m tool check solo5-cluster-host` | Three Raft servers as three host processes of that shim, each on its own link, with the test harness as the only network between them: it forwards, delays, drops, doubles, misdelivers and cuts frames from a seed, and kills and restarts nodes. 35 runs of seven scenarios (a leader elected and serving, the leader killed, a node restarted from its disk, a kill inside a step, a partition that heals, lossy links, all of it together) are each rebuilt as one simulator trace on which Raft's fourteen invariants and linearizability hold. Four broken drivers and two of three edits of Raft are rejected; the third, `local-get`, is NOT found. Timers run on the machine's clock: a run is recorded and checked, not reproducible from its seed. [README](tool/solo5/README.md#a-cluster) | part of the next row; 245 s as a flake gate built alone |
| `python3 -m tool check solo5-cluster` | The same, and the same runs on real Solo5: eight unikernels, three `solo5-spt` tenders on three tap interfaces with no bridge, in the Linux container. Same verdicts. `solo5-hvt` is tried once and is blocked: no `/dev/kvm`. | 772 s |
| `python3 -m experiments.branching_vm.check` | The partial evaluator in `tool/pe.py` still specializes an ordinary Shen stack-machine interpreter with jumps to four residual programs with the same block counts and answers. [README](experiments/branching_vm/README.md) | 3 s |
| `nix shell --inputs-from . 'nixpkgs#tlaplus' -c python3 -m targets.raft.tla.check` | TLC on the published `raft.tla` at one small bound (4,054,030 distinct states): none of the five Figure 3 properties is violated. Four of them can fail at that bound and each rejects an edit of the file there; Leader Completeness cannot fail there and rejects an edit at a larger bound. The Figure 8 edit is not rejected within the bound. Not part of `nix flake check`. | 372 s |
| `nix flake check` | Nine gates: `sim-tool` (the first four rows), `workflow`, `raft`, `raft-mutants`, `solo5-host`, `solo5-cluster-host`, and three that predate the tool: `staged-examples` (which runs the branching VM check), `native-replay`, `generated-applications`. | 121, 338, 346, 284, 92, 29, 12 and 76 s, built as `nix build .#checks.aarch64-darwin.<name>` in two batches; `solo5-cluster-host` 245 s, built alone. All nine together, twice on `0b38a87`: once all passed in 13 min 41 s (`solo5-cluster-host` 346 s). Once `solo5-host` failed after 2 minutes: its driver mutant that spins instead of waiting (`timer~wrapping-deadline`) is rejected by the processor time it uses, and that run was accepted. The likely reason, not measured, is that beside eight other gates it was given too little processor time to cross the limit. Whether this happens without the ninth gate was not tried |

On this machine `nix flake check` had nothing to fetch or compile beyond the eight checks themselves: every input was already in the store or the binary cache. The gates were built one name at a time so that no single command ran for ten minutes.

## What it does not establish

* **No proof.** Nothing shows that specialization preserves meaning. Native code is compared with shen-jit on the rows, seeds and `(state, event)` pairs listed in each README. A Shen form that no retained row uses is untested.
* **Two hand-written models of one environment.** The simulator and the Maude environment agree on observation sets at the bounds each check names. Timed behavior, partitions and larger bounds are not compared.
* **Seeds and bounds are the coverage.** Raft's Figure 8 and lost-vote histories are not found by the general fault profiles in hundreds of seeds; they are found under profiles written to produce them. Liveness is a measurement on seeds.
* **Solo5 is 19 scripted runs of one node and 35 of a Raft cluster, under one tender.** Each of those runs is shown to be a simulator run; that every run of the shim is one is an argument. `solo5-hvt` was not run (the container has no `/dev/kvm`), and virtio, muen and xen were not built. A block write that has returned survives the death of the unikernel and of the tender. It does not survive loss of power on the host: the Solo5 block API has no flush. The nodes that run on Solo5 are those of `tool/solo5/scenarios.py` (AcceptedJob, the simulator's probe, a timer node, a store node) and three Raft servers as a cluster whose only network is the test harness; what that cluster shows and does not is in [tool/solo5/README.md](tool/solo5/README.md#what-the-cluster-runs-do-not-establish). The workflow engine does not run on Solo5.
* **One node per world, small declared capacities.** A Raft log holds 8 entries and there is no compaction. The workflow engine runs one activity at a time.
* **The environment rules are assumptions.** A specification may rely on whole rows, in-order syncs, a crash keeping a prefix of pending writes, and no completion crossing a crash ([tool/README.md](tool/README.md#environment-rules)). AcceptedJob and Raft depend on the last one and have no defence of their own.
* **Each target's own list.** [Raft](targets/raft/README.md#10-what-is-established-and-what-is-not), [workflow](targets/workflow/README.md#what-is-not-established), [Solo5](tool/solo5/README.md#what-carries-over-from-simulation-and-what-does-not), [the compiler and simulator](tool/README.md#compiler-limits).

## Three ways to cheat, and where each part of the repository stands

A result here only counts if it avoids three shortcuts.

1. **Tabulation.** Running every input through the interpreter and emitting the table of answers is not specialization.
2. **Hand staging.** A specification that returns code trees is a compiler written by hand, once per example.
3. **Semantics in the shim.** Guarantees must come from the Shen specification. Hand-written C or Python may only perform dumb effects.

| Part | Standing |
|---|---|
| [tool/](tool/README.md), [targets/](targets/), [experiments/branching_vm](experiments/branching_vm/README.md), [experiments/ordinary_vm](experiments/ordinary_vm/README.md) | Ordinary Shen, specialized by a partial evaluator that knows nothing about the program. The specifications compute values and never mention residual code. The workflow check and the Solo5 check each carry teeth against cheats 1 to 3 for their own target. |
| [logic/](logic/build.py): AcceptedJob in Shen and Maude | **Tabulation.** `logic/build.py` asks the interpreter for all 28 pairs of 7 states and 4 events and emits the answers as a chain of string comparisons. It is not specialization, and earlier versions of this file and of [GOAL.md](GOAL.md) were wrong to call it that. `python3 logic/check.py` does show that the Shen and Maude definitions agree on those 28 pairs under both cancellation policies and that the emitted table follows an edit of either source. |
| [native/](native/spec.py): the JSON job specifications | A rule table written in JSON and emitted as branches. The SQLite read, the transaction and the commit are in hand-written [native/store.c](native/store.c), so the durability of an accepted job comes from the shim, not from the specification. [tool/examples/accepted_job](tool/examples/accepted_job/README.md) is the version with the commit boundary inside the Shen. |
| [examples/](examples/): VM, decoder, railway, queries | **Hand staging.** Each `spec.shen` and `spec.maude` builds the residual program itself, as a tree of `seq`, `set`, `while` and `emit`. They exercise the shared C backend. They are not evidence that an interpreter was specialized. |
| [example/](example/README.md): three applications in App Shen | A separate line of work that reached this branch with the commits of `main` up to `54e4d74`. App Shen is its own dialect with a Python reference evaluator, and Python hosts connect the generated code to databases and sockets. It does not use `tool/` and was not examined against the three cheats here. Its gate is `checks.<system>.generated-applications`. |

## Try it

```sh
git clone https://github.com/pyrex41/semantic-co-design.git
cd semantic-co-design
nix develop
python3 -m tool check accepted_job                 # the first application, about a minute
python3 -m tool compile accepted_job /tmp/out      # IR, C, shared library, executable
python3 -m tool compile raft /tmp/raft
python3 -m targets.raft.check story commit-by-counting churn paper   # Figure 8 as a seed found it
```

To write a new application, read [tool/README.md](tool/README.md): the specification convention, the manifest, what the compiler accepts and what the simulator provides.

## Earlier work kept in the tree

**Hand-staged examples.** `python3 -m specializer build vm --source shen --static examples/vm/sum.json -o /tmp/sum` builds a native program that sums its input. The four examples below share one IR and one C backend ([interface](specializer/FORMAT.md)). In each, the author's Shen or Maude constructs the residual program directly, which is cheat 2 above.

| Example | Fixed when built | Dynamic input | Replay |
|---|---|---|---|
| [VM](examples/vm/README.md) | Program and opcode dispatch | Array length and 32-bit values | `python3 -m examples.vm.check` |
| [Decoder](examples/decoder/README.md) | Byte order, accepted kinds, length and checksum rules | Bytes and chunk boundaries | `python3 -m examples.decoder.check` |
| [Railway](examples/railway/README.md) | Two-route layout | Serialized route events | `python3 -m examples.railway.check` |
| [Queries](examples/query/README.md) | Schema offsets and query tree | Rows, table sizes and cutoff | `python3 -m examples.query.check` |

The [compiled benchmark](benchmarks/README.md) times these four generated programs against Shen JIT, Maude compiled through C++ and handwritten C.

**A durable job from a table.** `python3 logic/build.py logic/accepted-job.shen --compile /tmp/job` and the same with `logic/accepted-job.maude` build a binary that loads one job from SQLite, applies one of 28 tabulated transitions and commits. `--policy keep` selects the other cancellation policy. See the table above for what this is and is not.

**Three applications in App Shen.** [Document Desk](example/webapp/README.md), a [spot trading engine](example/trading-engine/README.md) and a [router and load balancer](example/router-nlb/README.md). [Application guide](example/README.md) · [Language contract](example/LANGUAGE.md)

## Project documents

- [Goal and evidence](GOAL.md): the goal, what is established, and what remains open.
- [tool/README.md](tool/README.md): the specification convention, the compiler, the simulator, the checkers and their limits.
- [Research plan](RESEARCH.md) and [experiments](EXPERIMENTS.md): the earlier cycle on retry witnesses, Fenrir and Capcov controls and the learning pilot.
