# The wasm ladder: one compiled step, five places to run it

`tool/` compiles a node's Shen step to C. `NODE_TARGET=wasm` builds that C as a freestanding wasm32 module that imports nothing ([tool/wasm.py](tool/wasm.py)), so everything nondeterministic has to reach the node as an event. This file lists the places that module has been put, from the one that owns every decision to the one that owns none, and says for each what was run, with what result, and what was not.

| Rung | What runs the step | Flake check | Status here |
|---|---|---|---|
| 1 | The simulator, seeded | `wasm-step` | run, passes |
| 2 | The simulator, schedules chosen and shrunk by Hegel | `hegel-step` (host target), `wasm-step` (module) | run, passes |
| 3 | OS processes on loopback, killed and restarted | `real-host`, `packed-node` | run, passes |
| 4 | NixOS virtual machines with real faults | `real-vm`, `real-vm-mutant` (Linux only) | **UNKNOWN**: never built or booted |
| 5 | unkernel: Linux as a wasm module under a controlled host | `unkernel-probe` | run, passes; a probe of the substrate, the node is not in it |

On the branch `wasm/uk-node` the node is in rung 5 as well: `accepted_job` as a static aarch64-linux program inside the guest equals the native build line for line and reruns bit-identically (flake check `uk-node`; [experiments/unkernel_probe/README.md](experiments/unkernel_probe/README.md), section 8).

Rungs 1 to 4 run the same module and ask the same two questions of it: does it refine the Shen source in shen-jit, and do the Shen invariants hold. Rung 5 asks a different question, about a substrate, and nothing on rungs 1 to 4 depends on it.

## What this branch is

| | |
|---|---|
| Branch | `wasm/integration` |
| Base | `e4e7899` (`wasm-target`: the wasm module and `wasm-step`), which is one commit on `cf5cf74` of `sim-tool` |
| Merged | `wasm/hegel` at `cd78e2e`, `wasm/realkernel` at `0130d3a`, `wasm/unkernel` at `aa2ddd2`, each with `git merge --no-ff` |
| Conflicts | `flake.nix` (three lanes each added checks; all kept, and the two `packages` attributes made one) and `tool/README.md` (two lanes each wrote a section 3; Hegel is section 3, the real host 4, the three tiers 5). `tool/examples/accepted_job/check.py` merged without conflict: only the Hegel lane changed it |
| Evidence commit | `c029b57`. Everything below was run at that commit on 2026-11-24. The commits after it change this file and a comment in `flake.nix`. The script that made the first runs left `NODE_TARGET=wasm`, `HEGEL_PHASES` and `HEGEL_BACKEND` set for its later steps, so the hostile-variable run, the six seeded Hegel faults, and the loopback and unkernel steps were not run as the commands below read. An independent audit reran each from a clean clone of `29418cf` in a clean environment; those reruns are the evidence for them. The logs were kept only in session scratch space, so rungs 1 to 3 are evidenced by rerunning the commands; only rung 5 keeps its record in the repository |
| Machine | aarch64-darwin, macOS 27.2.1. nixpkgs `34ab990` through the flake lock: Python 3.14.7, clang 21.1.8, Node v24.19.0, Maude 3.5.1, hegeltest 0.5.2 with libhegel 0.53.2 |

**Not reconciled with `sim-tool`.** That branch has moved since `cf5cf74`: at the time of writing it is `021f5fa`, eight commits and 41 files on, among them a second trace format, changes to `tool/sim/__init__.py`, `tool/pe.py` and `tool/maude.py`, and an AcceptedJob specification without its acknowledgement token. Six files are changed on both sides: `flake.nix`, `tool/README.md`, `tool/__main__.py`, `tool/abi.c.in`, `tool/compile.py` and `tool/examples/accepted_job/check.py`. The Hegel lane reads the simulator's internals and writes `sim-trace/1`; the real host packs and judges the AcceptedJob that `cf5cf74` has. That merge was not attempted here. Every number in this file is about the tree on `cf5cf74`, and none of it carries over to the merged tree until the checks are run there.

