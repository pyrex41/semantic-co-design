# Raft on the kernel

One Raft server written as an ordinary Shen step function ([spec.shen](spec.shen)), compiled to native code by `tool/`, run in 3-server and 5-server clusters under the deterministic simulator, judged by invariants written in Shen ([invariants.shen](invariants.shen)) and by an independent linearizability checker ([lin.py](lin.py)), searched exhaustively in small bounds by Maude, and set beside TLC on the published `raft.tla`.

```sh
nix develop -c python3 -m tool check raft                    # the specification, about 5 minutes here
nix develop -c python3 -m targets.raft.check mutants         # the teeth of every check, about 4.5 minutes here
nix develop -c nix shell --inputs-from . 'nixpkgs#tlaplus' -c python3 -m targets.raft.tla.check   # about 6 minutes here
nix develop -c python3 -m tool compile raft /tmp/raft        # IR, C, library, executable
nix develop -c python3 -m targets.raft.check story local-get three     # a retained failing trace, readable
nix develop -c python3 -m targets.raft.check story commit-by-counting churn paper   # Figure 8, as a seed found it
nix develop -c python3 -m targets.raft.check hunt <mutant> [three|five|churn|amnesia|late ...] [--from=N] [--seeds=M] [--only=CHECK|paper]
```

The retained check is the first two commands together. It is two commands so that each stays well under ten minutes, and `nix flake check` runs them as `checks.<system>.raft` and `checks.<system>.raft-mutants`. Run alone they took 5 minutes 14 seconds and 4 minutes 24 seconds here; built side by side as flake checks, 5 minutes 54 seconds and 4 minutes 53 seconds. The numbers below are the output of these commands on the commit that last changed this file.

## 1. The node

Figure 2 of Ongaro and Ousterhout, "In Search of an Understandable Consensus Algorithm (Extended Version)": leader election, log replication, commit index, with a key-value state machine (two keys, `put` and `get`) on top. It is one pure function `raft.step : State Event -> [State' Effects]` and one initial value `raft.init`; the file computes values and never mentions residual code, the simulator or integers standing for symbols. shen-jit runs it unchanged and is the reference for the native code.

