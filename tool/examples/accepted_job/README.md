# AcceptedJob with the commit boundary inside the specification

[spec.shen](spec.shen) is the whole node. A submission computes a write set of one row and emits a disk-write effect. Only the later disk acknowledgement lets the node answer. A crash loses memory; on `restart` the node reads every slot back before serving requests. There is no SQLite and no transaction code outside the specification: the simulator's disk only stores rows, syncs them in order and loses pending ones at a crash.

```sh
nix develop -c python3 -m tool check accepted_job    # check against retained evidence
nix develop -c python3 -m tool record accepted_job   # regenerate mutants.json and mutants/
```

## What the check shows

* **Simulation.** 500 seeds with up to two crashes, two drops and one duplicate, six submissions each. Every seed reruns to identical trace bytes and 50 traces replay from their records. Native equals shen-jit on every distinct `(state, event)` pair the runs reach. The four invariants in [invariants.shen](invariants.shen) hold on every trace.
* **Required histories** are witness predicates in the same Shen file, counted over the seeds:
  a reply `ok R` is dropped, the client retries, the retry is answered `ok R` and one row is durable;
  a submission reaches the node, the node is then down with nothing durable, and a retry creates the job;
  a submission with the other digest is answered `conflict`.
* **Bounded search.** One key, two digests, two submissions, one crash, one dropped message. Maude and `sim.explore` reach the same observations; every one is safe, and each required history is reachable.
* **Mutants.** Three one-line edits of the specification ([check.py](check.py) `MUTANTS`): reply before the acknowledgement, insert without looking up the key, reload that ignores the durable row. For each, [mutants.json](mutants.json) retains a seed, the invariant it violates and the SHA-256 of its trace, and [mutants/](mutants/) retains a 1-minimal trace. The check rebuilds the mutant, reproduces the seed's trace byte for byte, replays the minimal trace, confirms that removing any one of its steps loses the violation, confirms the unedited specification passes the same seed, confirms the refinement check rejects the mutant binary against the unedited source, and confirms the bounded search reaches unsafe observations.

## Scope

* One node, one idempotency key, two digests, a table of two receipts. The crash points covered are before the write is issued, between the write and its acknowledgement (with the row either lost or made durable without an acknowledgement), and between the acknowledgement and the reply.
* A request that arrives while the table is loading, or while another write is in flight, is not answered. The client retries. Nothing here shows that a request is eventually answered.
* "Minimal" means 1-minimal under step removal, not shortest.