**No Linux.** This machine has no Linux builder: `/etc/nix/machines` does not exist and `nix build .#packages.aarch64-linux.node-runtime` stops at

```
error: Cannot build '/nix/store/y5a234ns7q8z5mc91rvkc7gpiaqzkz63-python3-3.14.7-env.drv'.
       Reason: platform mismatch
       Required system: 'aarch64-linux'
       Current system: 'aarch64-darwin'
```

So every check is observed on aarch64-darwin only. For `aarch64-linux` and `x86_64-linux` all checks evaluate (`nix flake check --no-build --all-systems`, exit 0, 25 checks over the three systems) and none has been built.

## The flake

```sh
nix develop                      # one shell with every rung's tools
nix flake check                  # every check for this system
```

| Check | Rung | What it runs | Systems |
|---|---|---|---|
| `wasm-step` | 1, 2 | `NODE_TARGET=wasm python3 -m tool check kernel accepted_job` | all |
| `hegel-step` | 2 | `python3 -m tool check accepted_job` on the host target, after printing Hegel's versions | all |
| `real-host` | 3 | `python3 -m tool real heartbeat accepted_job` | all |
| `packed-node` | 3 | [nix/packed.py](nix/packed.py) against the packages the machines deploy | all |
| `real-vm`, `real-vm-mutant` | 4 | [nix/vm.py](nix/vm.py), [nix/vm-mutant.py](nix/vm-mutant.py) under the NixOS test driver | `*-linux` only |
| `unkernel-probe` | 5 | `experiments/unkernel_probe/check.py --no-stock` | all |
| `staged-examples`, `native-replay` | | the checks the repository had before `tool/` | all |

The default shell is the tool's environment (clang, `wasm-ld`, Python with wasmtime and hegeltest, Maude, shen-jit) plus Node 24 and `UNKERNEL`, the pinned package. `devShells.unkernel` is the probe's inputs alone. `unkernel-probe` is a flake check because it needs no network while it runs (the package is a fixed-output fetch of the npm tarball, by hash) and took 56 s on its own here. The seven Darwin checks build together in 107 s:

```sh
nix build --no-link --max-jobs 2 -L .#checks.aarch64-darwin.{wasm-step,hegel-step,real-host,packed-node,unkernel-probe,native-replay,staged-examples}
```

## Rung 1: the wasm step under the simulator

```sh
NODE_TARGET=wasm nix develop -c python3 -m tool check kernel accepted_job    # 38 s
nix develop -c python3 -m tool check kernel accepted_job                     # the shared library, 37 s
```

