# Raft on the kernel

One Raft server written as an ordinary Shen step function ([spec.shen](spec.shen)), compiled to native code by `tool/`, run in 3-server and 5-server clusters under the deterministic simulator, judged by invariants written in Shen ([invariants.shen](invariants.shen)) and by an independent linearizability checker ([lin.py](lin.py)), searched exhaustively in small bounds by Maude, and set beside TLC on the published `raft.tla`.

```sh
nix develop -c python3 -m tool check raft                    # the retained check, 5.5 to 7 minutes here
nix develop -c python3 -m tool compile raft /tmp/raft        # IR, C, library, executable
nix develop -c nix shell --inputs-from . 'nixpkgs#tlaplus' -c python3 -m targets.raft.tla.check
nix develop -c python3 -m targets.raft.check story local-get three     # a retained failing trace, readable
nix develop -c python3 -m targets.raft.check hunt <mutant> [three|five] [--from=N] [--seeds=M] [--only=CHECK]
```

The numbers below are the output of these commands on the commit that added this file.

## 1. The node

Figure 2 of Ongaro and Ousterhout, "In Search of an Understandable Consensus Algorithm (Extended Version)": leader election, log replication, commit index, with a key-value state machine (two keys, `put` and `get`) on top. It is one pure function `raft.step : State Event -> [State' Effects]` and one initial value `raft.init`; the file computes values and never mentions residual code, the simulator or integers standing for symbols. shen-jit runs it unchanged and is the reference for the native code.

| | |
|---|---|
| Persistent state | `currentTerm`, `votedFor`, the log. On disk as row 0 `[Term meta Voted Len 0]` and one row per index `[Term Op Key Val Req]`. |
| Write order | Every prefix of the writes of a step is a state the server was allowed to be in: a new entry is written before the length that includes it, and a conflicting suffix is cut (the shorter length is written) before the entry that replaces it. A crash keeps a prefix of the pending writes. |
| Before replying | A reply to RequestVote or AppendEntries leaves only when every write issued before it is acknowledged. Until then it waits in one slot (`Hold`); a newer reply replaces a waiting one, which is a lost message. A candidate asks for votes only after its own vote is durable; a leader counts itself towards a majority only up to its durable length. |
| Recovery | After a restart the server reads row 0, then one row per entry, and takes no part in the protocol until that is done. |
| Clients | Only a leader takes a request, and every request becomes a log entry of its term. A `get` goes through the log like a `put`, so its answer is confirmed by a majority. Requests carry a number; a retry is appended again and the state machine executes the first copy only (section 8). A leader answers when it applies the entry. |
| Messages | One entry per AppendEntries. One committed entry is applied per step. |
| Timers | Heartbeat 50. Election timeout `150 + (41 * Id + 59 * Term) mod 200`: a fixed function of server and term, because the environment offers a node no randomness. The simulator skews it (section 3). |
| Declared bounds | At most 5 servers. Log capacity 8 entries; a leader whose log is full answers `full`. |

