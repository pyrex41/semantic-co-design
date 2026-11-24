# unkernel probe: is Linux-as-wasm a deterministic simulation substrate?

[unkernel](https://github.com/pithings/unkernel) is Linux built as one wasm module, with aarch64 user programs emulated inside it, driven by a JavaScript host under Node. This probe asks one question:

> With the host's clock and entropy under our control, do two boots of the same guest running the same scripted workload reach bit-identical state?

**Verdict: deterministic with the listed controls**, for one CPU, console-only I/O, no disk, no network, no vsock. Under the controlled host every boot of the workload ends with the same 128 MiB of linear memory, the same console bytes, the same sequence of host import calls and the same count of retired guest instructions: across processes, with the JIT off and on, and across snapshot and restore. Under the stock host two boots differ, as expected. Nothing inside the pinned package is edited; the controls are about 200 lines wrapped around it.

```sh
nix develop .#unkernel -c python3 experiments/unkernel_probe/check.py            # everything, about 50 s
nix develop .#unkernel -c python3 experiments/unkernel_probe/check.py --no-stock # what `nix flake check` runs (unkernel-probe)
nix develop .#unkernel -c python3 experiments/unkernel_probe/check.py --record   # rewrite evidence/
```

## 1. What is pinned

| | |
|---|---|
| unkernel | npm `0.2.0-261124-75694e2`, tarball `sha256-GxMTBQ/VwahhTkNRkSPo/tl5dG9WjdQmeFsqMFRyv/Q=` ([nix/unkernel.nix](../../nix/unkernel.nix)), unpacked and untouched |
| kernel | `dist/unkernel.wasm`, sha256 `46dcb726…544125b3`, with its bundled base system (busybox shell). No distro image, so nothing is downloaded at run time |
| Node | `nodejs_24` of the flake's nixpkgs: v24.19.0, started with `--experimental-wasm-jspi` (without the flag `WebAssembly.Suspending` is undefined in this build and the SDK refuses to boot) |
| guest | 1 CPU, 128 MiB, `vsock: false`, no `net`, no disks, `jit` as stated per run |

## 2. The files

* [host.mjs](host.mjs) is the controlled host. It is installed before the SDK is imported, replaces the globals the SDK reads, and wraps the kernel's imports when the SDK calls `WebAssembly.instantiate`. `WebAssembly.Suspending` is replaced by a constructor that remembers the wrapped function, so the JSPI imports can be wrapped too. Every import call is logged as `name(args)=return extra`, where `extra` is what the call gave or took besides its arguments (the clock value, the first random bytes, a digest of the bytes read or written).
* [run.mjs](run.mjs) boots once (or `--repeat` times in one process) and writes a record of digests and counters.
* [workload.json](workload.json) is the scripted console input: seven lines of shell at fixed times (loops, files in tmpfs, `sha256sum`, `date`, `/proc/uptime`, `/dev/urandom`, `sleep`, two background jobs racing on timers, a pipe, a 20000-iteration busy loop). [continuation.json](continuation.json) is what runs after a restore.
* [check.py](check.py) runs the comparisons below and fails if any is not as stated.
* [evidence/](evidence/) holds what `--record` wrote: every record ([summary.json](evidence/summary.json), bound to the package version, the kernel's hash and the hashes of the probe's sources), the console of a controlled boot and of a restore, and the first divergence between the two stock import logs.

## 3. The controls

All of them are in host.mjs, under `mode: "controlled"`.

| Source in the stock host | Control |
|---|---|
| `clock_time_get` reads `performance.now()` and `Date.now()` | One virtual clock, an integer of nanoseconds. `performance.now` returns it and `Date.now` returns 2026-01-01T00:00:00Z plus it. It moves by `stepNs` (1000) on each guest clock read, so time is a function of the number of reads. With `--ns-per-insn` it also moves by that much for each user instruction retired since the last read, so time is a function of guest progress |
| `poll_oneoff` (the idle wait) arms `setTimeout` for the nearest guest timer and returns to the event loop | While the SDK's own `poll_oneoff` runs, `setTimeout` is captured instead of armed. The virtual clock then jumps to the deadline (or to the next scripted input, whichever is first) and the captured callback is called, so the wait ends in the same microtask turn |
| `sched_yield` returns to the event loop every 20 ms of guest time | Returns at once. The guest never hands control to the event loop while it can run |
| `random_get` uses `crypto.getRandomValues` | A seeded generator (splitmix32). The kernel calls it once at boot and once more after a restore |
| `Math.random` (TCP initial sequence numbers in the SDK's network stack) | Seeded, though no network is attached here |
| Console input arrives from event-loop tasks at arbitrary guest points | The script is delivered by `vm.write` from inside the clock and idle imports when the virtual clock passes each line's time |
| The snapshot request arrives the same way | `vm.snapshot({stop: true})` is called from the same place at a virtual time |
| The run ends when a wall-clock timer fires | The run ends, and memory is hashed, in the import call at which the virtual clock reaches the end time |

The digest of a boot is the sha256 of the whole linear memory at that point. A record also holds the digest of the console bytes, the digest of the import log, the number of import calls, and `icount`: kwemu's count of retired guest instructions, read from memory through the exported `wasm_kw_cpu` (32 bytes into the cpu structure, the offset the SDK's own `jit.ts` uses).

## 4. What was observed

All numbers are from `check.py --record` on aarch64-darwin, in [evidence/summary.json](evidence/summary.json).

### Baseline: stock host, JIT off, two boots at the same time

| | boot A | boot B |
|---|---|---|
| memory sha256 | `14897d7e…` | `fb5f0aea…` |
| console sha256 (5742 bytes each) | `b5be1f9e…` | `b83eee8a…` |
| import calls | 23980 | 23614 |
| retired instructions | 382771955 | 382758745 |
| task switches / idle waits / yields | 1387 / 783 / 102 | 1478 / 724 / 101 |

The memories differ in 2412 of 32768 4 KiB pages (7.4 %), 4622646 of 134217728 bytes (3.4 %), in 132 runs of pages. The import logs differ in value from call 7 (`random_get`, then every clock read) and in which import is called from call 167, where one boot reads the clock once more before a task switch than the other ([evidence/stock_divergence.txt](evidence/stock_divergence.txt)).

### Controlled host

| Run | Boots compared | Result |
|---|---|---|
| JIT off | 3 (2 in one process, 1 in a fresh one) | identical: memory `846f05d7…`, console `bcc15f8b…`, 23302 import calls, 382762961 instructions, 804 idle waits, 1259 task switches, virtual end at 22 s |
| JIT on | 3 (2 in one process, 1 fresh) | identical: memory `14039a02…`, 23419 import calls, 100 blocks compiled (850957 bytes of wasm), 17 freed, 0 refused. Console and instruction count are the same as with the JIT off; memory and the import log differ from it, as they must |
| Clock driven by instructions (`--ns-per-insn 5`) | 2 fresh | identical: memory `423a85dd…`, 23885 import calls, 382760984 instructions |
| Snapshot at virtual 8 s | 2 fresh | identical: the two serialized snapshots are the same 134218146 bytes (`0fc5822d…`), taken at guest time 8000.123 ms after 9765 import calls and 63548971 instructions; 115544064 bytes of free pages are zeroed |
| Restore of one snapshot, then the continuation for 7 virtual s | 2 fresh | identical: memory `bb40f54d…`, console `9e63b9e3…`, 7548 import calls, 195819320 instructions |

In every controlled boot all inputs were delivered, all idle timers (804 in the JIT-off boots) were taken by the virtual clock, and no timer was left to the real clock. Two 22 s virtual boots in one process took 6.0 s of wall time on the interpreter; a stock boot takes 22 s.

### Teeth

The comparison is the same function for every row above; these must and do make it report a difference.

| Change | Digests that change |
|---|---|
| stock host instead of controlled | memory, console, imports, import calls, instruction count |
| entropy seed 2 instead of 1 | memory (2054 pages, 4289261 bytes), console (the guest's `/dev/urandom` output), imports, instruction count |
| clock step 1001 ns instead of 1000 | memory, console, imports |
| one byte of console input (`boot-ok` to `boot-oj`) | memory, console, imports |
| restore with seed 2 | memory, console, imports, instruction count |

The first version of the import log held only arguments and return values, and the one-byte input change did not move its digest (the pointers and lengths are the same). The log now includes a digest of the bytes read and written.

## 5. What this does not show

* **Disks, the network device, vsock and SMP were off.** The SDK completes block I/O from promises and raises the IRQ whenever they settle; `exec`/`spawn` and host mounts run over vsock the same way. Under this controlled host the guest never returns to the event loop while it can run, so such a completion would be seen only at an idle wait, and the shim's "idle with no timer means quiescent" rule would be wrong. Each device needs its own rule for when a completion is delivered in virtual time. SMP runs CPUs on threads and is out of scope.
* **The JIT has one path that depends on the garbage collector.** `kw_host_jit_compile` refuses (returns -1 to the guest) when more than a limit of freed modules have not been collected yet, as counted by a `FinalizationRegistry`. It was not reached here (0 refused, 17 freed), but a long JIT run could reach it, and then the guest's path depends on V8's collector. The interpreter has no such path.
* **Same machine, same Node binary.** Digests were not compared across machines or Node versions. The kernel is wasm and the shim removes time and entropy, so no difference is expected, but none was measured.
* **`new Date()` and `performance.timeOrigin` are not replaced**, only `Date.now` and `performance.now`. The single-CPU kernel path uses only the latter two.
* **Time by call count is not realistic time.** With 1000 ns per clock read the 20000-iteration busy loop costs about 20 ms of guest time. `--ns-per-insn` makes user time proportional to instructions; kernel code is native wasm and has no counter, so its time is still the per-read step.
* **The restored guest's wall clock** is whatever the restoring host's `Date.now` says. check.py passes the snapshot's guest time as `--epoch-offset` so that the date continues.

Since the digests agree, there was nothing to localize in the controlled runs. No change in unkernel is needed for this result. Three small ones would remove the need to patch globals: clock, entropy and idle-wait hooks in `createKernel`'s options (it already has `wallClock` for one case), a way to count freed JIT modules that does not depend on the collector, and `icount` as a public counter rather than an offset.

## 6. Cost of a host of our own

The shim depends on the SDK's internals: that the idle wait uses exactly one `setTimeout`, that `Kernel.sendInput` wakes it, that the SDK builds its import object before `WebAssembly.instantiate`. The package is published about daily (16 versions so far, three in the last two days), so each bump of the pin has to be rechecked by `check.py`.

The alternative is a host written for simulation rather than a fork of the SDK. The SDK is 33526 lines of TypeScript, but the kernel imports 56 functions, of which a console-only machine needs 20: the 13 WASI calls, `wasm_switch_to`, `wasm_snapshot_point`, `wasm_driver_console_size`, `wasm_dump_stacktrace` and the three JIT calls. The rest are device probes that answer "absent". Estimates, not measurements:

| Piece | Size | Effort |
|---|---|---|
| Console-only deterministic host (imports, JSPI task switching, virtual clock, snapshot copy and restore, JIT install) | 600 to 1000 lines | 2 to 4 days |
| Deterministic block device (in-memory image, completion at a virtual time) | 300 to 500 lines | 3 to 5 days, most of it reading `block.ts` (1524 lines) for the request ABI |
| Raw-frame network device and an L2 switch across guests | 300 lines | 3 to 5 days |
| Keeping up with the kernel's import ABI (`kw_abi_version`) | | a recheck per pin bump; a break is a day or two |

## 7. N guests on one virtual switch under one clock

It looks feasible; it was not built or run here.

* The SDK's `NetDevice` is two calls: the guest's frames arrive at `send(frame)` synchronously inside an import, and `Kernel.receiveFrame(frame)` queues a frame and sets an IRQ bit. A switch is a function from (sender, frame, virtual send time) to (receiver, virtual delivery time).
* Each guest is deterministic given the virtual times at which its inputs arrive. That is what section 4 shows for console input, and a frame is delivered the same way.
* "One kernel per thread" then gives two designs. In one thread, the shim parks a guest by returning a pending promise from `sched_yield` or the idle wait (both are JSPI imports; a busy guest yields every 20 ms of its own time) and always resumes the guest whose clock is furthest behind; this is a sequential discrete-event loop and is deterministic by construction, at the speed of one core. With one worker thread per guest, the guests run in parallel and block in their clock import (`Atomics.wait` is allowed in a worker) at the end of each window of virtual time; frames are exchanged between windows, which is deterministic as long as the link latency is at least one window (conservative parallel simulation with lookahead).
* What is unknown: whether two kernels in one thread interleave correctly (the SDK's notes say its API tests run concurrently, its README says only one kernel can run per thread), the cost of 128 MiB of linear memory per guest, and whether the guest's network stack has a time source this probe did not exercise.