| | |
|---|---|
| Persistent state | `currentTerm`, `votedFor`, the log. On disk as row 0 `[Term meta Voted Len 0]` and one row per index `[Term Op Key Val Req]`. |
| Write order | Every prefix of the writes of a step is a state the server was allowed to be in: a new entry is written before the length that includes it, and a conflicting suffix is cut (the shorter length is written) before the entry that replaces it. A crash keeps a prefix of the pending writes. |
| Before replying | A reply to RequestVote or AppendEntries leaves only when every write issued before it is acknowledged. Until then it waits in one slot (`Hold`); a newer reply replaces a waiting one, which is a lost message. A candidate asks for votes only after its own vote is durable; a leader counts itself towards a majority only up to its durable length. |
| Recovery | After a restart the server reads row 0, then one row per entry, and takes no part in the protocol until that is done. If a row inside the recorded length is not on the disk, which the write order excludes, the server takes the role `failed` and does nothing further. |
| Clients | Only a leader takes a request, and every request becomes a log entry of its term. A `get` goes through the log like a `put`, so its answer is confirmed by a majority. Requests carry a number; a retry is appended again and the state machine executes the first copy only (section 8 of the paper). A leader answers when it applies the entry. |
| Messages | One entry per AppendEntries. One committed entry is applied per step. A message counts only if its sender is another server of the cluster (an id from 0 to N-1 that is not the receiver's); any other is ignored. |
| Timers | Heartbeat 50. Election timeout `150 + (41 * Id + 59 * Term) mod 200`: a fixed function of server and term, because the environment offers a node no randomness. The simulator skews it (section 3). |
| Declared bounds | At most 5 servers. Log capacity 8 entries; a leader whose log is full answers `full`. |

Not there, by decision: membership change, snapshots and log compaction, a no-op entry at the start of a term (an entry of an earlier term commits when a later entry of the current term does, which a client's retry supplies), read leases or ReadIndex, batching.

What the specification relies on from its environment is listed at the top of `spec.shen` and is exactly the environment rules of [tool/README.md](../../tool/README.md): whole rows, syncs in issue order, a crash keeps a prefix of pending writes, no completion crosses a crash.

## 2. Compilation

The whole node is compiled. Nothing is interpreted at run time and nothing is hand-written in C.

`spec.shen` (272 lines without comments, 40 definitions) becomes 104,081 IR nodes, 4,568 slots, no residual loop blocks and 33,453 lines of C. A server's state is 73 integers. The library and the executable give the same initial state for every server of a 3-server and a 5-server cluster.

The compiler (`tool/pe.py`, `tool/compile.py`) was not changed for this target. What it took was writing the node inside the compiler's documented limits, all of which leave it ordinary Shen:

* Each handler is one `let` chain whose conditionals are in value position, so the two arms join leaf by leaf instead of duplicating the rest of the step. The runtime decisions in tail position are the event kind, "still loading", the tests of recovery, and whether a message comes from a member.
* The log and the per-server arrays are lists of declared capacity. Access by a runtime index (`raft.entry`, `raft.store`, `raft.at`, `raft.set`) is recursion over the list, which the compiler unfolds into a chain of tests, linear in the capacity at every access. The accessors are total (an index outside the list gives a default), so no path of the residual code can trap on a bad index.
* Loops over servers and over log indexes (`raft.feeds`, `raft.asks`, `raft.advance`, `raft.replicas`) recurse on a static counter up to the capacity and compare with the runtime server count or length inside.
* A step's effect list has a length that depends on runtime conditions. The compiler keeps such a list as a split value and emits one return path per reachable shape. The handlers therefore choose among whole write lists with one conditional rather than appending many optional effects.

The cost of that is size, not capability: every log access is 8 comparisons and a 5-leaf join, and the residual program is two orders of magnitude larger than its source. No timing is claimed here: an earlier version of this file gave one, and no retained command measured it.

Kernel changes made for this target, all additive and in commits whose message starts with `kernel:`:

* `tool/sim`: optional profile keys. `budget["oneway"]`: asymmetric cuts (everything into, or out of, one node is discarded until `heal`). `delay["timer"]` and `timer_rate`: a timer runs for its delay scaled by a drawn skew and by the node's clock rate. `budget["timer"]`: a bound on timer events for untimed searches. `slow` with `delay["net-slow"]` and `delay["disk-slow"]`: a drawn share of messages and of disk operations takes its delay from a second range.
* `tool/maude.py`: the same timer budget in the Maude environment.
* `tool/sim_options_check.py`, run by `tool check sim`: checks of those keys on the gossip and probe nodes, with a simulator or Maude model that ignores each key as the mutant.
* `tool/__main__.py`: `tool check <name>` also finds `targets/<name>`.

## 3. Simulation

`PROFILES` in [check.py](check.py): seeded, timed schedules with budgets of 4 crashes, 4 drops, 2 duplicates, 2 symmetric partitions and 2 one-way cuts per run. Message delays are 1 to 20 and disk delays 1 to 5, except that one message in twenty takes 100 to 500 (up to several election timeouts: the shortest is 108) and one disk operation in twelve takes 20 to 150 (longer than a round trip to a follower). Timers run 80% to 120% of what the node asked for on clocks at 90% to 115% of nominal rate. A crash can cut a step between two of its effects; it loses the node's memory and a suffix of its pending writes. The clients ([workload.py](workload.py)) hold five operations on two keys, keep two open at a time, and retry an unanswered one after 200 time units; they send to whichever servers currently believe they lead, stale leaders included.

The first version of these profiles had three faults of shape that its reviewers measured: the three crashes of a run were spent in its first tenth, because a crash in the middle of a step was drawn at 5% on every step with effects; no message could be delayed longer than an election timeout; and a leader's own disk write always landed before a follower's acknowledgement came back. Each made a class of edits invisible. The rates and the two slow shares are the repair, and the campaign now reports the shape and fails if it is lost. The teeth of that requirement (`check mutants`): under the rates of the first version the crashes of 30 runs of `three` fall at step index quartiles 32, 61 and 93 of 1,000, no run has one in its second half, and the requirement rejects that.

| | `three` | `five` |
|---|---|---|
| Servers, seeds, steps | 3, 150, 150,000 | 5, 60, 84,000 |
| Crashes between steps, crashes in the middle of a step, restarts | 446, 72, 513 | 190, 32, 222 |
| Step index of the crashes: quartiles, of the length of a run | 273, 456, 671 of 1,000 | 367, 613, 898 of 1,400 |
| Runs with a crash in their second half (required: at least half) | 112 of 150 | 46 of 60 |
| Drops, duplicates | 271, 196 | 190, 111 |
| Partitions, one-way cuts, heals | 119, 190, 218 | 92, 64, 121 |
| Messages lost to a server that was down | 1,733 | 471 |
| Issued writes that never reached the disk | 26 | 8 |
| A disk held part of a step's writes: after a sync, after a crash | 2,480, 3 | 1,655, 1 |
| A granted vote of an earlier term reached a candidate | 3 | 12 |
| An acknowledgement of an earlier term reached a leader | 16 | 9 |
| AppendEntries of an earlier term reached a server; of those, with an entry | 1,127; 346 | 391; 58 |
| AppendEntries handled after a later one from the same leader | 351 | 237 |
| Runs in which two servers held different entries at one index | 10 | 4 |
| Runs with: a leader change, a conflicting suffix cut, a log recovered from disk, an entry committed on 3 servers, a `full` answer, a retried put skipped | 142, 10, 149, 142, 1, 40 | 58, 4, 59, 60, 0, 14 |
| Runs by operations answered, 0 to 5 of 5 | 2, 3, 5, 3, 9, 128 | 0, 1, 3, 2, 4, 50 |
| Traces replayed and re-run from their seed | 15 | 6 |

The fourteen invariants of section 4 hold on all 210 traces and all 210 client histories are linearizable.

Every run is checked as it is produced: it must end at its step bound with no trap; every step changed the disk queue and the outputs in flight by exactly its applied effects (`check.effects_applied`); the invariants and the linearizability check below hold; every tenth trace is replayed step by step from its recorded actions and draws and re-run from its seed, byte for byte. Every distinct `(state, event)` pair the native step was run on is then evaluated by shen-jit on `spec.shen` and compared: 104,783 pairs over these runs and the biased runs below. The check fails if any of the five histories from "a disk held part of a step's writes" to "AppendEntries of an earlier term with an entry" never occurs in the two campaigns together.

### Biased profiles

`three` and `five` spread a little of every fault over a run. In hundreds of seeds they did not produce the histories in which several mutants violate a published property (section 5): Figure 8 and a vote lost in a crash among them. `BIASED` in `check.py` holds three more seeded profiles of three servers, each made for one kind of history. They were written after the fact, knowing which consequences were missing. They name no server, term or step: what is found under them is found by a seed, in a fault model chosen to make it likely.

| | `churn` | `amnesia` | `late` |
|---|---|---|---|
| What it is for | leaders cut off while they hold entries nobody else has, and replaced | a crash between a reply and the write it depends on | answers of earlier terms; acknowledgements that overtake the disk of the server counting them |
| Faults per run | up to 12 partitions, 2 crashes, a fault tried on 3% of the steps | up to 40 crashes, on 4% of the steps; a down server restarts on half of the steps | up to 20 crashes and 20 partitions, on 4% of the steps |
| Delays | messages 1 to 20, disk 1 to 5 | messages and disk 1 to 40 | messages and disk 1 to 40; a quarter of the messages 100 to 600, a quarter of the disk operations 40 to 300 |
| Clients | act on 2% of the steps, three sends per operation, 150 apart | as in `three` | three sends per operation, 150 apart |
| Seeds, steps | 40, 40,000 | 40, 40,000 | 40, 40,000 |
| Crashes, restarts, partitions, heals | 80, 80, 463, 459 | 1,536, 1,536, 0, 0 | 800, 800, 400, 379 |
| Step index of the faults that must be spread: quartiles of 1,000 | partitions: 270, 447, 654 | crashes: 238, 456, 712 | crashes and partitions: 208, 458, 676 |
| Runs with such a fault in their second half | 40 of 40 | 40 of 40 | 40 of 40 |
| Issued writes that never reached the disk | 4 | 119 | 135 |
| A disk held part of a step's writes after a crash | 0 | 18 | 16 |
| A granted vote of an earlier term reached a candidate | 0 | 0 | 62 |
| An acknowledgement of an earlier term reached a leader | 0 | 0 | 14 |
| Runs in which two servers held different entries at one index | 16 | 13 | 13 |
| Histories the campaign requires of it | two servers with different entries at one index | a write that never reached the disk; part of a step's writes on a disk after a crash | a vote and an acknowledgement of an earlier term; a write that never reached the disk |
| Runs by operations answered, 0 to 5 of 5 | 0, 0, 0, 0, 2, 38 | 5, 7, 4, 3, 6, 15 | 7, 4, 9, 8, 6, 6 |
| Runs with a `full` answer | 0 | 0 | 20 |

The specification is held to the same checks under them as under `three` and `five`: all fourteen invariants, linearizability, no trap, effects applied, every tenth trace replayed, every pair compared with shen-jit. It passes on all 120 runs. The two crashes of `churn` fall early (its partitions are what is spread), and in half of the runs of `late` the log of eight entries fills up, after which no request is taken: both limit what those profiles can show late in a run. The teeth of the requirement on histories (`check mutants`): 10 runs of three servers with no faults and nothing slow show none of them.

A one-off run, not retained: the specification also passes every check on seeds 0 to 299 of each of the three. The first draft of `amnesia` and `late` spent their crashes in the first fifth of a run, the fault the reviewers had found in `three`; the budgets were raised until the faults last the whole run, the campaign requires it, and every result in this file is from the profiles as they are in the tree.

**Messages from non-members** are not something the simulator produces: it fills in the true sender. The check therefore hands them to the step directly: 75 messages of all four kinds under the ids -1, 0 (the receiver itself), 3, 4 and 5, to server 0 of 3 as follower, as candidate and as leader, change nothing and cause no effect, natively and in shen-jit. In the first version a vote under id 4 made a candidate of a 3-server cluster the leader; a node edited back to that is the mutant of this check.

## 4. Invariants

Written in Shen, evaluated by shen-jit over every trace, at every step at which a server state or a disk can change. They share no code with the node. A server that is still reading its disk back, or that stopped as `failed`, counts as down for the log properties; `inv.recovery-completes` is about exactly those servers.

The five properties of Figure 3 of the paper:

| Property | Shen, over a whole trace | Per reachable world, bounded search | TLA+ on `raft.tla` |
|---|---|---|---|
| Election Safety | `inv.election-safety`: no two servers ever lead the same term, across the whole history | no two leaders of one term in one world | `ElectionSafety`, over the `elections` history variable |
| Leader Append-Only | `inv.leader-append-only`: between consecutive worlds in which a server leads the same term, its old log is a prefix of its new one | not checked | `LeaderAppendOnly`, an action property |
| Log Matching | `inv.log-matching`: every pair of running servers in every world | same | `LogMatching` |
| Leader Completeness | `inv.leader-completeness`: every entry some server has had at or below its commit index is in the log of every leader of that server's term then, or a later one | not checked | `LeaderCompleteness`, over current terms |
| State Machine Safety | `inv.state-machine-safety`: all servers, over the whole history, apply the same entry at each index; and each server's store is what its applied entries produce, each request once | committed prefixes of running servers agree | `StateMachineSafety` |

Rules of Figure 2 and of the header of `spec.shen`, each stated directly. None of them is one of the five properties; `raft.tla` has no disk and states none of them. They are kept apart in the mutant results for that reason: a mutant rejected by one of these has been shown to break a rule, and whether it also breaks a published property is a separate column.

| Rule | Shen, over a whole trace | Per reachable world, bounded search |
|---|---|---|
| Figure 2, "updated on stable storage before responding to RPCs" | `inv.replies-are-durable`: a granted vote is one that was on the voter's disk; a successful AppendEntries reply for index n needs n durable entries; no reply names a term the disk does not have; a request for votes comes from a candidate whose vote for itself is on its disk | a leader has a majority of durable votes; an answered put is durable on a majority of disks |
| Section 5.4.2, the commit rule | `inv.commit-rule`: a leader advances its commit index only to an entry of its own term | not checked |
| A leader was elected | `inv.leaders-are-elected`: whenever a server leads a term, more than half of the disks have held a vote for it in that term | same, per world |
| Figure 2, one vote per term | `inv.one-vote-per-term`: over everything a server ever sent, restarts included, it grants no two candidates its vote in one term, a request for votes counting as a vote for itself | not checked |
| The write order | `inv.disk-is-a-held-state`: in every world, what a restart would read from each disk is a term, a vote and a log whose rows are all there, and the server has had that term and vote in memory with that log or an extension of it | a disk never records a length whose rows are not all there |
| Recovery restores the disk | `inv.recovery-restores-the-disk`: when a server finishes reading its disk back, its term, vote and log are exactly what the disk holds | not checked |
| Recovery completes | `inv.recovery-completes`: a loading server always has a read outstanding (each restart and each row handed to it either asks for the next row or ends recovery), and no server ever becomes `failed` | no server is `failed` |
| A leader commits what a majority holds | `inv.commits-are-durable`: when a leader's commit index advances, every entry it newly covers is inside the recorded length of more than half of the disks | an answered put is durable on a majority of disks |
| What a leader counts is on disk | `inv.match-is-durable`: for a leader of term T, its matchIndex for a server whose disk is not in a later term is a length up to which that disk equals the leader's log; for its own slot, its durable length | a leader's own slot is covered by its disk |

Linearizability of the client-visible history is checked by `lin.py`, a Wing and Gong search over the operations of a trace: invocation is the step at which the request was first handed to the network, response is the step at which an `ok` reply first reached the outside. An operation with no reply is open (a put may have taken effect or not). The checker knows nothing of Raft. Its own teeth are 12 hand-written histories, 6 of them not linearizable (stale read, lost update, a value never written, a read before the write began, a put applied twice, two reads that go backwards).

## 5. Mutants

Each mutant is one textual edit of `spec.shen` (`MUTANTS` in `check.py`), compiled like the specification. `hunt` runs seeds 0, 1, 2, ... of a profile until a check fails, shrinks the failing trace by delta debugging on the schedule ([shrink.py](shrink.py)), and retains seed, check, digests and the short schedule in [mutants.json](mutants.json). `--only=paper` searches for a failure of a published property (the five of Figure 3, linearizability, no trap) and retains it separately. `check mutants` rebuilds every mutant, re-runs its seed, replays its short trace and requires the named check to reject both. It requires the specification to pass every check on the same seed and on the same short schedule: of the 35 short schedules it runs 8 exactly as recorded and 27 with the actions it does not enable skipped (617 of their 3,401 actions). It also requires every one of the fourteen invariants to reject at least one retained mutant, and a retained seed on which a published property rejects each of the four required mutants.

The first eight are the author's; the first four were required. Thirteen of the other fourteen are edits made by the reviewers of the first version, reproduced on that version before anything here was changed: ten survive every one of its checks on seeds 0 to 149 of `three` (as does a second way of forgetting the vote, which is not retained separately); `ack-old-term` fails only the commit rule there, on seed 132; `leader-overwrites-last` and `no-prev-term-check` fail Leader Append-Only on seed 0 and Log Matching on seed 3, which no retained mutant showed. `recovery-never-completes` was added here as the mutant of `inv.recovery-completes` and of the second clause of the liveness check.

"Seeds" is the number of seeds tried up to and including the first failure. The third column is the first failure of any check on `three`. The fourth says whether a seed also shows a published property failing: on `three`, or else under one of the biased profiles of section 3. "Directed" names a hand-written schedule of section 6.

| Mutant | The edit | `three`: first check to fail, seeds, steps after shrinking | A published property fails on a seed | Directed |
|---|---|---|---|---|
| `vote-without-log-check` (required, 1) | grants a vote without the up-to-date check (5.4.1) | Leader Completeness, seed 1 (2 seeds), 194 steps | `three`: the first check to fail is one | none |
| `commit-by-counting` (required, 2) | commits an entry of an earlier term by counting replicas (Figure 8) | `inv.commit-rule`, seed 0 (1 seed), 69 steps | `churn`: Leader Completeness, seed 4 (5 seeds), 153 steps. `three`: **not found in 800 seeds** | figure 8 |
| `vote-before-durable` (required, 3) | answers a vote request before votedFor is durable | `inv.replies-are-durable`, seed 0 (1 seed), 9 steps | `amnesia`: Election Safety, seed 59 (60 seeds), 117 steps. `three`: **not found in 400 seeds** | lost vote |
| `local-get` (required, 4) | a leader answers a get from its own state without confirming leadership | linearizability, seed 161 (162 seeds), 54 steps | `three`: the first check to fail is one | none |
| `votes-twice` | grants a vote in a term in which it already voted for another server | `inv.one-vote-per-term`, seed 1 (2 seeds), 13 steps | `three`: Election Safety, seed 5 (6 seeds), 135 steps | none |
| `ack-before-durable` | answers AppendEntries before the entry is durable | `inv.replies-are-durable`, seed 0 (1 seed), 15 steps | `late`: Leader Completeness, seed 2 (3 seeds), 109 steps. `three`: not found in 150 seeds | lost acknowledgement |
| `commit-unchecked-suffix` | a follower commits up to its own last entry, not the last one the leader confirmed | Leader Completeness, seed 48 (49 seeds), 108 steps | `three`: the first check to fail is one | none |
| `put-twice` | executes every copy of a put that a client's retries left in the log | State Machine Safety, seed 12 (13 seeds), 57 steps | `three`: the first check to fail is one | none |
| `leader-overwrites-last` | a leader puts a new entry in the place of its last one | Leader Append-Only, seed 0 (1 seed), 26 steps | `three`: the first check to fail is one | none |
| `no-prev-term-check` | a follower accepts AppendEntries without comparing prevLogTerm | Log Matching, seed 48 (49 seeds), 147 steps | `three`: the first check to fail is one | none |
| `accept-stale-leader` | a follower treats AppendEntries of an earlier term as coming from its leader | Leader Completeness, seed 119 (120 seeds), 56 steps | `three`: the first check to fail is one | stale leader |
| `ack-old-term` | a follower stores the entry of an AppendEntries of an earlier term | `inv.commit-rule`, seed 61 (62 seeds), 149 steps | `three`: Leader Completeness, seed 119 (120 seeds), 56 steps | stale leader |
| `stale-vote-counted` | a candidate counts a vote that was granted in an earlier term | `inv.leaders-are-elected`, seed 42 (43 seeds), 207 steps | `late`: Election Safety, seed 12 (13 seeds), 460 steps. `three`: not found in 150 seeds | stale vote |
| `stale-ack-counted` | a leader counts an acknowledgement that was given in an earlier term | **no check fails in 400 seeds**. `late`: `inv.commits-are-durable`, seed 643 (644 seeds), 132 steps | **not found**: `late`, 400 seeds | stale acknowledgement |
| `recovery-forgets-vote` | a restarted server has no vote, whatever its disk says | `inv.recovery-restores-the-disk`, seed 0 (1 seed), 54 steps | `late`: Election Safety, seed 50 (51 seeds), 338 steps. `three`: not found in 150 seeds | forgotten vote |
| `recovery-never-completes` | a server that restarts with entries on its disk reads past the last one | `inv.recovery-completes`, seed 0 (1 seed), 55 steps | not searched: the consequence is not a safety violation | none |
| `candidate-asks-before-durable` | a candidate asks for votes before its vote for itself is durable | `inv.replies-are-durable`, seed 4 (5 seeds), 82 steps | **not found**: `three` 150 seeds, `amnesia` 400, `late` 400 | unwritten candidacy |
| `leader-counts-undurable-self` | a leader counts its own copy of an entry the moment it appends it | `inv.match-is-durable`, seed 0 (1 seed), 14 steps | `late`: Leader Completeness, seed 22 (23 seeds), 240 steps. `three`: not found in 150 seeds | slow disk |
| `self-counts-before-durable` | a leader counts its whole log, durable or not, when an acknowledgement arrives | `inv.commits-are-durable`, seed 2 (3 seeds), 19 steps | `late`: Leader Completeness, seed 22 (23 seeds), 240 steps. `three`: not found in 150 seeds | slow disk |
| `length-before-entry` | a leader writes the new length before the entry it covers | `inv.disk-is-a-held-state`, seed 0 (1 seed), 15 steps | not searched: the consequence is not a safety violation | torn append, leader |
| `follower-length-before-entry` | a follower writes the new length before the entry it covers | `inv.disk-is-a-held-state`, seed 0 (1 seed), 16 steps | not searched: the consequence is not a safety violation | torn append, follower |
| `entry-before-cut` | a follower writes the replacing entry before it cuts the conflicting suffix | `inv.disk-is-a-held-state`, seed 32 (33 seeds), 83 steps | **not found**: `three` 150 seeds, `amnesia` 400, `late` 400 | torn cut |

`five` was run for the four required mutants: `vote-without-log-check`: Leader Completeness, seed 1 (2 seeds), 263 steps. `commit-by-counting`: `inv.commit-rule`, seed 0 (1 seed), 88 steps; no published property fails in 200 seeds. `vote-before-durable`: `inv.replies-are-durable`, seed 0 (1 seed), 11 steps; no published property fails in 200 seeds. `local-get`: linearizability, seed 48 (49 seeds), 79 steps. `stale-ack-counted` fails no check in 200 seeds of `five`. There is no biased profile of five servers.

What the columns say, plainly:

* **Figure 8 is found by a seed, under `churn` and not under the general profiles.** On seed 4 of `churn` the published properties reject `commit-by-counting`: Leader Completeness first, State Machine Safety later in the same run. `story commit-by-counting churn paper` prints the shrunk trace. Server 0 is elected in term 9 while server 1 still believes it leads term 8, and server 1 takes a get and appends it at index 4 with term 8. Server 1 is elected again in term 11 while server 0 still believes it leads term 9, and server 0 takes a put and appends it at index 4 with term 9. Server 1 sends its term-8 entry to server 2, counts two copies, commits it and answers the client. Server 2 then elects server 0 in term 12: its last entry is of term 9, and it does not have the committed one. The general profiles still do not find it: the published properties accept the mutant on seeds 0 to 799 of `three` and 0 to 199 of `five`, where `inv.commit-rule` rejects it on the first seed.
* **The lost vote is found by a seed, under `amnesia` and not under the general profiles.** On seed 59 of `amnesia` Election Safety rejects `vote-before-durable`: two leaders in one term. On `three` and `five` only `inv.replies-are-durable` rejects it (400 and 200 seeds).
* **Every mutant is rejected on a retained seed.** For `stale-ack-counted` that seed is the 644th of `late`, where `inv.commits-are-durable` fails (and `inv.match-is-durable` on the same trace). No check fails on seeds 0 to 399 of `three` or 0 to 199 of `five`, although in the campaigns of the specification under those two profiles an acknowledgement of an earlier term reaches a leader 25 times: it only matters when the follower's log has been rewritten in between and the leader has been deposed and re-elected.
* **For three mutants a seed shows the broken rule and no broken published property**: `stale-ack-counted`, `candidate-asks-before-durable` and `entry-before-cut`, on the seeds listed. For each a directed schedule shows the consequence. Three more were not searched, `recovery-never-completes` and the two `length-before-entry` mutants, because their consequence is a server that never comes back: an availability failure, which `inv.recovery-completes` and the liveness check of section 9 report and no safety property can.
* **A biased profile is evidence of a different kind from a general one.** That a published property rejects five mutants within the first 51 seeds of `late` says that the checks reject those histories when the fault model produces them, and that a fault model can be chosen to produce them. It does not say that the profile is a fair sample of anything: each was written, and its rates adjusted, with the missing histories in view.
* The shrunk traces are not minimal: the shrinker removes steps of a timed schedule, and a history that elects two leaders needs some tens of steps for each. `story` prints one.

## 6. Directed schedules

[figure8.py](figure8.py) and [directed.py](directed.py): twelve schedules written by hand, most of them by the reviewers, each a program that picks one enabled action at a time in an untimed profile. They are not found by a seed and are labelled so in the output. For nine of the fourteen rows below a seed now shows a published property failing too, seven of them under a biased profile (section 5); the schedules stay, because they are short and say which history is meant. Each is an ordinary scripted trace that replays. On the specification every check passes on every one of them; on the mutant the listed checks must all fail.

| Schedule | Steps on the specification | Mutant | Checks that reject the mutant on it |
|---|---|---|---|
| figure 8 | 83 | `commit-by-counting` (83 steps) | Leader Completeness, State Machine Safety, `inv.commit-rule` |
| lost vote | 20 | `vote-before-durable` (21 steps) | Election Safety, `inv.replies-are-durable`, `inv.leaders-are-elected`, `inv.one-vote-per-term` |
| forgotten vote | 21 | `recovery-forgets-vote` (23 steps) | Election Safety, `inv.one-vote-per-term`, `inv.recovery-restores-the-disk` |
| unwritten candidacy | 23 | `candidate-asks-before-durable` (27 steps) | Election Safety, `inv.replies-are-durable`, `inv.leaders-are-elected`, `inv.one-vote-per-term` |
| stale vote | 26 | `stale-vote-counted` (26 steps) | Election Safety, `inv.leaders-are-elected` |
| stale leader | 54 | `accept-stale-leader` (60 steps) | Leader Completeness, State Machine Safety, `inv.match-is-durable` |
| stale leader | 54 | `ack-old-term` (60 steps) | Leader Completeness, State Machine Safety, `inv.match-is-durable` |
| slow disk | 34 | `leader-counts-undurable-self` (34 steps) | Leader Completeness, `inv.commits-are-durable`, `inv.match-is-durable` |
| slow disk | 34 | `self-counts-before-durable` (34 steps) | Leader Completeness, `inv.commits-are-durable` |
| lost acknowledgement | 33 | `ack-before-durable` (34 steps) | Leader Completeness, `inv.replies-are-durable`, `inv.commits-are-durable`, `inv.match-is-durable` |
| torn append, leader | 23 | `length-before-entry` (24 steps) | `inv.disk-is-a-held-state`, `inv.recovery-completes` |
| torn append, follower | 28 | `follower-length-before-entry` (29 steps) | `inv.disk-is-a-held-state`, `inv.recovery-completes` |
| torn cut | 192 | `entry-before-cut` (180 steps) | Log Matching, Leader Completeness, State Machine Safety, `inv.disk-is-a-held-state`, `inv.commits-are-durable`, `inv.match-is-durable` |
| stale acknowledgement | 143 | `stale-ack-counted` (143 steps) | Leader Completeness, `inv.commits-are-durable`, `inv.match-is-durable` |

## 7. Bounded search

Untimed profiles: any enabled action may happen next, and a budget bounds the number of timer events. Maude searches the translated Shen in the Maude environment; `sim.explore` visits every schedule of the native step. The two reachable observation sets (server memories, durable rows, replies that got out) must be equal, no reachable step may be without a result, and every reachable world must satisfy the per-world predicates of section 4 (`unsafe` in `check.py`, restated in Python).

| Bound | Maude states | Simulator states | Observations, equal on both sides | with a leader | with the put answered |
|---|---|---|---|---|---|
| 2 servers, 2 timers, a put | 38,736 | 36,526 | 2,210 | 1,668 | 56 |
| 2 servers, 1 timer, a put, a crash | 7,782 | 6,500 | 1,282 | 850 | 26 |
| 2 servers, 1 timer, a put, a drop | 2,312 | 1,740 | 572 | 494 | 10 |
| 3 servers, 1 timer, a duplicate | 6,103 | 5,870 | 233 | 126 | no client |
| 3 servers, 1 timer, a crash | 9,692 | 8,352 | 1,340 | 540 | no client |

Teeth (`check mutants`): each of these mutants is searched in one of the bounds, Maude and the simulator must still agree with each other, and some reachable world must be unsafe.

| Mutant | Bound | Observations | Unsafe | First reason |
|---|---|---|---|---|
| `vote-before-durable` | 2 servers, 1 timer, a put, a crash | 2,446 | 748 | server 0 leads term 1 with 1 durable votes |
| `ack-before-durable` | 2 servers, 1 timer, a put, a crash | 1,922 | 100 | request 1 was answered with its entry durable on 1 disks |
| `recovery-forgets-vote` | 2 servers, 1 timer, a put, a crash | 1,282 | 224 | server 1 leads term 1 with 1 durable votes |
| `self-counts-before-durable` | 2 servers, 1 timer, a put, a crash | 1,384 | 66 | request 1 was answered with its entry durable on 1 disks |
| `leader-counts-undurable-self` | 2 servers, 1 timer, a put, a crash | 1,384 | 474 | server 0 counts 1 entries of its own, which its disk does not hold |
| `length-before-entry` | 2 servers, 1 timer, a put, a crash | 1,308 | 376 | the disk of server 0 records 1 entries and lacks one of them |
| `candidate-asks-before-durable` | 2 servers, 2 timers, a put | 3,880 | 234 | server 0 leads term 2 with 1 durable votes |
| `stale-vote-counted` | 2 servers, 2 timers, a put | 3,196 | 652 | server 0 leads term 2 with 1 durable votes |

The native step equals shen-jit on the 1,524 pairs the searches executed. The Maude translation of `spec.shen` reduces those pairs and 1,519 pairs sampled from the seeded runs to the native result, and the translation of `commit-by-counting` does not.

Larger bounds are out of reach for both searchers. While this target was written, 3 servers with one election and a put, and 3 servers with two timer events, each ran past 250,000 states, and 2 servers with a put and a duplicate was left out for time. Those runs were not retained and no command in the tree reproduces their sizes. The eight mutants above are the ones searched. The other fourteen were not searched by the retained check, and nothing is claimed about what these bounds would show of them; the reviewers ran `accept-stale-leader` and `ack-old-term` in them and found every observation safe.

## 8. TLC on the published raft.tla

[tla/raft.tla](tla/raft.tla) is byte for byte the file at github.com/ongardie/raft.tla (Copyright 2014 Diego Ongaro, CC BY 4.0; the check verifies its SHA-256). It states no invariants. [tla/MCraft.tla](tla/MCraft.tla) adds a bound and the five properties of Figure 3 as stated in the table of section 4. TLC 2.19 comes from the locked nixpkgs binary cache (`nixpkgs#tlaplus` 1.7.4); it is not in the development shell, so this is a separate command.

```
TLC, published raft.tla, 3 servers, 2 values, max term 2, log 1, messages 1: 50251285 states generated, 4054030 distinct, every state explored, no violation of ElectionSafety, LogMatching, LeaderCompleteness, StateMachineSafety or LeaderAppendOnly (148s). AtMostOneElection holds too: there is at most one election at this bound, so LeaderCompleteness cannot fail in this run.
  mutant votes-twice at 2 values, max term 2, log 1, messages 1, all five checked: violates ElectionSafety (140560 distinct states, 5s)
  mutant leader-without-quorum at 2 values, max term 2, log 1, messages 1, all five checked: violates ElectionSafety (181 distinct states, 1s)
  mutant commit-by-counting at 1 value, max term 2, log 1, messages 1, all five checked: NOT rejected within this bound (2379781 distinct states, 82s)
each other property, checked alone against an edit at a bound where it can fail:
  LeaderAppendOnly: mutant leader-overwrites-last at 2 values, max term 2, log 1, messages 1 violates it (11561 distinct states, 1s); the bound of the exhaustive run
  LogMatching: mutant leader-overwrites-last at 2 values, max term 2, log 1, messages 1 violates it (38449 distinct states, 2s); the bound of the exhaustive run
  StateMachineSafety: mutant leader-overwrites-last at 2 values, max term 2, log 1, messages 1 violates it (356852 distinct states, 7s); the bound of the exhaustive run
  LeaderCompleteness: mutant vote-without-log-check at 1 value, max term 3, log 1, messages 1 violates it (9004520 distinct states, 137s); a bound at which the published specification is not explored to the end
raft tla: ok
```

What this does and does not show:

* **The exhaustive run exercises four of the five properties.** Its bound has two values, terms up to 2, one entry and one message in flight. Election Safety, Leader Append-Only, Log Matching and State Machine Safety can each fail at that bound: an edit of `raft.tla` is rejected there by each of them. On the published specification none fails in any reachable state.
* **Leader Completeness is not exercised by the exhaustive run.** With terms up to 2 at most one election happens; the run checks that as an invariant (`AtMostOneElection`) and it holds. The only leader there is, is the one that committed. Leader Completeness rejects an edit at a bound with a second election (terms up to 3), which shows that the property as written can fail. The published specification is not explored to the end at that bound: `python3 -m targets.raft.tla.check deeper` ran it there for 500 seconds and TLC had not finished, so nothing is claimed about it.
* The first version of this file said that the published specification "satisfies the five properties" in a bound with one value and terms up to 2. In that bound every log entry is the same entry, so for four of the five that was true and empty, and both rejected edits violated Election Safety only.
* The Figure 8 edit is not rejected: no bound this small reaches the history, here as in the simulation.
* TLC stops a mutant at its first violation with two workers, so those state counts differ by a few hundred between runs.
* The bounds are small because `raft.tla` carries history variables and a message bag. With two messages in flight instead of one, TLC had 2.7 million distinct states after a minute and a growing queue; that run was stopped, was not retained and is not a result.

Comparison of the invariant sets: the five named properties are checked on both sides, as history properties over simulator traces on one and as state or action properties over reachable states of a bound on the other. The Shen side adds the nine rules of section 4 and linearizability of a client history, none of which `raft.tla` can state (it has no disk and no client replies). The TLA+ side covers every interleaving of its bound, which the seeded simulation does not. Nothing here relates `raft.tla` to `spec.shen`: they are two descriptions of one protocol by different authors, checked against the same named properties.

## 9. Liveness

At virtual time 1500 the client performs one action that ends all faults: budgets go to zero and the network heals. Late messages and disk stalls are not faults and go on. From then on the client sends one more put to one server after another every 60 time units. Declared bound, 1500 time units from the moment faults stop, for two things: the `ok` for that put reaches the outside; and every server is up, has finished recovery and has applied that put, from then until the end of the run.

| | `three` | `five` |
|---|---|---|
| Seeds | 60 | 30 |
| Answered within the bound | 60 | 30 |
| Time from the end of faults to the answer: least, median, most | 35, 166, 875 | 39, 306, 952 |
| Every server up, recovered and caught up within the bound | 60 | 30 |
| Time until that holds: least, median, most | 51, 208, 893 | 54, 358, 1017 |

Teeth (`check mutants`): a node that never releases a reply waiting for the disk (`held-reply-never-sent`) and a node that never grants a vote (`never-grants`) answer within the bound on none of 10 seeds of `three`. Those two disable the protocol. A mutant that only degrades it is `recovery-never-completes`: on 20 seeds of `three` the put is answered in time on 20, and on 6 of those a server never comes back within the bound. The first clause alone accepts it on those seeds; the second is what rejects it.

This is a measurement on these seeds, not a proof.

History, not a result of any retained command: the first draft of the node failed this check, because a put appended in one term, whose leader was deposed and re-elected, was never committed (retries of a put already in the log were not appended again and nothing of the new term ever entered the log). The node appends every request it takes and the state machine skips repeats, as section 8 of the paper describes.

## 10. What is established, and what is not

Established, by `tool check raft` and `targets.raft.check mutants` unless another command is named:

* The node compiles whole; the native step equals shen-jit on every `(state, event)` pair the simulations and the bounded searches reached, on the messages from non-members, and on the initial state of every server; a library compiled from an edited specification is rejected by the same comparison.
* On the seeds and the five profiles of section 3, with every fault kind occurring and with the faults spread over the runs, the fourteen invariants hold and every client history is linearizable.
* Each of the 22 mutants is rejected on a retained seed with a shrunk trace. Every invariant rejects at least one of them. On each retained seed and schedule the specification passes.
* Figure 8 (`commit-by-counting`) and the lost vote (`vote-before-durable`) each violate a published property on a retained seed of a biased profile, as do five further mutants.
* In the bounds of section 7, Maude on the translated Shen and the simulator on the native code reach the same observations, all of them safe, and eight mutants are each unsafe in one of them.
* After faults stop, the pending put is answered and the cluster is whole within the declared bound on every seed tried.
* By the TLC command: the published `raft.tla` satisfies Election Safety, Leader Append-Only, Log Matching and State Machine Safety in the bound of section 8, at which each of the four rejects an edit of it; Leader Completeness rejects an edit at a larger bound.

Not established:

* **Figure 8 and the lost vote under the general profiles.** `three` and `five` do not produce them: 800 and 200 seeds for the first, 400 and 200 for the second. They are found under `churn` and `amnesia`, which were written to produce them. Whether an untuned fault model of this simulator would find them in any practical number of seeds is not known.
* **The consequences of three edits by search.** For `stale-ack-counted`, `candidate-asks-before-durable` and `entry-before-cut` seeds found the broken rule only (section 5). A violation of a published property is shown for them by hand-written schedules only. A hand-written schedule shows that the checks reject the history when it occurs; it says nothing about how likely the simulator is to produce it.
* **Biased profiles of five servers.** There is none; on five servers the two required consequences are not found.
* **Anything beyond the seeds, bounds and pairs listed.** The evidence is differential and sampled. Nothing proves that the compiler preserves meaning, that the node is safe on schedules not run, or that the liveness bound holds in general.
* **Faults the simulator does not have.** A paused node (a process that stops for longer than an election timeout and resumes with its memory) is not a fault kind; late messages and a stalled disk are the nearest things. A lost or unreadable row on a disk that otherwise works is outside the environment rules; the node's answer to it is the role `failed`, which no run of the specification in the campaigns reaches and which is exercised only through the two `length-before-entry` mutants and `recovery-never-completes`.
* **Replay and determinism for every trace.** Every tenth trace is replayed and re-run; the others are produced once.
* **History properties in the bounded search.** Leader Append-Only, Leader Completeness and most of the rules are checked on simulator traces only. The per-world predicates of the bounded search are Python restatements, not the Shen invariants.
* **Agreement of Maude and the simulator beyond these bounds.** They are two hand-written models of one environment. Timed behaviour, late messages, partitions and one-way cuts are not in the Maude model.
* **Leader Completeness on the published `raft.tla`, and a relation between `raft.tla` and this node.** See section 8.
* **Randomized election timeouts.** The timeout is a fixed function of server and term plus the simulator's skew. A real deployment needs a source of randomness, which would be a new effect role.
* **Defence against a stale completion.** The node relies on the rule that no acknowledgement, read result or timer crosses a crash, as AcceptedJob does; the simulator has no profile in which one arrives.
* **Client behaviour.** The simulated clients send to servers that believe they lead, which the workload reads from the simulator's state. A real client learns that from redirects, which this node does not send; a request to a server that is not a leader is dropped silently.
* **Capacity.** With a log of 8 entries and no compaction a cluster stops taking requests after 8 entries; a `full` answer occurred in 1 of 150 and 0 of 60 runs.
* **TLC in the flake check.** The two retained commands are `checks.<system>.raft` and `checks.<system>.raft-mutants`. The TLC command is not a flake check: it took 6 minutes 11 seconds run alone and 10 minutes 8 seconds beside eight other jobs, which is over the ten-minute bound, and TLC is not in the development shell.
* **Solo5 beyond seven scenarios.** Three servers run as three unikernels under `solo5-spt`, with a test harness as the network between them, in seven scripted scenarios of five runs each; every run is shown to be a simulator trace on which the invariants and the linearizability check of this file hold. See [tool/solo5/README.md](../../tool/solo5/README.md#a-cluster) for what that leaves out: no reproducibility from a seed, no skewed clocks, no lost writes, a client that reads the servers' consoles, and one of three mutants tried there not found.
