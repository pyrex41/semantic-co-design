# unkernel probe: is Linux-as-wasm a deterministic simulation substrate?

[unkernel](https://github.com/pithings/unkernel) is Linux built as one wasm module, with aarch64 user programs emulated inside it, driven by a JavaScript host under Node. This probe asks one question:

> With the host's clock and entropy under our control, do two boots of the same guest running the same scripted workload reach bit-identical state?

**Verdict: deterministic with the listed controls**, for one CPU, console-only I/O, no disk, no network, no vsock. Under the controlled host every boot of the workload ends with the same 128 MiB of linear memory, the same console bytes, the same sequence of host import calls and the same count of retired guest instructions: across processes, with the JIT off and on, and across snapshot and restore. That holds when every input reaches an idle guest (the main workload) and when input and the snapshot request reach a guest in the middle of a busy loop (a second, shorter workload). Under the stock host two boots differ, as expected. Nothing inside the pinned package is edited; the controls are about 230 lines wrapped around it.

Four controls carry the result: the virtual clock, the virtual date, the idle wait taken off the real clock, and seeded entropy. Removing any one of them makes check.py fail. Two more are in the shim but are not shown to be needed by these workloads: `sched_yield` returning at once, and the seeded `Math.random` (section 3).

A block device is covered separately in [section 8](#8-disk): deterministic with one more control, the time at which a disk request completes.

```sh
nix develop .#unkernel -c python3 experiments/unkernel_probe/check.py            # everything, about 75 s
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
* [workload.json](workload.json) is the scripted console input: seven lines of shell at fixed times (loops, files in tmpfs, `sha256sum`, `date`, `/proc/uptime`, `/dev/urandom`, `sleep`, two background jobs racing on timers, a pipe, a 20000-iteration busy loop). [continuation.json](continuation.json) is what runs after a restore. Every line of both arrives while the guest is idle.
* [midrun.json](midrun.json) starts a 20000-iteration loop and types two more lines while it runs; [midrun_continuation.json](midrun_continuation.json) runs after a restore of a snapshot taken inside that loop.
* [check.py](check.py) runs the comparisons below and fails if any is not as stated. Without `--record` it also fails if [evidence/summary.json](evidence/summary.json) was recorded from other sources or another package, or if a fresh controlled digest is not the recorded one.
* [evidence/](evidence/) holds what `--record` wrote: every record (summary.json), the consoles of a controlled boot, a restore, and the two running-guest runs, and the first divergence between the two stock import logs. summary.json names what it was recorded from: the package version, the kernel's hash, the hashes of host.mjs, run.mjs, check.py and the four scripts, and the environment (system, Node store path, nixpkgs revision). It cannot name its own commit. When the environment differs, check.py prints `skip` and compares no digests with the record, since nothing here says they should match on another machine.

## 3. The controls

All of them are in host.mjs, under `mode: "controlled"`.

| Source in the stock host | Control |
|---|---|
| `clock_time_get` reads `performance.now()` and `Date.now()` | One virtual clock, an integer of nanoseconds. `performance.now` returns it and `Date.now` returns 2026-01-01T00:00:00Z plus it. It moves by `stepNs` (1000) on each guest clock read, so time is a function of the number of reads. With `--ns-per-insn` it also moves by that much for each user instruction retired since the last read, so time is a function of guest progress |
| `poll_oneoff` (the idle wait) arms `setTimeout` for the nearest guest timer and returns to the event loop | While the SDK's own `poll_oneoff` runs, `setTimeout` is captured instead of armed. The virtual clock then jumps to the deadline (or to the next scripted input, whichever is first) and the captured callback is called, so the wait ends in the same microtask turn |
| `sched_yield` returns to the event loop every 20 ms of guest time | Returns at once. **Not shown to be needed here**: with the SDK's own `sched_yield` left in place (15 yields to the event loop) memory, console and instruction count are the same. Nothing is pending in the event loop during these runs; with a disk or a network attached, something would be |
| `random_get` uses `crypto.getRandomValues` | A seeded generator (splitmix32). The kernel calls it once at boot and once more after a restore |
| `Math.random` (TCP initial sequence numbers in the SDK's network stack) | Seeded. **Not shown to be needed here**: no network is attached, and leaving `Math.random` alone changes no digest |
| Console input arrives from event-loop tasks at arbitrary guest points | The script is delivered by `vm.write` when the virtual clock passes each line's time. That happens in one of two imports: at an idle wait, when the clock jumps (the guest is idle), or at a clock read (the guest is running). Each record counts both (`deliveredFrom`) |
| The snapshot request arrives the same way | `vm.snapshot({stop: true})` is called from the same two places at a virtual time (`snapshotFrom`) |
| The run ends when a wall-clock timer fires | The run ends, and memory is hashed, in the import call at which the virtual clock reaches the end time |

The digest of a boot is the sha256 of the whole linear memory at that point. A record also holds the digest of the console bytes, the digest of the import log, the number of import calls, and `icount`: kwemu's count of retired guest instructions, read from memory through the exported `wasm_kw_cpu` (32 bytes into the cpu structure, the offset the SDK's own `jit.ts` uses).

## 4. What was observed

All numbers are from one `check.py --record` on aarch64-darwin, in [evidence/summary.json](evidence/summary.json). The stock numbers are different in every run; three recordings gave 2412, 2215 and 2317 differing pages.

### Baseline: stock host, JIT off, two boots at the same time

| | boot A | boot B |
|---|---|---|
| memory sha256 | `e214f37a…` | `60da2f04…` |
| console sha256 (5742 bytes each) | `28dcb1a6…` | `6ea98c67…` |
| import calls | 24561 | 24423 |
| retired instructions | 382760386 | 382760200 |
| task switches / idle waits / yields | 1361 / 958 / 93 | 1364 / 912 / 93 |

The memories differ in 2317 of 32768 4 KiB pages (7.1 %), 4448972 of 134217728 bytes (3.3 %), in 137 runs of pages. The import logs differ in value from call 7 (`random_get`, then every clock read) and in the call itself from call 301, where the two boots switch to different tasks; three calls later one switches again while the other reads the clock ([evidence/stock_divergence.txt](evidence/stock_divergence.txt)).

### Controlled host, every event reaching an idle guest

In these rows all seven inputs, and the snapshot request, were delivered at an idle jump (`deliveredFrom` is `jump: 7`, `clock: 0`; check.py asserts it). With 1000 ns per clock read the guest's own execution moves the clock by microseconds, so no scripted time falls inside a computation, and with `--ns-per-insn 5` this workload's lines still all arrive after the previous one has finished.

| Run | Boots compared | Result |
|---|---|---|
| JIT off | 3 (2 in one process, 1 in a fresh one) | identical: memory `846f05d7…`, console `bcc15f8b…`, 23302 import calls, 382762961 instructions, 804 idle waits, 1259 task switches, virtual end at 22 s |
| JIT on | 3 (2 in one process, 1 fresh) | identical: memory `14039a02…`, 23419 import calls, 100 blocks compiled (850957 bytes of wasm), 17 freed, 0 refused. Console and instruction count are the same as with the JIT off; memory and the import log differ from it, as they must |
| Clock driven by instructions (`--ns-per-insn 5`) | 2 fresh | identical: memory `423a85dd…`, 23885 import calls, 382760984 instructions |
| Snapshot at virtual 8 s | 2 fresh | identical: the two serialized snapshots are the same 134218146 bytes (`0fc5822d…`), taken at guest time 8000.123 ms after 9765 import calls and 63548971 instructions; 115544064 bytes of free pages are zeroed |
| Restore of one snapshot, then the continuation for 7 virtual s | 2 fresh | identical: memory `bb40f54d…`, console `9e63b9e3…`, 7548 import calls, 195819320 instructions |

### Controlled host, events reaching a running guest

[midrun.json](midrun.json) with `--ns-per-insn 5`: the loop starts at 3 s and runs for about 1 s of virtual time; lines are typed at 3.1 s and 3.3 s, and the snapshot is requested at 3.6 s. check.py asserts that these were delivered from a clock read, not at an idle wait.

| Run | Boots compared | Result |
|---|---|---|
| JIT off, to virtual 12 s | 3 (2 in one process, 1 fresh) | identical: memory `d0636375…`, console `ecda1c26…`, 13923 import calls, 308690890 instructions; 2 of 4 inputs delivered from a clock read |
| JIT on | 2 in one process | identical: memory `1e42250d…`, 14027 import calls, 89 blocks compiled, 15 freed, 0 refused. Console and instruction count are the same as with the JIT off |
| Snapshot at virtual 3.6 s, inside the loop | 2 fresh | identical: the same 134218146 bytes (`3e7fbf03…`), requested from a clock read at guest time 3600.749525 ms, after 6786 import calls and 119502968 instructions |
| Restore of that snapshot, then the continuation for 8 virtual s | 2 fresh | identical: memory `f29cd50c…`, console `965344ca…`, 7345 import calls, 308843675 instructions. The loop finishes after the restore; the continuation's first line is delivered from a clock read while it still runs |

In every controlled boot all inputs were delivered, all idle timers (804 in the JIT-off boots of the main workload) were taken by the virtual clock, no timer was left to the real clock, and the instruction count was read (check.py requires a positive integer in every record, so two unread counters do not count as agreeing). Two 22 s virtual boots in one process took 3.8 to 6.0 s of wall time on the interpreter in earlier runs; a stock boot takes 22 s.

### Teeth

The comparison is the same function for every row above; these must and do make it report a difference.

| Change | Digests that change |
|---|---|
| stock host instead of controlled | memory, console, imports, import calls, instruction count |
| entropy seed 2 instead of 1 | memory (2054 pages, 4289261 bytes), console (the guest's `/dev/urandom` output), imports, instruction count |
| clock step 1001 ns instead of 1000 | memory, console, imports |
| one byte of console input (`boot-ok` to `boot-oj`) | memory, console, imports |
| first input 1 ms later | memory, console, imports, import calls, instruction count |
| first two input lines swapped | memory, console, imports, instruction count |
| date 1 ms later (`--epoch-offset 1`) | memory, console, imports, import calls, instruction count |
| restore with seed 2 | memory, console, imports, instruction count |

The first version of the import log held only arguments and return values, and the one-byte input change did not move its digest (the pointers and lengths are the same). The log now includes a digest of the bytes read and written.

### Seeded faults

`run.mjs --fault NAME` removes one control again. check.py runs one boot with each and passes it, next to a good boot, to the function that judged every row above.

| Fault | Outcome |
|---|---|
| `entropy`: `random_get` passed through to `crypto` | rejected: memory, console, imports, import calls, instruction count differ |
| `date`: `Date.now` left alone | rejected: the same five differ |
| `jitter`: the low bit of the real clock, in ns, added to each clock step | rejected: the same five differ |
| `idle-real`: the idle wait's timer handed to the real event loop | rejected: a timer was left to the real clock (the digests do not differ) |
| `icount`: the instruction counter unreadable | rejected: instruction count not read. With `--ns-per-insn` the run refuses to start, rather than fall back to the per-read clock |
| `yield`: the SDK's own `sched_yield` | **not rejected**: memory, console, instruction count and number of import calls are unchanged. Only the import log differs, in what `sched_yield` returned (a promise instead of 0) |
| `math-random`: `Math.random` left alone | **not rejected**: nothing differs |

Two more: check.py's scratch directory has `--` in its name, which the first argument parser of run.mjs cut paths at; and a forged digest or a changed source hash in summary.json must each be reported by the evidence comparison.

## 5. What this does not show

* **Disks, the network device, vsock and SMP were off.** The SDK completes block I/O from promises and raises the IRQ whenever they settle; `exec`/`spawn` and host mounts run over vsock the same way. Under this controlled host the guest never returns to the event loop while it can run, so such a completion would be seen only at an idle wait, and the shim's "idle with no timer means quiescent" rule would be wrong. Each device needs its own rule for when a completion is delivered in virtual time. SMP runs CPUs on threads and is out of scope.
* **The JIT has one path that depends on the garbage collector.** `kw_host_jit_compile` refuses (returns -1 to the guest) when more than a limit of freed modules have not been collected yet, as counted by a `FinalizationRegistry`. It was not reached here (0 refused, 17 freed), but a long JIT run could reach it, and then the guest's path depends on V8's collector. The interpreter has no such path.
* **Same machine, same Node binary.** Digests were not compared across machines or Node versions. The kernel is wasm and the shim removes time and entropy, so no difference is expected, but none was measured.
* **`new Date()` and `performance.timeOrigin` are not replaced**, only `Date.now` and `performance.now`. The single-CPU kernel path uses only the latter two.
* **Time by call count is not realistic time.** With 1000 ns per clock read the 20000-iteration busy loop advances the virtual clock by about 3 ms (16020.075 to 16023.195 ms in the import log); the guest first waits about 20 ms on a timer before it runs the line. `--ns-per-insn` makes user time proportional to instructions; kernel code is native wasm and has no counter, so its time is still the per-read step.
* **`sched_yield` and `Math.random`** are controlled but, as the fault table says, nothing here shows they have to be.
* **The running-guest rows are one loop, one clock rate and console input.** Other points of arrival inside a computation were not swept.
* **The restored guest's wall clock** is whatever the restoring host's `Date.now` says. check.py passes the snapshot's guest time as `--epoch-offset` so that the date continues.

Since the digests agree, there was nothing to localize in the controlled runs. No change in unkernel is needed for this result. Three small ones would remove the need to patch globals: clock, entropy and idle-wait hooks in `createKernel`'s options (it already has `wallClock` for one case), a way to count freed JIT modules that does not depend on the collector, and `icount` as a public counter rather than an offset.

## 6. Cost of a host of our own

The shim depends on the SDK's internals: that the idle wait uses exactly one `setTimeout`, that `Kernel.sendInput` wakes it, that the SDK builds its import object before `WebAssembly.instantiate`. The package is published about daily (16 versions so far, three in the last two days), so each bump of the pin has to be rechecked by `check.py`.

The alternative is a host written for simulation rather than a fork of the SDK. The SDK is 33526 lines of TypeScript, but the kernel has 56 imports (55 functions and the memory), of which a console-only machine needs 20 functions: the 13 WASI calls, `wasm_switch_to`, `wasm_snapshot_point`, `wasm_driver_console_size`, `wasm_dump_stacktrace` and the three JIT calls. The rest are device probes that answer "absent". Estimates, not measurements:

| Piece | Size | Effort |
|---|---|---|
| Console-only deterministic host (imports, JSPI task switching, virtual clock, snapshot copy and restore, JIT install) | 600 to 1000 lines | 2 to 4 days |
| Deterministic block device (in-memory image, completion at a virtual time) | 300 to 500 lines | 3 to 5 days, most of it reading `block.ts` (1524 lines) for the request ABI |
| Raw-frame network device and an L2 switch across guests | 300 lines | 3 to 5 days |
| Keeping up with the kernel's import ABI (`kw_abi_version`) | | a recheck per pin bump; a break is a day or two |

## 7. N guests on one virtual switch under one clock

It looks feasible; it was not built or run here.

* The SDK's `NetDevice` is two calls: the guest's frames arrive at `send(frame)` synchronously inside an import, and `Kernel.receiveFrame(frame)` queues a frame and sets an IRQ bit. A switch is a function from (sender, frame, virtual send time) to (receiver, virtual delivery time).
* Each guest is deterministic given the virtual times at which its inputs arrive. Section 4 shows that for console input, both for an idle guest and, in the running-guest rows, for a guest interrupted inside one busy loop under the instruction-driven clock. A frame would be delivered from the same two imports, through `receiveFrame` instead of `sendInput`; that path was not run.
* "One kernel per thread" then gives two designs. In one thread, the shim parks a guest by returning a pending promise from `sched_yield` or the idle wait (both are JSPI imports; a busy guest yields every 20 ms of its own time) and always resumes the guest whose clock is furthest behind; this is a sequential discrete-event loop and is deterministic by construction, at the speed of one core. With one worker thread per guest, the guests run in parallel and block in their clock import (`Atomics.wait` is allowed in a worker) at the end of each window of virtual time; frames are exchanged between windows, which is deterministic as long as the link latency is at least one window (conservative parallel simulation with lookahead).
* What is unknown: whether two kernels in one thread interleave correctly (the SDK's notes say its API tests run concurrently, its README says only one kernel can run per thread), the cost of 128 MiB of linear memory per guest, and whether the guest's network stack has a time source this probe did not exercise.

## 8. Disk

> Does the guest stay deterministic once it uses a block device?

**Verdict: deterministic with the listed controls**, for one kwblk disk, one CPU, on one machine. The four controls of section 3 are enough when the host completes each disk request inside the submit call, which is what the SDK's own in-memory and file disks do. A disk that completes asynchronously needs a fifth control: **the host must decide when each completion is delivered, as a function of virtual time or guest progress**. Left to the host's event loop, three boots of the same workload gave three different memories, consoles and disk images. With either rule, every boot ends with the same memory, console, import log, instruction count, final disk image, flushed image and sequence of disk requests: across processes, with real delays injected into every request on a loaded machine, with the JIT, and across a snapshot taken while the guest was writing.

```sh
nix develop .#unkernel -c python3 experiments/unkernel_probe/disk_check.py            # 51 comparisons, 45 boots, about 15 s
nix develop .#unkernel -c python3 experiments/unkernel_probe/disk_check.py --record   # rewrite evidence/disk_*
nix build .#checks.aarch64-darwin.unkernel-disk -L                                    # the same, offline in the sandbox (41 s)
```

### What the base system offers, and what was attached

* The base system's kernel has ext2, ext3, ext4 and erofs (`/proc/filesystems`) but no vfat, and its busybox has `mkfs.vfat` and `mkdosfs` but no `mke2fs`. So **nothing the guest can make can be mounted**: there is no mkfs for a file system the kernel has. `dd`, `sync`, `fsync`, `blkid`, `blkdiscard`, `fstrim`, `mount` and `umount` are there.
* The pinned kernel module carries an ext4 image of its own (custom section `unkernel.datadisk`, read with the SDK's public `bundledBoot()` and `readKwsparse()`): 4 GiB sparse, 547 non-zero 4 KiB blocks, label `kwdata`, fixed UUID, with a journal. That is the initial disk, so nothing is built or downloaded. The base `/init` finds it by label and mounts it at `/data` (`ext4 rw,noatime,commit=1`) before the shell starts.
* The disk is a `BlockDevice` of our own ([disk.mjs](disk.mjs), `MemDisk`) passed to the public `createKernel({ disks: [...] })`: a sparse in-memory image, a log of every request with a digest of its data, and a second image that takes writes only when the guest flushes. It advertises flush (`F_FLUSH`; the guest reports `write back` cache and `kwblk: wda: 4096 MiB, flush`), not discard or write-zeroes. The kernel exposes no FUA (`/sys/block/wda/queue/fua` read 0 and `queue/scheduler` read `none [mq-deadline] kyber` in an exploratory boot; those two lines are not in the retained evidence).
* [disk_workload.json](disk_workload.json), 7 lines to virtual 40 s: write three small files and 256 KiB of `/dev/urandom`, `sync`, hash; append 40 lines to a file and `fsync` it; unmount, mount again elsewhere, hash the files read back from the disk; write 2 MiB in one background job while another spins, `rm`, `sync`; drop caches and hash again, print `/sys/block/wda/stat`; write a file without syncing, unmount, hash the first 64 KiB of the raw device. The three hashes of the random file agree ([evidence/disk_console_sync.txt](evidence/disk_console_sync.txt)).

The image digest is the sha256 of the image's canonical bytes (the SDK's kwsparse encoding: size, then the non-zero blocks in order), so it identifies the content of the whole 4 GiB.

### When a request completes: four rules

`disk.mjs --completion`:

| Rule | What the host does | Result |
|---|---|---|
| `sync` | completes inside `wasm_driver_block_submit` (returns 0) | deterministic with the four controls alone |
| `microtask` | returns a resolved promise: the SDK's asynchronous path (submit returns 1, IRQ 3, `wasm_driver_block_reap`), the completion landing at the guest's next suspension | deterministic |
| `virtual` | returns a promise that host.mjs resolves when the virtual clock passes submit time + 200 us (optionally + a seeded 0 to 5000 us), from a clock read or an idle jump | deterministic: **the control** for an asynchronous disk |
| `real` | returns a promise resolved by a real timer of random length, 0 to 20 ms | **not deterministic**: the seeded fault |

host.mjs gained two small hooks for this (`cfg.devices`): the idle wait also jumps to a device's next virtual deadline, and it leaves the wait to the host's event loop, with the virtual clock standing still, while a device says it owes an event only that loop can deliver.

### What was observed

All numbers are from one `disk_check.py --record` on aarch64-darwin, in [evidence/disk_summary.json](evidence/disk_summary.json). Every boot is a fresh process; up to four run at once. Compared in every row: memory, console, import log, number of import calls, instruction count, final image, flushed image, request log.

| Run | Boots | Result |
|---|---|---|
| `sync` | 3 | identical: memory `a59c0f61…`, console `db7633e0…`, 29281 import calls, 201700902 instructions, image `5e44d8ce…` (668 non-zero blocks), 71 reads, 64 writes, 32 flushes (3196928 bytes read, 2768896 written), 0 asynchronous requests |
| The SDK's `CowDisk` over the same image (in memory, no store) | 2 | identical: memory `6b3f1741…`, 29300 import calls, 201713477 instructions, image `d15b464b…`, 0 asynchronous requests. Another run than `sync` (this disk's flags are flush, discard and write-zeroes; ours are flush only) |
| `microtask` | 2 | identical: memory `5cdeb1bc…`, 30384 import calls, 201701077 instructions, image `d107e957…`, 170 asynchronous requests (71 reads, 66 writes, 33 flushes) |
| `virtual`, 200 us | 3 | identical: memory `ff4119de…`, 30514 import calls, 201701171 instructions, image `b1b398ee…`, 169 completions delivered at virtual times |
| `virtual`, 200 to 5200 us seeded | 2 | identical: memory `7d833cb8…`, 30787 import calls, 201700803 instructions, image `40c78ef3…`, 170 completions |
| **Attack**: each of the four rows above with a busy-wait of random length, 0 to 3 ms of real time, inside every request, while 4 processes spin | 1 each | the same digests as the undisturbed boots of that row, all eight keys |
| JIT on, `sync` and `virtual` | 2 each | identical; 58 and 59 blocks compiled (517943 and 518796 bytes), 2 freed, 0 refused. Console, instruction count, image, flushed image and request log are those of the interpreter; memory and the import log differ from it, as in section 4 |
| **`real`** | 3 | **all three differ on all eight keys**: 30860, 30880 and 30895 import calls; images `831ec3cb…`, `86e526d0…`, `5ba1afa6…`; three different consoles. Each boot finished the workload with nothing in flight |

The three deterministic rules give three different runs (different memory, console and image: the virtual time at which things happen is in ext4's timestamps and in `/sys/block/wda/stat`), and the comparison tells them apart.

### The mechanism of the divergence

[evidence/disk_real_divergence.txt](evidence/disk_real_divergence.txt). At import calls 4545 to 4548, while `/init` mounts the disk, the guest submits four reads in a row (requests 15274864, 15277232, 15270128, 15272496: sectors 4194304, 4194424, 4104, 4224). All four are in flight when it goes idle (call 4554, `poll_oneoff`). The first import call that differs between two boots is **call 4556, `wasm_driver_block_reap()`: one boot reaps request 15277232, the other 15272496**. Nothing differs before it: same calls, same virtual clock values.

So the cause is host event-loop ordering, not a clock read in the block path. `BlockHost.submit()` chains each request's completion on the device's promise (`r.then(...)`: write the status, push the request on `done`, raise IRQ 3 and wake the idle wait). Which promise settles first, and how many settle before the guest runs again, is decided by the host's timers. From that call on the two import logs are different sequences, and at the end memory, console and image differ. Two requests in flight are enough for this; kwblk's queue is 32 deep.

A second way to fail was seen first: with the four controls alone and nothing else (`--real-starve 1`), the first read **never completes**. The controlled idle wait ends in the same microtask turn, so the event loop never runs and the real timer never fires: the virtual clock reaches the end time (4 s here) after 1333787 import calls with one read submitted, one in flight, and no user instruction retired. This is the gap section 5 predicted.

### The controls, and proof that each is needed

| Control | Proof |
|---|---|
| The four of section 3 (clock, date, idle wait, entropy) | section 4; they are on in every row above |
| Completion of a disk request at a time the host computes from the virtual clock (`virtual`), or inside the submit call (`sync`), or at the next suspension (`microtask`) | removed by `--completion real`: rejected, all eight keys differ, first at a block reap |
| The idle wait knows about pending disk completions (it jumps to the next one; it does not call the guest quiescent) | host.mjs `cfg.devices`. Needed by construction (a `virtual` completion due while the guest idles is resolved from that jump); no run here removes it alone |

`virtual` is the rule to build on: it gives the disk a latency in guest time, it allows several requests in flight, and it is immune to the host by construction, since a completion is resolved only from an import call at which the virtual clock has passed its time. No change inside unkernel is needed: the device object decides when its promises settle, and that object is ours. (In the source, `js/core/block.ts`, `BlockHost.submit()`; pinned package `dist/_chunks/core.mjs`.)

### Snapshot and restore with a disk

What the SDK's rules are, as read in the pinned package (`dist/_chunks/core.mjs`); the table below says which were exercised:

* `snapshot()` rejects unless every disk has `snapshotState()` and `snapshotSeal()`. The SDK's own in-memory `CowDisk` throws once written ("keeps its writes in memory only"; read, not run); ours implements both.
* A snapshot holds no disk content: only what `snapshotState()` returned, `{name, identity, gen}`. Keeping the image that belongs to a snapshot is the host's job (`disk.mjs` saves it at the seal).
* At the consistent point the snapshot is refused if a block request is in flight or unreaped. With `stop: true` the kernel syncs and drops caches first. No snapshot below was refused, so the refusal itself was not seen.
* `createKernel({ restore })` compares each disk's identity and generation with the snapshot's.

| Run | Boots | Result |
|---|---|---|
| Snapshot at virtual 10.05 s (`--ns-per-insn 5`), requested from a clock read while one job writes 2 MiB and another spins, `sync` | 2 | the same 134218202 bytes (`4a81de9b…`) at guest time 10050.249415 ms, after 13770 import calls, 38900042 instructions and 48 reads, 40 writes, 17 flushes; the same sealed image (`2f4bc6ee…`), generation 1 |
| The same, `virtual` | 2 | the same 134218202 bytes (`1b591218…`) at guest time 10142.66431 ms (the kernel's sync before the snapshot takes virtual time here), 15137 import calls, 57294228 instructions, 104 asynchronous requests; sealed image `68c8f3d6…` |
| Restore with the sealed image, then [disk_continuation.json](disk_continuation.json) for 7 virtual s (wait for the jobs, `sync`, hash, write a file, unmount), `sync` | 2 | identical: memory `777f0c31…`, 8164 import calls, 201746671 instructions, image `42da6c2b…`, 25 reads, 23 writes, 14 flushes |
| The same, `virtual` | 2 | identical: memory `8ac301e6…`, 8898 import calls, 201746841 instructions, image `fc28c413…`, 68 asynchronous requests |
| Restore with the disk at generation 2 | 1 | refused by the SDK: `disk wda (probe memdisk) was written after the snapshot (generation 2, the snapshot has 1)` |
| Restore with **another image** under the same identity and generation | 1 | **not refused**: it boots and runs on a disk that is not the snapshot's (image `94800ee6…`). The SDK checks what the disk object says, not its content; a simulation has to bind the image digest to the snapshot itself |

### Power cut

The block interface shows the host five operations (read, write, flush, discard, write-zeroes) and nothing else: **flushes are visible as requests of their own, FUA is not** (the driver has none; the block layer turns it into flushes). An ext4 journal commit is seen as: write the journal blocks, flush, write the commit block, flush ([evidence/disk_requests_power_cut.txt](evidence/disk_requests_power_cut.txt)). So the host can keep exactly what the guest flushed.

[disk_crash.json](disk_crash.json): write a file and `sync`; write a second and `fsync` it; write a third and 2 MiB more with no sync. `--crash-at-op 48` ends the run at the 48th disk request, the write of the commit block of the transaction that holds the third file, before the flush after it.

| Run | Boots | Result |
|---|---|---|
| Power cut at request 48 | 2 | identical: virtual 5120.370 ms, 7824 import calls, 4793095 instructions, 21 reads, 21 writes, 6 flushes; written image `a5694681…`, flushed image `07723112…`; they differ by one unflushed write of 4096 bytes |
| Second guest on the flushed image ([disk_recover.json](disk_recover.json)) | 2 | identical: memory `6e0a51d9…`, 9315 import calls, 6175358 instructions, image `15c8302b…`. ext4 says `recovery complete`; the synced and the fsynced file are there; the unsynced file and the 2 MiB file are not ([evidence/disk_console_after_power_cut.txt](evidence/disk_console_after_power_cut.txt)) |
| Second guest on the written image (the unflushed block kept) | 1 | the unsynced file and the 2 MiB file are there. The one dropped block decides |

Dropped here is everything after the last flush. Keeping an arbitrary subset of the unflushed writes (a torn or reordered cut) is the same mechanism and was not run.

### Teeth

| Change | Keys that change |
|---|---|
| one bit of the 256 KiB data write, as stored | console (the guest's hash of the file read back), import log, image, flushed image, request log. Memory does not differ (why not was not looked into) |
| one byte of the initial image, in a block the guest never reads (offset 3 GiB + 5) | image and flushed image **only**: the image has to be compared on its own |
| one bit of the first write, as stored (the guest writes that block again later) | request log **only**: the requests have to be compared on their own |
| one byte of console input (`file-$f` to `file_$f`) | memory, console, import log, instruction count, image, flushed image, request log |
| virtual latency 201 us instead of 200 | all eight |
| latency seed 8 instead of 7 | all eight |
| a forged image digest in the record | reported by the evidence comparison |

### What this does not show

* **One disk, one CPU, one machine, one Node binary.** Two disks, a distro image as the first disk, and SMP were not run.
* **The stock host with a disk** was not booted; the baseline here is the controlled host with the `real` rule.
* **The SDK's persistent disks were not run**: `FileDisk` (synchronous `pread`/`pwrite` on a real file, by its declaration), `CowDisk` with a store (its flush is an asynchronous save), and `HttpBase` (asynchronous reads over the network). The last two are asynchronous on the host's event loop, which is the case shown above to diverge; a deterministic host has to wrap or replace them.
* **Discard and write-zeroes** were offered only in the `CowDisk` row; `MemDisk` implements them (`--discard 1`) but no row ran it.
* **Request errors** (a failed read or write, a full device) and a disk larger than its backing were not injected.
* **`Kernel.requestSync()`** (the host asking the guest to sync, IRQ 4) and `snapshot({ commit })` were not used.
* **`microtask` under the JIT, and `real` under any setting but the one shown**, were not run. Three `real` boots differed every time it was tried (10 times in this session); nothing bounds how often they could agree by chance, and then disk_check.py would fail.
* **A sweep of cut points.** One request number was cut at; every request number is a candidate, and each is deterministic by the same argument, but only one was run.
* **Virtual latency is one number per request**, not a model of a device (no queueing, no dependence on size or operation).