Both exit 0 and end `accepted_job: ok`. With times removed, the two outputs differ in two places only: the `wasm_bytes` entry of the two compile statistics (8263 bytes for the kernel's clause-selection step, 6357 for AcceptedJob). Observed under `NODE_TARGET=wasm`:

* kernel: 9600 state and event rows agree with shen-jit, 1536 of them traps; a swapped-clause mutant differs on 96 rows.
* simulation: 500 seeds, 12800 steps, all quiescent; rerun byte for byte, 50 traces replayed from their records; sha256 over all trace digests `4c7d5468b1d480d6`, the same as on the shared library.
* refinement: the 12800 steps are 163 distinct (state, event) pairs and the module equals shen-jit on all of them. The line of output says "native" on both targets.
* Maude: the translated module reduces the same 163 pairs to the same results; bounded search reaches 1698 states against the simulator's 1573, with the same 125 observations.
* teeth: an edited trace and a shortened trace are rejected on replay. Each of three mutants of the specification is found by its invariant, cut to a minimal trace (5, 10 and 14 steps) and rejected by refinement.

The module of AcceptedJob has no imports and exports `memory`, `node_in`, `node_out`, `node_call`, `node_failure`. `tool.wasm.Wasm` refuses a module with imports; a scratch build of the same C with one call to an undefined `host_now` was refused with `bad.wasm imports ['host_now']`. That refusal is not exercised by any retained check.

UNKNOWN: Linux, as above.

## Rung 2: schedules chosen by Hegel

[tool/sim/hegel.py](tool/sim/hegel.py) lets Hegel answer every question the simulator asks (the next action, each draw, and whether to stop), so Hegel generates the schedule, the faults and the workload and shrinks a failing run. It is the last section of `check accepted_job`, so the two commands of rung 1 run it on both targets. [tool/README.md](tool/README.md), section 3, is the account of it.

Observed, seed 0, 200 cases, identical on the shared library and on the wasm module:

| | Found at case | Hegel shrinks to | Runs | Hand shrinker, same failure | Hand shrinker, retained |
|---|---|---|---|---|---|
| unedited `spec.shen` | none in 200 schedules (170 distinct) | | | | |
| `reply-before-ack` | 2 (28 steps) | 5 steps | 1164 | 5 | 5 |
| `insert-without-key-check` | 2 (31 steps) | 11 steps | 2153 | 10 | 10 |
| `reload-ignores-durable-row` | 4 (27 steps) | 12 steps | 4192 | 17 | 14 |
| `no-conflict-clause` (a trap) | 2 (18 steps) | 9 steps | 2011 | | |

Each shrunk trace replays byte for byte with no Hegel involved, still fails, and a second run gives the same bytes (`c84963bc7d97f51d`, `6cab7c3297605112`, `f125ef4b9caad9c5`). The trap's nine steps also replay on the unedited binary; what distinguishes it is the trapping step, which is replayed and must raise the recorded error.

Teeth retained in the check: the shrink phase switched off must come back unshrunk and be reported; nine hostile settings (`HEGEL_PHASES`, `HEGEL_BACKEND`, `HEGEL_SEED`, `CI` and five more) must not change a byte; two faults in one binary must be pursued as one failure; a profile with one schedule must say `exhausted`; zero cases is refused. Run from outside as well:

```sh
HEGEL_PHASES=generate HEGEL_BACKEND=urandom nix develop -c python3 -m tool check accepted_job
```

exits 0 and prints the same Hegel lines as the native and wasm runs (sha256 of those lines with times removed begins `f7ad43cbdd4f` in all three).

Six faults seeded into a scratch copy of `hegel.py`, one at a time, each make `check accepted_job` exit 1: shrinker always off ("not shrunk: 28 steps, the first failure had 28"), backend unpinned ("HEGEL_* variables changed the run"; with `HEGEL_BACKEND=urandom` already in the environment the same fault is reported as "the same seed gave a different shrunk trace"), trapping step not replayed, target not fixed, `exhausted` always false, zero cases allowed.

UNKNOWN or open:

* Linux. The wheels for both Linux systems are fetched by hash and have never been imported. The retained ceilings on the shrunk length (5, 11, 12, 9) were observed on aarch64-darwin; if Hegel shrinks differently elsewhere the check fails, it does not pass quietly.
* With a trap and `reply-before-ack` in one binary the first failure is 10 steps and Hegel leaves it at 10. That case shows the failure stays the same one, not that it shrinks.
* Whether Hegel's shrinker has a wall-clock limit of its own was not established. Every repeated run here gave the same run counts.

## Rung 3: processes on loopback

[tool/host/](tool/host/) runs one node as an OS process: `node.wasm` in wasmtime, real sockets, files, `fsync` and clock. It writes every event down before acting on the step's effects, and [tool/host/conform.py](tool/host/conform.py) judges a run from the files alone. [tool/README.md](tool/README.md), sections 4 and 5.

```sh
nix develop -c python3 -m tool real heartbeat accepted_job    # 20 s
```

Exit 0. The counts depend on the moment of each kill and differ from run to run; this run:

* heartbeat: 3 runs of two nodes, 9 `kill -9`, 15 log segments, 833 logged events replayed through shen-jit; after node 0's reboot with the clock counted from zero its 9 readings begin at 24 ms and the logs conform; five edits to the logs are each rejected.
* AcceptedJob: 20 runs, 281 requests, 223 replies received, 68 `kill -9` (26 at random moments, the rest at the five points inside the host), 88 log segments. 511 logged events replayed through shen-jit with identical state and effects at every one, every event traced to its cause, invariants hold on every run, 16 torn tails, 12 writes lost with their process.
* log teeth: one run's files are rejected with each of 17 edits (a state, an effect, an event, a cause, a removed record, a changed byte, malformed records, no directory, no records, an output received once more than emitted, an output never emitted).
* hostile input: the host survives and logs nothing for numbers outside 64 bits, 65535 open brackets, events only the host may make and ill-typed fields; it disconnects the sender of 200000 brackets and of 4 MiB without an end of line.
* short write: with `events.log` limited to 803 bytes the host stops at record 5, half written, and its reply never reaches the client.
* the three mutants, run as real processes, are each rejected twice on run 0: by refinement against `spec.shen` and by their named invariant.

`real-host` is the same command in the Nix sandbox (992 and 515 events in that run). `packed-node` drives the packages a machine would deploy: 79 events in 5 segments conform and the 62 outputs the client received are all in the log; one forged output, the outputs received twice over and a directory with no log are rejected; the packaged `reply-before-ack` is rejected both ways; a package for two nodes refuses to start as one; two heartbeat nodes, 109 events in 3 segments in that run (128 in another; the count varies), conform.

Nine faults seeded into a scratch copy of `tool/`, each undoing one repair: eight make `tool real` exit non-zero (clock not reset at boot, no 64-bit bound, a sender never dropped, a short write ignored, an empty log accepted, `--received` as membership only, and two unchecked record forms). One survives: without the host's `RecursionError` catch the check still passes, because a line is at most 64 KiB and 65535 brackets do not overflow this Python's parser. That catch is unexercised.

What this rung cannot show, and the tree says so: that `fsync` did anything. A killed process loses its memory and the kernel keeps every byte it was handed, so a host that never synced would pass. The network is one kernel's loopback. The schedules are the few the kernel chose, and a failure here is a log, not a schedule that replays.

UNKNOWN: Linux. The `RLIMIT_FSIZE` and hostile-input teeth were observed on Darwin only. Heartbeat has no simulator check: it is not on rungs 1 and 2.

## Rung 4: NixOS machines

**UNKNOWN. `real-vm` and `real-vm-mutant` have never been built or booted, here or anywhere.** The exact failure is the platform mismatch quoted above; `nix build .#checks.aarch64-linux.real-vm` reports 64 derivations to build and several hundred paths to fetch (700 in the retained run; the number depends on the local store), then stops at the first Linux derivation.

What was observed, and it is not a run:

* Both tests evaluate for both Linux systems. At `c029b57`:

  ```
  aarch64-linux real-vm         /nix/store/j11iliydv1b2cwdzvgh6kfhwwdlmcxbz-vm-test-run-real-vm.drv
  aarch64-linux real-vm-mutant  /nix/store/ai2dl5gh2jjy5bgkpj9ligvnv06icn1m-vm-test-run-real-vm-mutant.drv
  x86_64-linux  real-vm         /nix/store/0v4hmq0cq0y0313zbpidqwixanp4psz4-vm-test-run-real-vm.drv
  x86_64-linux  real-vm-mutant  /nix/store/3z72milnwg5v5f9nzxji60s0m5xahmgi-vm-test-run-real-vm-mutant.drv
  ```

  These are not the paths the lane reported: the node packages are built from the tree, and the tree is now the merged one.
* The two test scripts, as the driver would see them, pass `ty check` and `ruff check --select F` run by hand against the pinned nixpkgs' test-driver sources ("All checks passed!" twice for each).
* `packed-node` passes on loopback with the same packages, launchers and `node-conform` invocations.

What the tests are written to do is in [tool/README.md](tool/README.md), section 5: a client and two node machines, `systemctl kill -s KILL`, the machine's exit and restart, a cut link, `iptables` drops, `tc netem` delay and loss, then conformance on the logs copied out. `real-vm-mutant` deploys `reply-before-ack` and passes only if conformance rejects it.

Assumptions that the first run will test: `eth1` as the interface between machines, the 50 ms bound under a 60 ms netem delay, `iptables -s` by host name, `sch_netem` in the test kernel. Not built even as a script: a deployment of a host that skips `fsync`, without which nothing shows the power-loss steps have teeth.

To run it, on Linux with KVM:

```sh
nix build .#checks.x86_64-linux.real-vm .#checks.x86_64-linux.real-vm-mutant -L    # or aarch64-linux
```

One thing in the history still says otherwise: the subject of commit `0ba83ee` reads "test it on virtual machines with real faults". It was left as it is so that the lane's commits keep their hashes.

## Rung 5: unkernel

[unkernel](https://github.com/pithings/unkernel) is Linux built as one wasm module, with aarch64 user programs emulated inside it and a JavaScript host under Node. [experiments/unkernel_probe/](experiments/unkernel_probe/) asks whether, with the host's clock and entropy under our control, two boots of the same guest running the same scripted workload reach bit-identical state. The compiled node is not in the guest: this rung is about the substrate.

```sh
nix develop -c python3 experiments/unkernel_probe/check.py              # 70 s, 45 comparisons, 0 failed
nix develop -c python3 experiments/unkernel_probe/check.py --no-stock   # what unkernel-probe runs: 43 comparisons
```

**Verdict: deterministic with four controls**, for one CPU, console-only I/O, no disk, no network device and no vsock, on one machine and one Node binary. The four are a virtual clock, a virtual date, the idle wait taken off the real clock, and seeded entropy. Nothing in the pinned package (npm `0.2.0-261124-75694e2`, kernel sha256 `46dcb726…544125b3`) is edited; the controls are about 230 lines wrapped around it.

Observed here, each digest equal to the one recorded in `evidence/summary.json` (the check requires that, and that the record was made from these sources and this package):

* three boots, two in one process and one fresh, agree on all 128 MiB of memory (`846f05d7…`), the console (`bcc15f8b…`), the import log, 23302 import calls and 382762961 retired guest instructions;
* with the JIT on, three boots agree with each other (memory `14039a02…`) and with the interpreter on console and instruction count;
* with the clock driven by retired instructions, two boots agree;
* two snapshots at virtual 8 s are the same 134218146 bytes, and two restores of one of them agree after seven more virtual seconds;
* with input and the snapshot request arriving inside a busy loop, three boots agree (memory `d0636375…`), two mid-loop snapshots are the same bytes and two restores agree;
* under the stock host two boots differ in memory, which the same comparison reports.

Teeth: seven changes of input (another seed, at boot and at a restore; a clock step of 1001 ns; the date 1 ms later; one input byte; the first input 1 ms later; two lines swapped) each change the digests. Five seeded faults that remove one control are each rejected: entropy, date, clock jitter, the idle wait left to the real clock, and an unreadable instruction counter. A forged digest and a changed source hash in the record are each reported.

Two controls in the shim are **not shown to be needed**: `sched_yield` returning at once, and a seeded `Math.random`. With either removed the guest's memory, console and instruction count are unchanged in these workloads.

Not run:

* disks, the network device, vsock, SMP, a distro image. The host completes block I/O from promises whenever they settle, so each device needs its own rule for when a completion arrives in virtual time. None exists.
* N guests on one virtual switch, two kernels in one thread, a frame delivered through `receiveFrame`. The probe's README argues these are feasible; none was built.
* another machine or Node version. On a different environment the check prints `skip` for the comparison with the record rather than failing.
* the JIT's refusal path, which depends on the garbage collector (0 refused in every run).
* the compiled node, or its host, inside the guest.
* any Linux system: `unkernel-probe` evaluates there and has not been built.

### unkernel or Solo5 for whole-system determinism

Solo5 was not built, run or measured in this work. This compares one measured result with Solo5's published design, and it is a recommendation, not a finding.

The node's step is already deterministic: it imports nothing, and rungs 1 and 2 replay it byte for byte. What whole-system determinism would add is the software under the step: a kernel's network stack, its filesystem and its scheduler, executed so that a failure found there replays. That is the gap between rungs 1 and 2, which replay and own a model of the system, and rungs 3 and 4, which run the real system and replay nothing.

* **unkernel puts a real Linux inside a boundary that can be closed.** The whole guest, kernel and user programs, is wasm, so everything nondeterministic crosses its imported functions: 55 by the probe's count, of which a console-only machine needs 20. The probe shows that closing four of them is enough, within its scope, for bit-identical memory across boots, across the JIT, across snapshot and restore and with input arriving in the middle of a computation, and that a retired-instruction count is there to drive a virtual clock. The guest is the kind of kernel rung 4 deploys.
* **Solo5 offers a narrow interface, not a deterministic guest.** Its guest is native code run by a tender on hardware virtualization or as a confined process. Narrow hypercalls leave the instructions between them on a real CPU, where Solo5 neither counts them nor stops the guest reading a counter; a deterministic tender would have to be written, and what it would give is what the simulator of rung 1 already gives. Its guests are unikernels, not Linux, so the "whole system" would be the step and a library OS, not the kernel the node is deployed on. Its hardware-virtualized tender needs a Linux or BSD host, so on this machine it could not have been run at all.

**Recommendation: carry unkernel forward as the candidate for whole-system determinism, and do not start on Solo5 for that purpose.** Keep it a research rung: no result on rungs 1 to 4 should come to depend on it while its verdict covers one CPU and a console. The next things to establish, in order, are a block device and a network device whose completions arrive at virtual times, with the same two-boot comparison and a seeded fault for each; a frame crossing between two guests under one clock; and whether the node's host can run in the guest at all. Until the first of those is shown, "whole-system determinism" is a claim about a shell on a console.

This is about determinism only. It says nothing about Solo5 as a place to deploy the node, a small guest behind a narrow interface, which is a different purpose and one that nothing here measures. The research plan gives Solo5 that other purpose: "Solo5 becomes a portability test after host behavior agrees" (`RESEARCH.md`).

## What is open, in one place

| | Status | Why |
|---|---|---|
| Every check on `aarch64-linux` and `x86_64-linux` | UNKNOWN | no Linux builder; all evaluate, none built |
| `real-vm`, `real-vm-mutant` | UNKNOWN | never built or booted; four untested assumptions in `nix/vm.py` |
| A VM-tier fault that shows the power-loss steps have teeth | not built | needs a deployed host that skips `fsync` |
| The host's `RecursionError` catch | unexercised | the seeded fault survives |
| The import refusal of `tool.wasm.Wasm` | observed once by hand | no retained check seeds a module with an import |
| Heartbeat under the simulator, Hegel and Maude | not written | it has no `check.py` |
| Hegel's shrinker: a wall-clock limit of its own | not established | |
| unkernel: disk, network, vsock, SMP, N guests, another machine | not run | see rung 5 |
| The compiled node inside unkernel | not attempted | |
| Solo5 | not run | the comparison above is by design |
| Reconciliation with `sim-tool` (`021f5fa`) | not attempted | six files changed on both sides; a second trace format and a changed AcceptedJob |