Not there, by decision: membership change, snapshots and log compaction, a no-op entry at the start of a term (an entry of an earlier term commits when a later entry of the current term does, which a client's retry supplies), read leases or ReadIndex, batching.

What the specification relies on from its environment is listed at the top of `spec.shen` and is exactly the environment rules of [tool/README.md](../../tool/README.md): whole rows, syncs in issue order, a crash keeps a prefix of pending writes, no completion crosses a crash.

## 2. Compilation

The whole node is compiled. Nothing is interpreted at run time and nothing is hand-written in C.

`spec.shen` (260 lines without comments, 39 definitions) becomes 98,364 IR nodes, 4,464 slots, no residual loop blocks and 31,574 lines of C. A server's state is 73 integers. The library and the executable give the same initial state for every server of a 3-server and a 5-server cluster.

The compiler (`tool/pe.py`, `tool/compile.py`) was not changed for this target. What it took was writing the node inside the compiler's documented limits, all of which leave it ordinary Shen:

* Each handler is one `let` chain whose conditionals are in value position, so the two arms join leaf by leaf instead of duplicating the rest of the step. The only runtime decisions in tail position are the event kind, "still loading", and the tests of recovery.
* The log and the per-server arrays are lists of declared capacity. Access by a runtime index (`raft.entry`, `raft.store`, `raft.at`, `raft.set`) is recursion over the list, which the compiler unfolds into a chain of tests, linear in the capacity at every access. The accessors are total (an index outside the list gives a default), so no path of the residual code can trap on a bad index.
* Loops over servers and over log indexes (`raft.feeds`, `raft.asks`, `raft.advance`, `raft.replicas`) recurse on a static counter up to the capacity and compare with the runtime server count or length inside.
* A step's effect list has a length that depends on runtime conditions. The compiler keeps such a list as a split value and emits one return path per reachable shape. The handlers therefore choose among whole write lists with one conditional rather than appending many optional effects.

The cost of that is size, not capability: every log access is 8 comparisons and a 5-leaf join, and the residual program is two orders of magnitude larger than its source. One measurement on this machine, not a benchmark: a native step takes about 3 microseconds called through ctypes, and the same step in shen-jit about 140 microseconds including reading its arguments (2,320 recorded pairs).

Kernel changes made for this target, all additive and in commits whose message starts with `kernel:`:

* `tool/sim`: three optional profile keys. `budget["oneway"]`: asymmetric cuts (everything into, or out of, one node is discarded until `heal`). `delay["timer"]` and `timer_rate`: a timer runs for its delay scaled by a drawn skew and by the node's clock rate. `budget["timer"]`: a bound on timer events for untimed searches.
* `tool/maude.py`: the same timer budget in the Maude environment.
* `tool/sim_options_check.py`, run by `tool check sim`: checks of those keys on the gossip node, with a simulator or Maude model that ignores each key as the mutant.
* `tool/__main__.py`: `tool check <name>` also finds `targets/<name>`.

## 3. Simulation

`PROFILES` in [check.py](check.py): seeded, timed schedules with budgets of 3 crashes, 4 drops, 2 duplicates, 2 symmetric partitions and 2 one-way cuts per run, message delays 1 to 20 and disk delays 1 to 5 (reordering comes from the delays), timers running 80% to 120% of what the node asked for on clocks at 90% to 115% of nominal rate, and a mid-step crash that cuts a step between two of its effects. A crash loses the node's memory and a suffix of its pending writes. The clients ([workload.py](workload.py)) hold five operations on two keys, keep two open at a time, and retry an unanswered one after 200 time units; they send to whichever servers currently believe they lead, stale leaders included.

| | `three` | `five` |
|---|---|---|
| Servers, seeds, steps | 3, 150, 150,000 | 5, 60, 84,000 |
| Crashes between steps, crashes in the middle of a step, restarts | 79, 371, 450 | 20, 160, 180 |
| Drops, duplicates | 582, 289 | 240, 120 |
| Partitions, one-way cuts, heals | 299, 300, 592 | 120, 120, 240 |
| Messages lost to a server that was down | 1,461 | 404 |
| Issued writes that never reached the disk | 64 | 41 |
| AppendEntries handled after a later one from the same leader (reordering or a late duplicate) | 141 | 119 |
| Runs with: a leader change, a conflicting suffix cut, a log recovered from disk, an entry committed on 3 servers, a `full` answer, a retried put skipped | 150, 19, 124, 144, 2, 54 | 59, 6, 39, 57, 1, 20 |
| Runs by operations answered, 0 to 5 of 5 | 3, 5, 5, 4, 9, 124 | 3, 1, 0, 1, 5, 50 |
| Traces replayed and re-run from their seed | 15 | 6 |

The seven invariants of section 4 hold on all 210 traces and all 210 client histories are linearizable. The native step equals shen-jit on the 46,058 distinct `(state, event)` pairs of these runs.

Every run is checked as it is produced: it must end at its step bound with no trap; every step changed the disk queue and the outputs in flight by exactly its applied effects (`check.effects_applied`); the invariants and the linearizability check below hold; every tenth trace is replayed step by step from its recorded actions and draws and re-run from its seed, byte for byte. Every distinct `(state, event)` pair the native step was run on is then evaluated by shen-jit on `spec.shen` and compared.

## 4. Invariants

Written in Shen, evaluated by shen-jit over every trace, at every step at which a server state or a disk can change. They share no code with the node. A server that is still reading its disk back counts as down for the log properties.

| Property (Figure 3 of the paper) | Shen, over a whole trace | Per reachable world, bounded search | TLA+ on `raft.tla` |
|---|---|---|---|
| Election Safety | `inv.election-safety`: no two servers ever lead the same term, across the whole history | no two leaders of one term in one world | `ElectionSafety`, over the `elections` history variable |
| Leader Append-Only | `inv.leader-append-only`: between consecutive worlds in which a server leads the same term, its old log is a prefix of its new one | not checked | `LeaderAppendOnly`, an action property |
| Log Matching | `inv.log-matching`: every pair of running servers in every world | same | `LogMatching` |
| Leader Completeness | `inv.leader-completeness`: every entry some server has had at or below its commit index is in the log of every leader of that server's term then, or a later one | not checked | `LeaderCompleteness`, over current terms |
| State Machine Safety | `inv.state-machine-safety`: all servers, over the whole history, apply the same entry at each index; and each server's store is what its applied entries produce, each request once | committed prefixes of running servers agree | `StateMachineSafety` |
| Figure 2, "updated on stable storage before responding to RPCs" | `inv.replies-are-durable`: a granted vote is one that was on the voter's disk; a successful AppendEntries reply for index n needs n durable entries; no reply names a term the disk does not have | a leader has a majority of durable votes; an answered put is durable on a majority of disks | not modelled: `raft.tla` has no disk |
| Section 5.4.2, the commit rule | `inv.commit-rule`: a leader advances its commit index only to an entry of its own term | not checked | not stated |

`inv.commit-rule` is not one of the five properties. It states the rule that Figure 8 motivates at the place it is applied, and it is kept apart in the mutant results below for that reason.

Linearizability of the client-visible history is checked by `lin.py`, a Wing and Gong search over the operations of a trace: invocation is the step at which the request was first handed to the network, response is the step at which an `ok` reply first reached the outside. An operation with no reply is open (a put may have taken effect or not). The checker knows nothing of Raft. Its own teeth are 12 hand-written histories, 6 of them not linearizable (stale read, lost update, a value never written, a read before the write began, a put applied twice, two reads that go backwards).

## 5. Mutants

Each mutant is one textual edit of `spec.shen` (`MUTANTS` in `check.py`), compiled like the specification. `hunt` runs seeds 0, 1, 2, ... of a profile until a check fails, shrinks the failing trace by delta debugging on the schedule ([shrink.py](shrink.py)), and retains seed, check, digests and the short schedule in [mutants.json](mutants.json). `tool check raft` rebuilds every mutant, re-runs its seed, replays its short trace, requires the named check to reject both, and requires the specification to pass on the same seed and, where it is a valid schedule for it, on the same short schedule. "Seeds" is the number of seeds tried up to and including the first failure.

| Mutant | The edit | `three`: first failing seed, seeds, check, steps after shrinking | `five` |
|---|---|---|---|
| `vote-without-log-check` (required, 1) | grants a vote without the up-to-date check of 5.4.1 | seed 0, 1 seed, Leader Completeness, 153 steps | seed 7, 8 seeds, Leader Completeness, 136 steps |
| `commit-by-counting` (required, 2: Figure 8) | commits an entry of an earlier term by counting replicas | **not found in 800 seeds** by any published property or by linearizability | **not found in 200 seeds** |
| `vote-before-durable` (required, 3) | answers a vote request before `votedFor` is durable | seed 0, 1 seed, `inv.replies-are-durable`, 9 steps. Election Safety itself: **not violated in 400 seeds** | seed 0, 1 seed, `inv.replies-are-durable`, 11 steps |
| `local-get` (required, 4) | a leader answers a get from its own state without confirming leadership | seed 28, 29 seeds, linearizability, 99 steps | seed 46, 47 seeds, linearizability, 121 steps |
| `votes-twice` | grants a vote in a term in which it voted for another server | seed 2, 3 seeds, Election Safety, 181 steps | not run |
| `ack-before-durable` | answers AppendEntries before the entry is durable | seed 0, 1 seed, `inv.replies-are-durable`, 15 steps | not run |
| `commit-unchecked-suffix` | a follower commits up to its own last entry, not the last one the leader confirmed | seed 3, 4 seeds, Leader Completeness, 74 steps | not run |
| `put-twice` | executes every copy of a put that retries left in the log | seed 10, 11 seeds, State Machine Safety (the store is not what the applied entries produce), 86 steps | not run |

The shrunk traces are 9 to 181 steps, from 1,000 or 1,400. They are not minimal: the shrinker removes steps of a timed schedule, and a history that elects two leaders needs some tens of steps for each. `story` prints one. The retained `local-get` trace is not the textbook partitioned leader: a restarted server is elected, has not yet learned what is committed, and answers a get from an empty store after 12 was written and acknowledged, which is the other reason section 8 of the paper gives for confirming before answering.

**The consequence of `vote-before-durable` was not found by the seeded search either.** `inv.replies-are-durable` rejects it on the first vote of the first seed, but two leaders in one term did not occur in 400 seeds of `three`. A directed schedule ([figure8.py](figure8.py), `lost_vote`, 21 steps, written by hand) shows it: a server grants its vote, crashes before the vote reaches its disk, restarts and grants another candidate its vote in the same term. On the specification the first answer is never sent and one leader is elected; on the mutant `inv.election-safety` and `inv.replies-are-durable` fail.

**Figure 8 was not found by the seeded search.** The five properties, the durability invariant and the linearizability check accept `commit-by-counting` on seeds 0 to 799 of `three` and seeds 0 to 199 of `five`. The history needs two leaders in succession that each accept an entry and lose contact before replicating it, the first of them re-elected, and then deposed again before it repairs the second: three well-timed isolations in one run. What is retained instead, and labelled as such:

* `inv.commit-rule`, which states the rule directly, rejects the mutant on seed 0 of `three` and seed 2 of `five` (shrunk to 63 and 116 steps). That shows the mutant misbehaves on ordinary runs; it does not show a safety violation.
* A directed schedule ([figure8.py](figure8.py)), written by hand after Figure 8 and not found by any seed: 83 steps, three crashes, untimed. On the specification nothing is committed in it and every check passes. On the mutant `inv.leader-completeness`, `inv.state-machine-safety` and `inv.commit-rule` fail: server 0 commits and answers put 1, and server 2 then leads term 4 without it and overwrites it.

So the invariants do reject the Figure 8 outcome when the history occurs, and the random schedules of this simulator did not produce the history within the budget.

## 6. Bounded search

Untimed profiles: any enabled action may happen next, and a budget bounds the number of timer events. Maude searches the translated Shen in the Maude environment; `sim.explore` visits every schedule of the native step. The two reachable observation sets (server memories, durable rows, replies that got out) must be equal, no reachable step may be without a result, and every reachable world must satisfy the per-world predicates of the table above (`unsafe` in `check.py`, restated in Python).

| Bound | Maude states | Simulator states | Observations, equal on both sides | with a leader | with the put answered |
|---|---|---|---|---|---|
| 2 servers, 2 timer events, a put | 38,736 | 36,526 | 2,210 | 1,668 | 56 |
| 2 servers, 1 timer event, a put, a crash | 7,782 | 6,500 | 1,282 | 850 | 26 |
| 2 servers, 1 timer event, a put, a drop | 2,312 | 1,740 | 572 | 494 | 10 |
| 3 servers, 1 timer event, a duplicate | 6,103 | 5,870 | 233 | 126 | no client |
| 3 servers, 1 timer event, a crash | 9,692 | 8,352 | 1,340 | 540 | no client |

Teeth: for `vote-before-durable` on the second bound Maude and the simulator agree with each other on 2,446 observations, 748 of them unsafe (a server leads a term with one durable vote); for `ack-before-durable`, 1,922 observations, 100 unsafe (a put answered with its entry on one disk). The native step equals shen-jit on the 1,524 pairs the searches executed. The Maude translation of `spec.shen` reduces those pairs and 1,536 pairs sampled from the seeded runs to the native result, and the translation of `commit-by-counting` does not.

Larger bounds were tried and are out of reach for both searchers: 3 servers with one election and a put, and 3 servers with two timer events, each exceed 250,000 states; 2 servers with a put and a duplicate is 117,530 states in the simulator and was left out for time. None of the bounds reaches a second leader after a commit, so of the four required mutants only `vote-before-durable` is visible to the bounded search. `ack-before-durable` is the second mutant used for its teeth.

## 7. TLC on the published raft.tla

[tla/raft.tla](tla/raft.tla) is byte for byte the file at github.com/ongardie/raft.tla (Copyright 2014 Diego Ongaro, CC BY 4.0; the check verifies its SHA-256). It states no invariants. [tla/MCraft.tla](tla/MCraft.tla) adds a bound and the five properties of Figure 3 as stated in the table of section 4. TLC 2.19 comes from the locked nixpkgs binary cache (`nixpkgs#tlaplus` 1.7.4); it is not in the development shell, so this is a separate command and not part of `tool check raft`.

```
TLC, published raft.tla, 3 servers, 1 value, max term 2, log 1, messages 1: 28169044 states generated, 2379781 distinct, no violation of ElectionSafety, LogMatching, LeaderCompleteness, StateMachineSafety or LeaderAppendOnly (89s)
  mutant votes-twice: violates ElectionSafety (121061 distinct states, 5s)
  mutant leader-without-quorum: violates ElectionSafety (177 distinct states, 1s)
  mutant commit-by-counting: NOT rejected within this bound (2379781 distinct states, 96s)
```

TLC stops a mutant at its first violation with two workers, so those two state counts differ by a few hundred between runs (121,322 and 178 in a second run); the other figures are the same. The bound is small because `raft.tla` carries history variables and a message bag. With two messages in flight instead of one, TLC had 2.7 million distinct states after a minute and a growing queue; that run was stopped and is not reported as a result.

Comparison of the invariant sets: the five named properties are checked on both sides, as history properties over simulator traces on one and as state or action properties over all reachable states of the bound on the other. The Shen side adds durability before replies, the commit rule and linearizability of a client history, none of which `raft.tla` can state (it has no disk and no client replies). The TLA+ side covers every interleaving of its bound, which the seeded simulation does not. Nothing here relates `raft.tla` to `spec.shen`: they are two descriptions of one protocol by different authors, checked against the same named properties.

## 8. Liveness

At virtual time 1500 the client performs one action that ends all faults: budgets go to zero and the network heals. From then on it sends one more put to one server after another every 60 time units. Declared bound: the `ok` for that put reaches the outside within 1500 time units of the moment faults stop.

| | `three` | `five` |
|---|---|---|
| Seeds | 60 | 30 |
| Answered within the bound | 60 | 30 |
| Time from the end of faults to the answer: least, median, most | 38, 154, 632 | 31, 297, 503 |

Teeth, on 10 seeds of `three` each: a node that never releases a reply waiting for the disk (`held-reply-never-sent`) and a node that never grants a vote (`never-grants`) answer within the bound on none.

This is a measurement on these seeds, not a proof. The first version of the node failed it: a put appended in one term, whose leader was deposed and re-elected, was never committed, because retries of a put already in the log were not appended again and nothing of the new term ever entered the log. The node now appends every request it takes and the state machine skips repeats, as section 8 of the paper describes.

## 9. What is established, and what is not

Established, by `tool check raft` unless another command is named:

* The node compiles whole; the native step equals shen-jit on every `(state, event)` pair the simulations and the bounded searches reached, and on the initial state of every server; a library compiled from an edited specification is rejected by the same comparison.
* On the seeds and profiles of section 3, with every fault kind occurring, the seven invariants hold and every client history is linearizable.
* Each of the eight mutants is rejected on a retained seed, with a shrunk trace, except that the Figure 8 mutant is rejected by the commit rule and by a hand-written schedule only. On each retained seed and schedule the specification passes.
* In the bounds of section 6, Maude on the translated Shen and the simulator on the native code reach the same observations, all of them safe.
* After faults stop, the pending put is answered within the declared bound on every seed tried.
* By the TLC command: the published `raft.tla` satisfies the five properties in the bound of section 7.

Not established:

* **Figure 8 by search, and two leaders in one term from a lost vote.** Neither history was produced by the seeded schedules within the budgets of section 5. Both are shown by hand-written schedules only.
* **Anything beyond the seeds, bounds and pairs listed.** The evidence is differential and sampled. Nothing proves that the compiler preserves meaning, that the node is safe on schedules not run, or that the liveness bound holds in general.
* **Replay and determinism for every trace.** Every tenth trace is replayed and re-run; the others are produced once.
* **History properties in the bounded search.** Leader Append-Only and Leader Completeness are checked on simulator traces only. The per-world predicates of the bounded search are Python restatements, not the Shen invariants.
* **Agreement of Maude and the simulator beyond these bounds.** They are two hand-written models of one environment. Timed behaviour, partitions and one-way cuts are not in the Maude model.
* **A relation between `raft.tla` and this node.** See section 7. TLC's bound allows one round of elections (terms up to 2), one entry and one message in flight; it does not expose the Figure 8 edit either.
* **Randomized election timeouts.** The timeout is a fixed function of server and term plus the simulator's skew. A real deployment needs a source of randomness, which would be a new effect role.
* **Defence against a stale completion.** The node relies on the rule that no acknowledgement, read result or timer crosses a crash, as AcceptedJob does; the simulator has no profile in which one arrives.
* **Client behaviour.** The simulated clients send to servers that believe they lead, which the workload reads from the simulator's state. A real client learns that from redirects, which this node does not send; a request to a server that is not a leader is dropped silently.
* **Capacity.** With a log of 8 entries and no compaction a cluster stops taking requests after 8 entries; a `full` answer occurred in 2 of 150 and 1 of 60 runs.
* **The flake check.** `tool check raft` is not added to `checks.sim-tool` in `flake.nix`: it takes several minutes and that line is shared.
* **Solo5.** Not attempted.
