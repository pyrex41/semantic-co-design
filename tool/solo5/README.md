# tool/solo5: the effect shim, on Solo5 and as a host process

The kernel compiles a Shen specification to `node_step`. The simulator is one driver of that function. This directory is a second one: a C event loop written against the Solo5 API, which turns device results into events and the step's effects into device operations. The same loop is linked twice, into a Solo5 unikernel and into a host process, and a recorded run of either is checked by turning it back into a simulator trace.

```sh
nix develop -c python3 -m tool check solo5-host   # host-process driver against the simulator
nix develop -c python3 -m tool check solo5        # that, and the unikernels under solo5-spt
```

`solo5` builds Solo5 and the unikernels with a Nix derivation and runs them on Linux. On macOS that happens in a Linux container (see [Where Solo5 runs](#where-solo5-runs)); without one the command fails and prints the command it could not run. `nix flake check` runs `solo5-host` as `checks.<system>.solo5-host`.

## Files

| File | What it is |
|---|---|
| [driver.c](driver.c) | The event loop. Includes only `solo5.h` and the generated tables. |
| [host.c](host.c) | The ten Solo5 calls the driver uses, over a file and a datagram socket. Not Solo5. |
| [include/solo5.h](include/solo5.h) | The Solo5 header at the pinned commit, for the host build. The Nix build compares it byte for byte with the pinned source and compiles the unikernel against the installed one. |
| [freestanding/](freestanding/) | What `node.c` needs with no C library: `setjmp` from the compiler builtins, four `mem*` functions, empty headers. |
| [emit.py](emit.py) | Manifest to `node.c`, `node_tables.h` and the Solo5 device manifest. |
| [run.py](run.py) | Starts a driver, plays a scripted client, kills it, records console, frames and disk image. |
| [compare.py](compare.py) | A record as a simulator trace, or the reason it is not one. |
| [scenarios.py](scenarios.py), [mutants.py](mutants.py) | The images, the scripted runs, the broken drivers and specifications. |
| [nix/](nix/) | Solo5, the images and the runner as Nix derivations. |
| [container.py](container.py) | Runs the Nix build and the runner in a Linux container. |
| [timer/](timer/) | A node that only arms, cancels and reads the clock, to exercise those paths. |

## What the driver does

One loop, one event at a time:

1. Take the next event: the oldest completion of the node's own effects, else a timer that is due, else a frame from the network device. With nothing to take, `solo5_yield` until the next timer or a frame.
2. Call `node_step(state, event)`.
3. Perform the returned effects in order.

| Effect role | Solo5 call | What is queued as an event |
|---|---|---|
| `disk-write Token Key v...` | `solo5_block_write` of block `Key`: the word 1, then the values | `[ack Token]`, once the call has returned |
| `disk-read Token Key` | `solo5_block_read` of block `Key` | `[result Token Key Found v...]`; a block of zeros is a missing row |
| `output ...` | `solo5_net_write` of one broadcast frame, ethertype 0x88B5: a count, the tag code, the fields | nothing |
| `timer-set Id Delay`, `timer-cancel Id` | an entry in a table of 16; `solo5_yield` to the earliest | `[timer Id]` when `solo5_clock_monotonic` reaches it |
| `clock-read Token` | the `solo5_clock_monotonic` reading taken when the step's event was dequeued, in milliseconds | `[clock Token Now]` |

| Event role | Source |
|---|---|
| `restart` | boot, after `node_init` |
| `request` | a frame of the same layout whose tag is a request event and whose fields are of the declared kinds; any other frame is logged and ignored |
| `disk-ack`, `disk-read-result`, `timer`, `clock` | above |

`send` and `recv` are refused by `emit.py`: one image is one node, and nothing here addresses another node.

The loop holds no request table, repeats nothing and reorders nothing. Three choices in it are policy and are stated here because a different shim could choose otherwise: completions are handled before timers and timers before frames; a clock reading is taken once per event; a step with no result, a failed block operation, a row key outside the device or a full completion queue stops the node (`solo5_abort`). Stopping is a crash, which a specification already has to survive.

Two things in the loop are for tests only. `hold=N.K` on the boot command line makes the driver stop for good after `K` effects of the `N`-th event, so that the harness can `SIGKILL` the tender at a known point instead of racing it. The console lines starting with `@` are the record of the run.

## How a run is compared with the simulator

`run.py` records three things per boot: the console lines, the frames the client's socket received, and the rows in the block image after the process was killed. `compare.reconstruct` then scripts `tool.sim` with the same event sequence: the events the driver handled in the order it handled them, a crash where the process was killed (inside a step, with the number of effects performed, when it was held there), a restart for the next boot. The simulator computes everything else with its own library and its own disk and network. The record is accepted when

* every event the driver handled is queued in the simulator at that point, so a read result is what the simulated disk holds and an acknowledgement belongs to a write that was issued;
* the simulator's step gives the state and effects the driver logged, and the same number were performed;
* the frames the client received are exactly the outputs performed;
* the block image after every kill holds exactly the simulated disk's rows;
* for a node with a clock, the simulator's clock, moved by its own timers and by the ticks the driver logged, agrees with every logged tick.

The result is an ordinary `sim-trace/2` trace that replays, so the node's Shen invariants are evaluated on it unchanged.

The simulator separates issuing a write, the write becoming durable (`sync`) and the acknowledgement. The driver's block write is synchronous, so a real run uses one corner of that space: `sync` directly after the step. A kill inside a step maps to the simulator's crash with every write issued before the cut persisted.

## What the checks establish

The commands print these counts.

**`python3 -m tool check solo5-host`** (macOS or Linux, no Solo5; about 50 seconds):

* 16 host processes are built from `driver.c`, `host.c` and each image's `node.c`: three nodes, and 13 images with one edit of the driver or of a specification. Each `node.c` has the SHA-256 of the one `tool.compile.build` compiled into the simulator's library.
* 12 scenarios, 22 boots, 109 events. 21 boots end by `SIGKILL` (the other stops itself), 10 of them are followed by a restart on the same block image, and 6 of those kills are inside a step. Every record reconstructs to a simulator trace that replays.
* AcceptedJob: killed after its block write returned and before any reply, restarted on the same image, the retry is answered `ok` with the same receipt and no second write. Also killed before the write (the retry writes) and after the acknowledgement before the reply (the retry does not). `conflict` and `full` are reached. The five Shen invariants of `examples/accepted_job/invariants.shen` hold on the four reconstructed runs.
* The simulator's probe node is killed after 0, 1, 2, 3 and 4 of the four effects of one step; image and frames show exactly that prefix each time.
* A step without a result stops the node and the simulated run ends in the same error.
* Timers fire in due order, a re-armed timer fires once, a cancelled timer and a timer armed before a kill do not fire.
* Five frames that are not requests never reach the step.
* Teeth, on the record: 15 single edits of the record of the kill between write and reply are each rejected (a row missing from, changed in or added to the image; a reply missing or changed at the client; a request not sent; a write, a state, an effect, a receipt, a hold point, a read result or an initial state changed on the console; a boot left out; the boots reordered).
* Teeth, on the driver: 10 edits of `driver.c` and 1 of a specification, built as 12 images, each rejected for its own defect: acknowledging without writing, a torn row, a read that misses the row, dying one effect later than reported (on two nodes), discarding the new state, not sending an output, passing an out-of-range field to the step, firing a timer early, ignoring a cancel, a stale clock, AcceptedJob replying before the acknowledgement. For that last one the comparison rejects the run against the unedited node, and `inv.ok-is-durable` fails on the run reconstructed with the mutant's own library.

**`python3 -m tool check solo5`** adds, on Linux:

* Solo5 v0.9.3 (`38e0c348`) and 17 unikernels (16 for spt, AcceptedJob also for hvt) are built by `nix-build tool/solo5/nix/pinned.nix -A solo5-run`. `solo5-elftool` reports the ABI and the device manifest of each. Each `node.c` hash equals the simulator's.
* The same 12 scenarios, 15 record edits and 12 mutant images under `solo5-spt`, with the block device a file and the network device a tap interface reached through an `AF_PACKET` socket, and again as Linux host processes in the same container. Same verdicts. Every kill is a `SIGKILL` of the `solo5-spt` process.
* For the 10 scenarios of nodes without a clock, the unikernel, the macOS host process and the Linux host process reconstruct to byte-identical simulator traces.

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

The container is the pinned `nixos/nix` image with `/dev/net/tun` and `CAP_NET_ADMIN`, and it needs the network for the Nix binary cache. Its Nix store is kept in the Docker volume `sim-tool-solo5-nix` (about 1.5 GB) so that a second run downloads nothing and takes about two minutes; `docker volume rm sim-tool-solo5-nix` removes it. A first run has to fetch a compiler and may not finish within the check's 580 seconds, in which case the check reports blocked and a second attempt continues from the volume.

`SOLO5_DOCKER` names another container command than `docker`. With none on the path the check fails with `solo5: blocked: no ... on PATH`.

On Linux with Nix no container is needed: `nix build .#solo5-run`, run `result/bin/sim-tool-solo5-run OUT` as root, then `SOLO5_RECORDS=OUT python3 -m tool check solo5`. That path was exercised only with records produced in the container. The runner also writes the SHA-256 of every image and the store path they were built as; the check prints the hash of `accepted_job.spt`.

## What carries over from simulation, and what does not

An invariant checked over simulator traces says something about a real run only if the real run is a run of the simulator. That is what the comparison shows, for the scenarios above and for no others. Per environment rule of [tool/README.md](../README.md#environment-rules):

| Rule the specification may rely on | Under `solo5-spt` (and the host process) | Evidence |
|---|---|---|
| A node that dies during a step performed exactly the effects before the point of death, in order | Holds for death of the tender process: effects are performed one call at a time in the returned order. | Kills after every prefix of four effects; `late-cut` rejected. |
| No completion crosses a crash | Holds: completions and timers live in guest memory. | Timer armed before a kill does not fire; acknowledgement lost at the kill between write and reply. |
| A read sees durable rows only | A read sees every write that has returned, which is durable in the sense of the next row and no other. | `stale-read` rejected. |
| A disk row is written whole or not at all; writes become durable in order; a crash keeps a prefix | **Only against death of the guest or the tender.** | See below. |

**What a block write returning means.** In `solo5-spt` the guest's `solo5_block_write` is one `pwrite64` of one block on a file the tender opened `O_RDWR`. In `solo5-hvt` it is a hypercall and the tender does the same `pwrite`. Neither tender opens the file with `O_SYNC` or `O_DIRECT`, neither calls `fsync`, and the spt seccomp filter allows the guest only `pread64`, `pwrite64`, `read`, `write`, `epoll_pwait`, `timerfd_settime`, `clock_gettime`, `arch_prctl` and `exit_group`. The Solo5 block API has no flush. (Read from the pinned source; the virtio bindings define a flush request and never send one.) So when the write returns, the row is in the host kernel's page cache:

* It survives the guest exiting, aborting or being killed, and the tender being killed. This is the crash the scenarios inject, and for it the acknowledgement means what the simulator's `sync` means.
* It does **not** survive the host losing power or its kernel crashing before writeback. Then an acknowledged row can be gone, later rows can be present without earlier ones, and nothing here says a 512-byte row cannot be torn. `inv.ok-is-durable` shown in simulation is therefore, on Solo5, "an `ok` reply implies the row survives the death of the unikernel", and nothing stronger. No specification can do better on this API; a shim could, only by adding a flush that Solo5 does not offer.

**What does not carry over at all.**

* **Coverage.** The simulator explores schedules; a real run is one schedule. The driver always handles completions first and writes synchronously, so it can only ever show a subset of what was simulated. Everything said about Solo5 is for 12 scripted runs.
* **Kill points.** The process is killed only where the driver holds or is idle. A kill at an arbitrary instant, including inside a `pwrite64`, is not exercised. Two crashes during one recovery are not exercised.
* **The first boot's restart step** cannot be cut: the simulator's boot has no crash.
* **The network.** One client, one link, no loss, no duplication, no reordering was observed or injected. The simulator allows all three; the real runs show none.
* **Time.** A timer is shown to fire no earlier than its delay; how late is not bounded. Under spt the clock is the host's `CLOCK_MONOTONIC` and keeps going across a restart. Under hvt (read from the source, not run) it starts again at each boot, so a specification that compares clock readings across a restart is wrong there. The simulator's clock never goes back.
* **Same code.** The unikernel and the simulator's library are compiled from the same `node.c` by different compilers for different targets (gcc, freestanding, aarch64 ELF; clang, Mach-O). The comparison covers the steps the scenarios reach: 109 events.
* **One node.** No `send`, no `recv`.
* **Trust.** The harness reads the console, the frames and the image. A driver that lied consistently on all three would pass; the mutants each lie on one.

## The three cheats

1. No tabulation: `node.c` is the specializer's output, the same text the simulator loads.
2. No hand staging: the specifications are the kernel's `spec.shen` files, plus [timer/spec.shen](timer/spec.shen), ordinary Shen.
3. Semantics in the shim: the C does block reads and writes, frames, a timer table and a clock, in the order the step returns them. It has input validation (a frame must be a declared request with fields of the declared kinds) and fail-stop on errors. It has no retry, no dedupe, no ordering and no recovery logic; the mutant `reply-before-ack` shows the guarantee moving with the specification while the C stays the same. The `hold` point and the `@` console lines are test instrumentation that ships in the same binary, and the kill they enable is a real `SIGKILL` of the tender.
