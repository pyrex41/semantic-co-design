# tool/solo5: the effect shim, on Solo5 and as a host process

The kernel compiles a Shen specification to `node_step`. The simulator is one driver of that function. This directory is a second one: a C event loop written against the Solo5 API, which turns device results into events and the step's effects into device operations. The same loop is linked twice, into a Solo5 unikernel and into a host process, and a recorded run of either is checked by turning it back into a simulator trace.

```sh
nix develop -c python3 -m tool check solo5-host   # host-process driver against the simulator
nix develop -c python3 -m tool check solo5        # that, and the unikernels under solo5-spt
nix develop -c python3 -m tool check solo5-cluster-host   # three Raft servers as three host processes
nix develop -c python3 -m tool check solo5-cluster        # that, and three unikernels under solo5-spt
```

`solo5` builds Solo5 and the unikernels with a Nix derivation and runs them on Linux. On macOS that happens in a Linux container (see [Where Solo5 runs](#where-solo5-runs)); without one the command fails and prints the command it could not run. `nix flake check` runs `solo5-host` as `checks.<system>.solo5-host`.

The last line of `solo5` is `solo5: ok` only when the command itself ran Solo5. Given records made elsewhere (`SOLO5_RECORDS`), it ends `solo5: ok (judged from the records in SOLO5_RECORDS; Solo5 was NOT run by this command)`.

## Files

| File | What it is |
|---|---|
| [driver.c](driver.c) | The event loop. Includes only `solo5.h` and the generated tables. |
| [host.c](host.c) | The ten Solo5 calls the driver uses, over a file and a datagram socket. Not Solo5. |
| [include/solo5.h](include/solo5.h) | The Solo5 header at the pinned commit, for the host build. The Nix build compares it byte for byte with the pinned source and compiles the unikernel against the installed one. |
| [freestanding/](freestanding/) | What `node.c` needs with no C library: `sigsetjmp` from the compiler builtins, four `mem*` functions, empty headers. |
| [emit.py](emit.py) | Manifest to `node.c`, `node_tables.h` and the Solo5 device manifest. |
| [run.py](run.py) | Starts a driver, plays a scripted client, kills it, records console, frames and disk image. |
| [compare.py](compare.py) | A record as a simulator trace, or the reason it is not one. |
| [scenarios.py](scenarios.py), [mutants.py](mutants.py) | The images, the scripted runs, the broken drivers and specifications. |
| [cluster.py](cluster.py) | Starts several nodes, each on its own link, and is the switch between them and their client. Records every frame and what it did with it. |
| [cluster_compare.py](cluster_compare.py) | The record of a cluster as one simulator trace, or the reason it is not one. |
| [cluster_scenarios.py](cluster_scenarios.py), [cluster_check.py](cluster_check.py) | The Raft images, the cluster runs, the broken drivers and specifications, and the check. |
| [nix/](nix/) | Solo5, the images and the runners as Nix derivations. |
| [container.py](container.py) | Runs the Nix build and the runner in a Linux container. |
| [timer/](timer/) | A node that only arms, cancels and reads the clock, to exercise those paths. |
| [store/](store/) | A node that reads on request and writes and reads in one step, to exercise the disk paths AcceptedJob and the simulator's probe do not reach. |

## What the driver does

One loop, one event at a time:

1. Take the next event: the oldest completion of the node's own effects, else a timer that is due, else a frame from the network device. With nothing to take, `solo5_yield` until the next timer or a frame.
2. Call `node_step(state, event)`.
3. Perform the returned effects in order.

| Effect role | Solo5 call | What is queued as an event |
|---|---|---|
| `disk-write Token Key v...` | `solo5_block_write` of block `Key`: the word 1, then the values | `[ack Token]`, once the call has returned |
| `disk-read Token Key` | `solo5_block_read` of block `Key`. Refused, and the node stops, when the same step wrote block `Key` earlier (see [below](#a-read-of-a-row-the-same-step-wrote)) | `[result Token Key Found v...]`; a block of zeros is a missing row |
| `output ...` | `solo5_net_write` of one broadcast frame, ethertype 0x88B5: a count, the tag code, the fields | nothing |
| `send Dest ...` | `solo5_net_write` of one frame to the address of node `Dest`, from this node's address: a count, the tag code of the effect's `deliver` event, this node's number, the payload. `Dest` outside the cluster stops the node | nothing |
| `timer-set Id Delay`, `timer-cancel Id` | an entry in a table of 16; `solo5_yield` to the earliest | `[timer Id]` when `solo5_clock_monotonic` reaches it |
| `clock-read Token` | the `solo5_clock_monotonic` reading taken when the step's event was dequeued, in milliseconds | `[clock Token Now]` |

| Event role | Source |
|---|---|
| `restart` | boot, after `node_init` |
| `request` | a frame of the same layout whose tag is a request event and whose fields are of the declared kinds; any other frame is logged and ignored |
| `recv` | a frame of that layout whose tag is a recv event, whose fields are of the declared kinds, and whose destination is this node's address; one addressed elsewhere is logged (`@skip address`) and ignored |
| `disk-ack`, `disk-read-result`, `timer`, `clock` | above |

The address of node `N` is `02:53:35:00:00:NN`, and a node knows its own number from `node=` on the boot command line. This is the simulator's convention and no other: the first field of a `send` is the destination, and the receiver's event has the sender in that place ([tool/README.md](../README.md)). In the simulator the network fills in the sender. Here the sending driver writes it into the frame, so the receiver believes what the frame says, and it is the link between the nodes that has to keep a node from speaking under another's number. The cluster harness checks every frame for it; nothing in the driver does. `emit.py` refuses a `send` or `recv` whose first field is not a number. The address has one byte for the node, so a cluster has at most 256.

The loop holds no request table, repeats nothing and reorders nothing. Three choices in it are policy and are stated here because a different shim could choose otherwise: completions are handled before timers and timers before frames; a clock reading is taken once per event; and whatever the driver cannot do stops the node (`solo5_abort`, exit status 255). Stopping is a crash before the effect that could not be performed, which a specification already has to survive, and the comparison maps it onto exactly that simulator crash.

| The node stops when | Console | Exercised by a scenario |
|---|---|---|
| a step has no result | `@trap CODE` | yes; the code is compared with the simulator's |
| a block write fails | `@fail block-write` | yes |
| a block read fails | `@fail block-read` | yes |
| a row key is outside the device | `@fail disk-key` | yes |
| a step reads a row it wrote | `@fail read-after-write` | yes |
| a seventeenth timer is armed | `@fail timer-table-full` | yes |
| a send names a destination outside the cluster | `@fail send-destination` | **no**; and the simulator ends such a run with an error, not a crash |
| a block holds neither zeros nor a row | `@fail unreadable-row` | **no** |
| 64 completions are waiting | `@fail completion-queue-full` | **no** |
| a row is wider than a block, the manifest or the step is inconsistent with the tables, a device is missing at boot | other `@fail` | **no** |

Two things in the loop are for tests only. `hold=N.K` on the boot command line makes the driver stop for good after `K` effects of the `N`-th event, so that the harness can `SIGKILL` the tender at a known point instead of racing it. The console lines starting with `@` are the record of the run. Under `solo5-spt` a held boot used about 0.15 s of processor time in the recorded runs, against 0.01 s for the others and none as a host process. The likely cause, not investigated further, is that a held driver no longer reads the network, so a frame waiting on the tap makes every wait return at once until the harness kills it. The loop that runs outside tests reads every frame.

The wait for the next timer is computed in ticks. Computed in nanoseconds, a due time far enough away wraps 64 bits; one that wraps to a past instant makes every `solo5_yield` return at once and the unikernel spin a core without ever firing the timer. One request frame could do that to the driver as first written (kept as the mutant `wrapping-deadline`).

### A read of a row the same step wrote

The simulator separates issuing a write from the write becoming durable (`sync`), and `sync` is an action of its own: none happens between two effects of one step. So in every simulated schedule a step `[write K v] [read K]` reads the row as it was before the step. `solo5_block_write` is synchronous. The device would answer the read with `v`, a result no simulated run contains, and an invariant checked over every simulator schedule would say nothing about a node that saw it.

The driver therefore refuses: it performs the write, stops before the read, and the next boot restarts. That is the simulator's crash between the two effects, so the real run is again a simulated one. The price is that such a step never completes on this shim. A read of another row in the same step is not affected. The mutant `reads-own-write` is the driver without the refusal (the driver as it was first written) and its run is rejected.

The alternative, a simulator that lets a write become durable inside a step, would remove the refusal. It changes the simulator and the Maude environment and was not done; [tool/README.md](../README.md#simulator-limits) lists it.

## How a run is compared with the simulator

`run.py` records three things per boot: the console lines, the frames the client's socket received, and the rows in the block image after the process was killed. Beside them it notes what only the harness can know: the exit status, the processor time used, when it sent each request and when it read each console line on its own clock, the argument vector, the banner the process printed, and the SHA-256 of the image and the tender it executed. `compare.reconstruct` then scripts `tool.sim` with the same event sequence: the events the driver handled in the order it handled them, a crash where the process was killed or stopped itself (inside a step, with the number of effects performed), a restart for the next boot. The simulator computes everything else with its own library and its own disk and network. The record is accepted when

* every event the driver handled is queued in the simulator at that point, so a read result is what the simulated disk holds and an acknowledgement belongs to a write that was issued;
* the simulator's step gives the state and effects the driver logged, and the same number were performed;
* the frames the client received are exactly the outputs performed;
* the block image after every kill holds exactly the simulated disk's rows;
* every boot starts with one `@boot` line for node 0 of 1 on the block file given, a killed boot has exit status -9 and a boot that stopped itself has 255;
* for a node with a clock, the simulator's clock, moved by its own timers and by the ticks the driver logged, agrees with every logged tick;
* and those ticks are milliseconds of the harness's clock (`CLOCK_MONOTONIC`, read by the harness): a timer armed by a request fires no sooner after the harness sent that request than its delay, and between two events no more ticks pass than the time from sending the first to reading the second, and no fewer than from reading the first to sending the second. The tolerance is 2 ms and 1 %. Both bounds hold for a correct driver however long the harness or the driver is kept waiting in between, so a busy machine does not fail them. They assume the harness and the driver read the same clock, which is so for the host process and for `solo5-spt`.

The result is an ordinary `sim-trace/2` trace that replays, so the node's Shen invariants are evaluated on it unchanged.

The simulator separates issuing a write, the write becoming durable (`sync`) and the acknowledgement. The driver's block write is synchronous, so a real run uses one corner of that space, and the reconstruction places `sync` directly after the step. A kill or a stop inside a step maps to the simulator's crash with every write issued before the cut persisted.

That placement is the simulator's, not the device's. For a step that writes a row and then sends a packet, the reconstructed step shows the row still pending when the packet is made, although the device held it by then. The trace is a schedule the simulator has, and it ends in the same world; it does not say when within a step the device did what. See [reply-before-ack](#what-the-acknowledgement-is-worth-on-this-shim).

## What the checks establish

The commands print these counts, and since each scenario states how many events every boot handles, how it ends, and the frames and rows it leaves, the counts are asserted and not only printed.

**`python3 -m tool check solo5-host`** (macOS or Linux, no Solo5; about 90 seconds):

* 26 host processes are built from `driver.c`, `host.c` and each image's `node.c`: four nodes, and 22 images with one edit of the driver or of a specification. Each `node.c` has the SHA-256 of the one `tool.compile.build` compiled into the simulator's library.
* Every name of the `<setjmp.h>` family that a `node.c` uses is defined by [freestanding/setjmp.h](freestanding/setjmp.h). The host images are compiled against the system header, which has every spelling, so without this a change of the ABI template can pass every check that needs no container and still leave the unikernels uncompilable. That happened once, when the template took up `sigsetjmp`; the header as it was then is the mutant and is rejected for `sigjmp_buf`, `sigsetjmp` and `siglongjmp`. This reads the two texts; only the Linux build compiles them.
* 19 scenarios, 35 boots, 203 events. 29 boots end by `SIGKILL` (status -9) and 6 by the node stopping itself (status 255). 16 boots are restarts on the same block image. 11 deaths are inside a step: 6 kills and 5 stops. Every record reconstructs to a simulator trace that replays.
* AcceptedJob: killed after its block write returned and before any reply, restarted on the same image, the retry is answered `ok` with the same receipt and no second write. Also killed before the write (the retry writes) and after the acknowledgement before the reply (the retry does not). `conflict` and `full` are reached. The same request sent three times in one boot reaches the step three times. The five Shen invariants of `examples/accepted_job/invariants.shen` hold on the six reconstructed runs, and in none of the six does a client hold `ok` while the block image lacks the row (read from the frames and the image, without the simulator).
* A device that refuses a write (see [below](#device-failures)): the node stops without acknowledging, the client hears nothing, and the next boot accepts the retry. A read the device fails, and a row outside the device, stop the node the same way.
* A step that writes a row and reads it stops before the read; a step that writes one row and reads another completes.
* The simulator's probe node is killed after 0, 1, 2, 3 and 4 of the four effects of one step; image and frames show exactly that prefix each time.
* A step without a result stops the node with the error code the simulator's library gives, and the simulated run ends in that error.
* Timers fire in due order, a re-armed timer fires once, a cancelled timer, a timer armed before a kill and sixteen timers armed before a stop do not fire. Timer 2, armed with 400 ms, fired 401 to 404 ms after the harness sent its request in the runs made for this document. Two timers due far away, one of them at a due time whose nanoseconds wrap 64 bits, cost 0.00 s of processor time in 3 s.
* Five frames that are not requests never reach the step.
* Teeth, on the record: 18 single edits of the record of the kill between write and reply are each rejected, each with the message that belongs to the edit (a row missing from, changed in or added to the image; a reply missing or changed at the client; a request not sent; a write, a state, a key, a receipt, a hold point, a read result or an initial state changed on the console; a boot left out; the boots reordered; an exit status that is not a kill; the `@boot` line missing or naming another block file). A record whose harness clock says a timer fired 300 ms early is rejected. 4 edits of what a record says was executed (image hash, kind of driver, program, banner) are rejected.
* Teeth, on the driver: 18 edits of `driver.c`, built as 19 images, and 2 edits of AcceptedJob, each rejected for its own defect:

  | Defect | Mutant | Rejected because |
  |---|---|---|
  | acknowledges without writing; a torn row; dies one effect later than reported (two nodes) | `ack-without-write`, `torn-row`, `late-cut` | the image does not hold the simulated rows |
  | acknowledges a write the device refused | `acks-failed-write` | the client holds `ok 1` and the image has no such row |
  | a read misses the row; a failed read returns the buffer; a read sees the row its own step wrote | `stale-read`, `ignores-failed-read`, `reads-own-write` | the simulator has no such read result |
  | discards the new state; does not send an output; sends every output twice | `keeps-old-state`, `swallows-output`, `repeats-output` | state, or frames at the client, differ |
  | passes an out-of-range field to the step; drops a request equal to the last one | `accepts-any-field`, `dedupes-requests` | requests handled are not the requests sent |
  | fires a timer early; ignores a cancel; a stale clock | `early-timer`, `ignores-cancel`, `stale-clock` | the simulator's clock or its queue disagrees |
  | a tick of half a millisecond; of two | `half-tick`, `slow-tick` | the harness's clock: a 400 ms timer fired after 202 ms; 77 ticks in at least 154 ms |
  | a wait computed in nanoseconds | `wrapping-deadline` | 0.81 s (host) or 3.47 s (spt) of processor time in 3 s of waiting |
  | AcceptedJob replies, then writes | `reply-then-write` | see the next section |
  | AcceptedJob writes and replies in one step | `reply-before-ack` | see the next section |

**`python3 -m tool check solo5`** adds, on Linux:

* Solo5 v0.9.3 (`38e0c348`) and 27 unikernels (26 for spt, AcceptedJob also for hvt) are built by `nix-build tool/solo5/nix/pinned.nix -A solo5-run`. `solo5-elftool` reports the ABI and the device manifest of each. Each `node.c` and `driver.c` hash equals this checkout's.
* The same 19 scenarios, record edits and 21 mutant images under `solo5-spt`, with the block device a file and the network device a tap interface reached through an `AF_PACKET` socket, and again as Linux host processes in the same container. Same verdicts and the same counts. Every kill is a `SIGKILL` of the `solo5-spt` process; every stop is `solo5_abort` in the guest, and the tender exits with 255.
* Every boot recorded as a unikernel is a `solo5-spt` command line naming the `.spt` image, and printed `Solo5: Bindings version v0.9.3`. The host process's records are not accepted in their place: not as they are, not relabelled, not with the image and tender hashes copied from the real records.
* For the 15 scenarios of nodes without a clock, the unikernel, the macOS host process and the Linux host process reconstruct to byte-identical simulator traces.

### What the acknowledgement is worth on this shim

`solo5_block_write` returns after the tender's `pwrite`. An effect that follows a write in the list a step returned therefore runs with the row already on the device. On this shim, waiting for the acknowledgement adds nothing over putting the write first.

Two specifications that answer too early show the difference, on the unedited `driver.c`:

* `reply-then-write` returns `[reply ok] [write]`. Killed after one effect, the client holds `ok 1` for key 8 and the block image has no row for key 8. That is in the frames and the image, on the host process and under `solo5-spt`. `inv.ok-is-durable` fails on its trace. This is the guarantee moving with the specification while the C stays the same.
* `reply-before-ack` returns `[write] [reply ok]`. No kill point leaves `ok` without its row. The retained run kills it after the first of its two effects: the image has the row and the client nothing, and the check asserts that no `ok` anywhere in that run lacks its row. (Killed after both effects, in a run made once on the host process and not retained, the client has `ok` and the image the row.) Its run is rejected against the unedited node because the effects differ, and `inv.ok-is-durable` does fail on its reconstructed trace, but only because the simulator places `sync` after the step. **That failure is a property of the simulator's model, not an observation of this run.** A shim whose writes complete later (a write cache, an asynchronous device) would make it real; this one cannot show it.

### Device failures

The scenarios make a block operation fail in two ways. A write is refused by giving the running process a file size limit of zero with `SIGXFSZ` ignored (`prlimit` on the `solo5-spt` process after boot; before exec for the macOS host process), so `pwrite64` returns `EFBIG`. A read is failed by truncating the block file to one block under the running process and reading a later block, so `pread64` comes back short. Both make `solo5_block_write` or `solo5_block_read` return an error, under `solo5-spt` as in the host process.

The simulator has no failed operation. The driver's answer to one is to stop, and a stop is reconstructed as a crash before that effect. Nothing here covers a device that fails silently, returns wrong data, or tears a row.

## A cluster

`tool check solo5-cluster-host` and `tool check solo5-cluster` run three Raft servers ([targets/raft](../../targets/raft/README.md)), each the same image booted with `node=0`, `1` or `2` and `nodes=3`, each with its own block image and its own link. The link of a host process is a directory of datagram sockets; the link of a unikernel is a tap interface (`tap0`, `tap1`, `tap2`). No bridge joins the links. The harness ([cluster.py](cluster.py)) holds the other end of each, so a frame gets from one node to another only if the harness reads it from one link and writes it to another. It is the switch and it is the client.

**What the harness does to a frame between nodes**, decided from the scenario's seed when the frame arrives: forward it after a delay drawn from a range; drop it; forward it twice, each copy with its own delay; or cut it, because a partition separates the two nodes at that moment. With a small probability a forwarded frame is also written to the link of the third node, which it is not addressed to, as a switch that has not learned an address floods. It kills a node with `SIGKILL` and boots it again on the same block image. As the client it sends each put or get to whichever node last said on its console that it leads the highest term, and sends it again under the same request number while there is no `ok`.

**The record.** In the order the harness did them: every frame it read from a link and what it decided, every frame it wrote, every request, boot, kill, cut and heal, each with the harness's clock. Per node and boot: the console lines and when each was read, the exit status, the argument vector, and the rows in the block image after the kill.

**A run is not reproducible from its seed.** The seed fixes the harness's decisions per frame, in the order frames arrive. When a timer fires, how long a step takes and in which order two nodes' frames arrive are the machine's. Two runs of one scenario and seed differ. What is checked is the record of the run that happened, and it is the reconstructed trace that replays, byte for byte, in the simulator.

### How a cluster record is checked

[cluster_compare.py](cluster_compare.py), in three layers:

1. **Each node alone.** Every `@step` line on a node's console is the compiled step applied to the state the console showed before it and the event of the `@event` line. This needs no simulator and no other node.
2. **Consoles against the harness's log**, without the simulator. The frames read from a node's link are, in order, exactly the sends and outputs its console says it performed: a send to node `D` is a frame to `D`'s address, from the node's own address, carrying the `deliver` event with the node as sender. Every request or message a node handled is a frame the harness had written to that node's link, addressed to that node. A node skipped no more frames for their address than the harness wrote to its link for another node.
3. **One simulator trace.** Everything is put in one order that respects every "before" the record establishes (a console's order, the harness's order, a frame is sent before the harness reads it and written before the node handles it, a boot's steps lie between its boot and its kill) and is otherwise by the clock, which harness and nodes share. `tool.sim` is scripted in that order with three nodes. It computes each step with its own library, disks and network, and the record is accepted when every event a node handled is queued for that node in the simulator at that point, every step gives the state and effects on the console, the simulator's clock agrees with every tick (so no timer fired early), and after every kill the block image holds the simulated disk's rows.

| In the run | In the trace |
|---|---|
| a node handles a request or a message | `deliver` |
| a node handles a completion or a timer | `local` |
| a block write returned | `sync`, straight after the step |
| the harness read a reply | `output` |
| the harness sent a request | `client` |
| the harness dropped a frame, or cut it | `drop` |
| the harness forwarded a frame twice | `dup` |
| `SIGKILL` | `crash`; inside a step, the step with `crash-after` = effects performed |
| the next boot | `restart` |

Two things in this mapping are weaker than the run. A cut is a `drop` and not the simulator's `partition`: the harness decides when a frame arrives and the simulator when the step sends, so a frame in flight when a cut starts or ends would be judged differently by the two. And a frame the harness forwarded that the node never handled (it was down, or was killed first) is left undelivered in the simulator; it is not shown that the node could not have handled it.

The trace is then judged by the Raft target's own checks, unchanged: `targets.raft.check.judge`, which evaluates the fourteen Shen invariants of [invariants.shen](../../targets/raft/invariants.shen) in shen-jit and runs the linearizability checker [lin.py](../../targets/raft/lin.py) on the client history. An operation is invoked where the harness sent the request and returns where the harness read the reply, so the real-time order the checker uses is the harness's.

**A kill inside a step.** A node killed between an effect and the `@did` line that logs it has performed one effect more than its console says. The comparison counts that effect when the evidence shows it: one frame more on the node's link than its console accounts for, or the row in its block image. A timer set or cancelled leaves no evidence and makes no difference after a death. The harness's ordinary kills land where they land, and in the runs below every one of them landed between two steps. One scenario therefore boots a node with `hold=N.K`, so that it stops after `K` effects of its `N`-th event and is killed there, as the single-node scenarios do.

### Scenarios

Seven, in [cluster_scenarios.py](cluster_scenarios.py). They name roles (the leader, a follower, the node that is down), never a server number or a term. All of them have the harness misdeliver 5 % of the frames (10 % in the lossy ones) and delay each by up to 4 ms (15 ms).

| Scenario | What happens | What it must show |
|---|---|---|
| `elect-and-serve` | a leader is elected; put, get, put, put, get | all five answered; the gets return 11 and 12 |
| `kill-the-leader` | two puts, the leader is killed, a later term gets a leader; get, put, get, get | all six answered; the first get returns the value acknowledged before the kill |
| `restart-from-disk` | a put, a follower is killed, a put, the follower is booted on its disk, the leader is killed; two gets | all four answered: the two servers left, one of them the restarted one, are the whole majority |
| `kill-inside-a-step` | as above, and the follower's first boot after the kill stops inside its recovery or its catching up and is killed there; it is booted a third time | the same |
| `partition-heals` | a put, the leader is cut off, the others elect a leader of a later term and take a put, the cut ends; a get | all three answered; the get returns the later value |
| `lossy-links` | `elect-and-serve` with 10 % of the frames dropped and 10 % doubled | as `elect-and-serve` |
| `lossy-kills-and-cuts` | lossy links, the leader killed, the dead node booted, a leader cut off, the cut healed | safety only: no operation has to be answered |

The Raft log holds eight entries and every operation and every repeat of one is an entry; the workloads have at most six operations.

CLUSTER_RESULTS

## Where Solo5 runs

`~/projects/fenrir` pins Solo5 v0.9.3 (`38e0c348`) and builds it from source in its flake (`mkSolo5`: the spt and hvt tenders, `solo5-elftool`, the `<arch>-solo5-none-static-cc` wrappers) for x86_64-linux and aarch64-linux. It runs spt guests on Linux, including Linux/AArch64 inside the Docker server of this Mac from an Alpine image. Its hvt comparison needs `/dev/kvm` and its README says it has not been executed.

This directory reuses the commit and the recipe ([nix/default.nix](nix/default.nix)) with this repository's nixpkgs. nixpkgs' own `solo5` 0.10.1 is cached for x86_64-linux only; on aarch64-linux it does not evaluate. So Solo5 itself is compiled (a few seconds); its compiler and libraries come from the cache.

What this ARM64 Mac can do, each observed:

| | Outcome |
|---|---|
| `nix build .#packages.aarch64-linux.solo5-images` on macOS | Fails: `Cannot build ... solo5-target-cc.drv. Reason: platform mismatch. Required system: 'aarch64-linux'. Current system: 'aarch64-darwin'`. No Linux builder is configured. |
| The same derivations by `nix-build` in a `nixos/nix` container (Linux 7.2 aarch64 under OrbStack) | Built. |
| `solo5-spt` in that container | Runs. This is the real tender and the real unikernel on a Linux kernel; the virtual machine under the container is the Mac's. |
| `solo5-hvt` in that container | Not run: there is no `/dev/kvm`. The check prints the tender's own message. The hvt image is linked and its ABI note read, nothing more. |
| virtio, muen, xen targets | Not built, not run. |

The container is the pinned `nixos/nix` image with `/dev/net/tun` and `CAP_NET_ADMIN`, and it needs the network for the Nix binary cache. Its Nix store is kept in the Docker volume `sim-tool-solo5-nix` (about 1.5 GB) so that a second run downloads nothing and takes about two and a half minutes; `docker volume rm sim-tool-solo5-nix` removes it. A first run has to fetch a compiler and may not finish within the check's 580 seconds, in which case the check reports blocked and a second attempt continues from the volume.

`SOLO5_DOCKER` names another container command than `docker`. With none on the path the check fails with `solo5: blocked: no ... on PATH`.

On Linux with Nix no container is needed: `nix build .#solo5-run`, run `result/bin/sim-tool-solo5-run OUT` as root, then `SOLO5_RECORDS=OUT python3 -m tool check solo5`. That path was exercised only with records produced in the container.

**What ties a record to what ran.** The runner writes, and the check compares:

* the SHA-256 of `run.py`, `scenarios.py`, `mutants.py`, `emit.py`, `driver.c` and `host.c` as the runner executed them, against this checkout;
* the SHA-256 of every image's `node.c` and `driver.c`, against the ones generated here;
* the SHA-256 of every unikernel from the build (and of every Linux host process, taken by the runner), against the hash `run.py` took of the file it executed for each record;
* the SHA-256 of the `solo5-spt` binary, against the hash `run.py` took of the tender it executed; the check prints it;
* per boot, the argument vector and the lines the process printed outside its record: a unikernel's boot is a `solo5-spt` command and printed the Solo5 banner, a host process's is neither.

This catches records of another checkout, another build, another driver, or the host process passed off as the unikernel. The host process's records in place of the unikernel's, and an edit of each of the last three entries, are retained checks. Records made by an earlier `run.py` were tried once and rejected for it. It is not authentication: nothing is signed, and whoever writes the directory can write all of it. `SOLO5_RECORDS` moves the trust to whoever made the records, and the check's last line says so.

**Reproducibility.** The image derivation rebuilds to the same bytes in the container: `nix-build tool/solo5/nix/pinned.nix -A solo5-images --check` exits 0 (run twice). The Solo5 derivation does not: `nix-build ... -A solo5 --check` exits 104, "may not be deterministic". Of its 59 files, four differ between two builds: `bin/solo5-spt`, `bin/solo5-hvt`, `bin/solo5-hvt-debug` and `bin/solo5-elftool`. The guest bindings, which are what is linked into the images, are identical. So a unikernel's hash names a build that was repeated here, while the tender is input-addressed: its store path names the recipe, and the hash the check prints names the one binary that ran. That is why the hash of the tender is recorded with every run and the hashes of the host processes are taken by the runner and kept out of the image derivation (a host process links against its own output path).

## What carries over from simulation, and what does not

An invariant checked over simulator traces says something about a real run only if the real run is a run of the simulator. That is what the comparison shows, for the scenarios above and for no others. Per environment rule of [tool/README.md](../README.md#environment-rules):

| Rule the specification may rely on | Under `solo5-spt` (and the host process) | Evidence |
|---|---|---|
| A node that dies during a step performed exactly the effects before the point of death, in order | Holds for death of the tender process: effects are performed one call at a time in the returned order. | Kills after every prefix of four effects; `late-cut` rejected. |
| No completion crosses a crash | Holds: completions and timers live in guest memory. | Timer armed before a kill does not fire; acknowledgement lost at the kill between write and reply. |
| A read sees durable rows only | **Holds by refusal.** A read sees every write that has returned. For writes of earlier steps that is the simulator's schedule in which each step is followed by its syncs. For a write of the same step the simulator has no such schedule, so the driver does not perform the read: the node stops there ([above](#a-read-of-a-row-the-same-step-wrote)). A specification with such a step is safe on this shim and cannot make progress through that step. | The store's two scenarios; `reads-own-write` and `stale-read` rejected. |
| A disk row is written whole or not at all; writes become durable in order; a crash keeps a prefix | **Only against death of the guest or the tender.** | See below. |

**What a block write returning means.** In `solo5-spt` the guest's `solo5_block_write` is one `pwrite64` of one block on a file the tender opened `O_RDWR`. In `solo5-hvt` it is a hypercall and the tender does the same `pwrite`. Neither tender opens the file with `O_SYNC` or `O_DIRECT`, neither calls `fsync`, and the spt seccomp filter allows the guest only `pread64`, `pwrite64`, `read`, `write`, `epoll_pwait`, `timerfd_settime`, `clock_gettime`, `arch_prctl` and `exit_group`. The Solo5 block API has no flush. (Read from the pinned source; the virtio bindings define a flush request and never send one.) So when the write returns, the row is in the host kernel's page cache:

* It survives the guest exiting, aborting or being killed, and the tender being killed. This is the crash the scenarios inject, and for it the acknowledgement means what the simulator's `sync` means.
* It does **not** survive the host losing power or its kernel crashing before writeback. Then an acknowledged row can be gone, later rows can be present without earlier ones, and nothing here says a 512-byte row cannot be torn. `inv.ok-is-durable` shown in simulation is therefore, on Solo5, "an `ok` reply implies the row survives the death of the unikernel", and nothing stronger. No specification can do better on this API; a shim could, only by adding a flush that Solo5 does not offer.

**What does not carry over at all.**

* **Coverage.** The simulator explores schedules; a real run is one schedule. Everything checked about Solo5 is checked for 19 scripted runs: each of them is shown to be a simulator run. That *every* run of this driver is one is an argument, not a check: effects are performed in order, completions are handled first, a synchronous write is the schedule with `sync` after the step, and the one place where that argument failed (a read of a row the same step wrote) is refused. Another such place may exist.
* **Stops count as crashes.** A node that stops itself is covered by simulation only as a crash, and a simulation is run with a bounded number of crashes. A device that keeps failing, or a step the driver always refuses, stops the node at every boot; nothing here says the node then makes progress.
* **Kill points.** The process is killed only where the driver holds or is idle. A kill at an arbitrary instant, including inside a `pwrite64`, is not exercised. Two crashes during one recovery are not exercised.
* **The first boot's restart step** cannot be cut: the simulator's boot has no crash. A record in which the node dies or stops there is rejected, not explained.
* **One tender per block image.** The simulator has one node per disk, and its crash is certain death. Solo5 does not lock the block file: `tenders/common/block_attach.c` of the pinned source opens it with `open(path, O_RDWR)` and nothing else. Two tenders attached to one image (a supervisor that starts a replacement while the old tender hangs and is not dead) are two nodes on one disk. Each reads the table once at boot and then overwrites the other's rows, so an acknowledged job can be replaced and its key accepted again under another receipt; `inv.rows-are-immutable` and `inv.ok-is-durable` say nothing about it. A reviewer ran exactly this under `solo5-spt` and saw it; it is not a retained scenario. Single attachment is an assumption the operator has to enforce, for example with `flock` in the launcher. Neither Solo5 nor a specification can.
* **The network.** In the 19 single-node scenarios: one client, one link, no loss, no duplication, no reordering observed or injected. In the cluster runs the only network is the harness, and loss, duplication, delay and cuts are what it chose to do; see [A cluster](#a-cluster) for what that does and does not show. Nodes joined by a real bridge or a real switch were not run.
* **Time.** A timer armed by a request is shown, on the harness's clock, to fire no earlier than its delay; how late is not bounded. A timer armed by any other step (a restart, a completion, another timer) has no send time to measure from and is checked only in the driver's own ticks; the rate of those ticks is checked against the harness's clock in both directions. Under spt the clock is the host's `CLOCK_MONOTONIC` and keeps going across a restart. Under hvt (read from the source, not run) it starts again at each boot, so a specification that compares clock readings across a restart is wrong there. The simulator's clock never goes back.
* **Device failures** are a refused write and a short read, each injected once. See [Device failures](#device-failures).
* **Same code.** The unikernel and the simulator's library are compiled from the same `node.c` by different compilers for different targets (gcc, freestanding, aarch64 ELF; clang, Mach-O). The comparison covers the steps the scenarios reach: 203 events, 117 distinct (state, event) pairs.
* **Several nodes** only as in [A cluster](#a-cluster): three Raft servers, seven scenarios, the harness between them.
* **Trust.** The harness reads the console, the frames, the image, the exit status and its own clock. A driver that lied consistently on the first three would pass; the mutants each lie on one. A directory of records is trusted as a whole (see [Where Solo5 runs](#where-solo5-runs)).

## The three cheats

1. No tabulation: `node.c` is the specializer's output, the same text the simulator loads.
2. No hand staging: the specifications are the kernel's `spec.shen` files, plus [timer/spec.shen](timer/spec.shen) and [store/spec.shen](store/spec.shen), ordinary Shen.
3. Semantics in the shim: the C does block reads and writes, frames, a timer table and a clock, in the order the step returns them. It has input validation (a frame must be a declared request with fields of the declared kinds) and it stops the node on what it cannot do, which includes one thing it could do and refuses (a read of a row the same step wrote). It has no retry, no dedupe, no ordering and no recovery logic. That claim has teeth for dedupe and retry: `dedupes-requests` (a loop that drops a request equal to the last one) and `repeats-output` (a loop that sends each output twice) are rejected by a scenario that sends one request three times. It has none for ordering or recovery beyond the comparison of every step's effects and state. The guarantee moves with the specification while the C stays the same in `reply-then-write`; it does not visibly move in `reply-before-ack`, for the reason given [above](#what-the-acknowledgement-is-worth-on-this-shim). The `hold` point and the `@` console lines are test instrumentation that ships in the same binary, and the kill they enable is a real `SIGKILL` of the tender.
