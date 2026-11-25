# AcceptedJob with the commit boundary inside the specification

[spec.shen](spec.shen) is the whole node. A submission computes a write set of one row and emits a disk-write effect. Only the later disk acknowledgement lets the node answer. A crash loses memory; on `restart` the node reads every slot back before serving requests. There is no SQLite and no transaction code outside the specification.

What the specification takes from its environment instead of doing itself: the simulator's disk stores whole rows, syncs them in order and loses a suffix of the pending ones at a crash, **and it discards a dead node's queued acknowledgements and read results**. The safety below rests on that last rule. At most one write is in flight, so an acknowledgement can only be for that write, and the specification does not look at the acknowledgement's token; it has no defence of its own against a completion from before a crash. The check shows the dependence: in a simulator that delivers completions across a crash, the same binary answers `ok` for a row that is not durable. Any real effect shim has to provide this rule.

```sh
nix develop -c python3 -m tool check accepted_job    # check against retained evidence
nix develop -c python3 -m tool record accepted_job   # regenerate mutants.json and mutants/
```

## What the check shows

* **Simulation.** 500 seeds with up to two crashes, two drops and one duplicate, eight submissions each over three keys and two digests. Every seed reruns to identical trace bytes and every trace replays. Native equals shen-jit on every distinct `(state, event)` pair the runs reach. The five invariants in [invariants.shen](invariants.shen) hold on every trace.
* **Crash points** are counted from the world, not from what a record says about itself: a node died with its write not yet in the disk queue, with the write pending and then lost, with the write pending and then durable but unacknowledged, and after the acknowledgement with the reply not yet in flight. Under a simulator that performs every effect of a step it cuts, or cuts one effect late, the first and the last never occur.
* **Required histories** are witness predicates in the same Shen file, counted over the seeds:
  a reply `ok R` is dropped, the client retries, the retry is answered `ok R` and one row is durable for the key;
  a submission reaches the node, the node is then down with nothing durable, and a retry for that key is answered `ok`;
  a submission with the other digest is answered `conflict`;
  with both receipts taken, a third key is answered `full`.
* **Bounded search.** Three bounds, each compared between Maude and `sim.explore`, each all safe: two keys, two digests, two submissions, one crash, one dropped message; one key with a duplicated message as well; three keys for the two receipts with no faults, which is the only one that reaches `full`. Refinement and the Maude step check also cover the pairs these searches execute.
* **Mutants.** Eight one-line edits of the specification ([check.py](check.py) `MUTANTS`): reply before the acknowledgement, insert without looking up the key, reload that ignores the durable row, a lookup that ignores the key, a reply that always names receipt 0, `full` instead of the second receipt, `conflict` for the same digest, `ok` for another digest. Every declared invariant is the target of at least one. For each, [mutants.json](mutants.json) retains a seed, the invariant it violates and the SHA-256 of its trace, and [mutants/](mutants/) retains a 1-minimal trace. The check rebuilds the mutant, reproduces the seed's trace byte for byte, replays the minimal trace, confirms that removing any one of its steps loses the violation, confirms the unedited specification passes the same seed, confirms the refinement check rejects the mutant binary against the unedited source and the Maude step check rejects the mutant's translation against the unedited binary, and confirms the bounded search reaches unsafe observations. Two of the mutants must also make a witness impossible on all 500 seeds; the other two witnesses are shown not to be vacuous by runs without crashes and with two keys.
* **A reply is judged where it was made.** A node that answers `ok` while its row is only pending is caught even when it then dies and the crash happens to persist the row.

## Scope

* One node, three keys, two digests, a table of two receipts. The crash points covered are before the write is issued, between the write and its acknowledgement (with the row either lost or made durable without an acknowledgement), and between the acknowledgement and the reply.
* Safety assumes the environment rule above. Nothing here tests a specification that defends itself against stale completions, because the simulator has no profile in which they arrive.
* Every AcceptedJob step has at most one effect apart from the two reads at restart, so a crash between two effects of one step, sync order and a prefix of several pending writes are not exercised by this example. `tool check sim` exercises them on a probe node.
* A request that arrives while the table is loading, or while another write is in flight, is not answered. The client retries. Nothing here shows that a request is eventually answered.
* "Minimal" means 1-minimal under step removal, not shortest. A client submission that nothing uses usually cannot be removed, because removing it renumbers the later requests, so retained traces keep some idle submissions.
