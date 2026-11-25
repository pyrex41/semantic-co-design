# unkernel probe: is Linux-as-wasm a deterministic simulation substrate?

[unkernel](https://github.com/pithings/unkernel) is Linux built as one wasm module, with aarch64 user programs emulated inside it, driven by a JavaScript host under Node. This probe asks one question:

> With the host's clock and entropy under our control, do two boots of the same guest running the same scripted workload reach bit-identical state?

**Verdict: deterministic with the listed controls**, for one CPU, console-only I/O, no disk, no network, no vsock. Sections 8 to 10 go past that scope (the compiled node in the guest, the network device, a block device). Section 9 adds the network device: one guest against a scripted peer, and three guests on one virtual switch, are deterministic too, with one more control (when a frame is handed to the guest). Under the controlled host every boot of the workload ends with the same 128 MiB of linear memory, the same console bytes, the same sequence of host import calls and the same count of retired guest instructions: across processes, with the JIT off and on, and across snapshot and restore. That holds when every input reaches an idle guest (the main workload) and when input and the snapshot request reach a guest in the middle of a busy loop (a second, shorter workload). Under the stock host two boots differ, as expected. Nothing inside the pinned package is edited; the controls are about 230 lines wrapped around it.

Four controls carry the result: the virtual clock, the virtual date, the idle wait taken off the real clock, and seeded entropy. Removing any one of them makes check.py fail. Two more are in the shim but are not shown to be needed by these workloads: `sched_yield` returning at once, and the seeded `Math.random` (section 3).

A block device is covered separately in [section 10](#10-disk): deterministic with one more control, the time at which a disk request completes.

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

All numbers are from one `check.py --record` on aarch64-darwin, in [evidence/summary.json](evidence/summary.json). The stock numbers are different in every run; four recordings gave 2412, 2215, 2317 and 2393 differing pages.

### Baseline: stock host, JIT off, two boots at the same time

| | boot A | boot B |
|---|---|---|
| memory sha256 | `35b81135…` | `df58d233…` |
| console sha256 (5742 bytes each) | `b36884f5…` | `bb2c1326…` |
| import calls | 24170 | 24242 |
| retired instructions | 382773082 | 382759835 |
| task switches / idle waits / yields | 1375 / 755 / 104 | 1347 / 764 / 108 |

The memories differ in 2393 of 32768 4 KiB pages (7.3 %), 4506790 of 134217728 bytes (3.4 %), in 140 runs of pages. The import logs differ in value from call 7 (`random_get`, then every clock read) and in the call itself from call 837, where the two boots switch to different tasks; three calls later they switch to different tasks again ([evidence/stock_divergence.txt](evidence/stock_divergence.txt)).

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

* **Disks, vsock and SMP were off** (the network device is section 9, a block device section 10). The SDK completes block I/O from promises and raises the IRQ whenever they settle; `exec`/`spawn` and host mounts run over vsock the same way. Under this controlled host the guest never returns to the event loop while it can run, so such a completion would be seen only at an idle wait, and the shim's "idle with no timer means quiescent" rule would be wrong. Each device needs its own rule for when a completion is delivered in virtual time. SMP runs CPUs on threads and is out of scope.
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

It looks feasible; this section is the argument made before it was built. Section 9 builds the one-thread design and runs it.

* The SDK's `NetDevice` is two calls: the guest's frames arrive at `send(frame)` synchronously inside an import, and `Kernel.receiveFrame(frame)` queues a frame and sets an IRQ bit. A switch is a function from (sender, frame, virtual send time) to (receiver, virtual delivery time).
* Each guest is deterministic given the virtual times at which its inputs arrive. Section 4 shows that for console input, both for an idle guest and, in the running-guest rows, for a guest interrupted inside one busy loop under the instruction-driven clock. A frame would be delivered from the same two imports, through `receiveFrame` instead of `sendInput`; that path was not run.
* "One kernel per thread" then gives two designs. In one thread, the shim parks a guest by returning a pending promise from `sched_yield` or the idle wait (both are JSPI imports; a busy guest yields every 20 ms of its own time) and always resumes the guest whose clock is furthest behind; this is a sequential discrete-event loop and is deterministic by construction, at the speed of one core. With one worker thread per guest, the guests run in parallel and block in their clock import (`Atomics.wait` is allowed in a worker) at the end of each window of virtual time; frames are exchanged between windows, which is deterministic as long as the link latency is at least one window (conservative parallel simulation with lookahead).
* What is unknown: whether two kernels in one thread interleave correctly (the SDK's notes say its API tests run concurrently, its README says only one kernel can run per thread), the cost of 128 MiB of linear memory per guest, and whether the guest's network stack has a time source this probe did not exercise.

## 8. The compiled node inside the guest

Sections 1 to 7 ran the base shell only. This section puts our own program in the guest: the `accepted_job` node, compiled by `tool/compile.py` to C and built as a static aarch64-linux executable, run by the guest's Linux under the same controlled host.

**Verdict: inside the guest the node computes exactly what the native build computes, and the whole session is bit-identical across fresh processes**, with the JIT off and on, with the files delivered in the initramfs or over 9p on vsock. The initramfs path needs no control beyond the four of section 3. The 9p path needs one more, kept outside the package: the SDK's own timers on the virtual clock. Without it the mount never happens.

```sh
nix develop .#uk-node -c python3 experiments/unkernel_probe/guest_node_check.py           # everything, about 1 min 45 s
nix develop .#uk-node -c python3 experiments/unkernel_probe/guest_node_check.py --quick   # what `nix flake check` runs (uk-node), about 1 min
nix develop .#uk-node -c python3 experiments/unkernel_probe/guest_node_check.py --record  # rewrite evidence/guest_node_*
nix build .#checks.aarch64-darwin.uk-node -L                                              # the quick run in the sandbox, no network
```

All numbers below are from one `--record` run on aarch64-darwin, in [evidence/guest_node_summary.json](evidence/guest_node_summary.json); [evidence/guest_node_console.txt](evidence/guest_node_console.txt) is the console of that session up to the end of the row comparison. No binary is committed: the check rebuilds them and compares their digests with the record.

### The files

* [guest_node_build.py](guest_node_build.py) generates `node.c`, builds the native reference and the aarch64 programs, and derives the rows and the expected outputs.
* [guest_node.mjs](guest_node.mjs) boots once with files in the guest. It uses host.mjs unchanged, adds the delivery (`--deliver initrd | mount | none`), `--host-timers virtual`, and `marks`: the instruction count and the real time at each delivery of a console line.
* [guest_node_history.c](guest_node_history.c) drives recorded histories through the node in the guest; [guest_node_bench.c](guest_node_bench.c) runs the step in a loop without I/O. Both `#include` the generated `node.c`.
* [guest_node_check.py](guest_node_check.py) runs everything here and fails if any line is not as stated.

### The binary

| | |
|---|---|
| Toolchain | `zig` 0.16.0 of the flake's nixpkgs (substituted for aarch64-darwin from cache.nixos.org; `ZIG` in the `uk-node` shell). No Linux builder is used |
| Command | `$ZIG cc -target aarch64-linux-musl -std=c11 -O2 -fbracket-depth=8000 -static -mcpu=generic -s -Wl,--build-id=none -ffile-prefix-map=OUT=. -DNODE_MAIN OUT/node.c -o OUT/node.aarch64` |
| Input | `node.c`, 420 lines, sha256 `baa253d8…01610060` |
| Output | ELF aarch64, statically linked, stripped, 37728 bytes, sha256 `3df86291…1d7f01d7` |
| Reproducible | yes: a second build with an empty compiler cache has the same digest, and so did every build of this session, in several directories and in the Nix sandbox |

The history driver is 37912 bytes (`6eaa071b…`), the step loop 36712 bytes.

### Delivery

| Path | Works | What it adds |
|---|---|---|
| initramfs: a cpio archive (newc, all times and owners zero) appended to the kernel's bundled initramfs, passed as `initrd` with the bundled command line | yes | no import call and no host I/O: the bytes are in memory before the first instruction. 1494448 bytes of base initramfs become 1696176. `sha256sum` in the guest gives the host's digest for each of the 4 files |
| 9p over vsock: the SDK's `MemoryFs` as `mounts`, `vsock` on | only with `--host-timers virtual` | 337 vsock import calls in the session (`wasm_driver_vsock_send` 128, `_rx_len` 137, `_recv` 71, `_init` 1), 203393 bytes to the guest and 1068 from it, and 78 SDK timers of which 1 fires |

Why 9p fails as it is. Observed: the host's first 44-byte vsock packet reaches the guest during boot (import calls 1316 and 1317, `wasm_driver_vsock_rx_len()=44` and `wasm_driver_vsock_recv(…,44)=44`), the guest answers in call 1318, and the SDK counts the connection as refused (`refused: 1`, `connected: 0`). After that the only vsock import call of the whole run is one more `rx_len` (call 1319), 4 timers are left to the real clock, and `/node` does not exist when the script runs. Read from the SDK's source: its `connectRetry` waits between attempts with `setTimeout`, which is real time here, while the rest of the machine runs on the virtual clock and finishes its 40 virtual seconds first. Two such boots are identical to each other; they are just not the run that was asked for.

The control: `guest_node.mjs --host-timers virtual` replaces `setTimeout` before host.mjs is installed, so every timer host.mjs does not capture itself goes to a queue ordered by virtual deadline and then by creation, and is fired at the first clock read at or after its deadline. With it the retry fires once, the mount is served, and the node runs. Proof that it is needed: the row above without it.

### Equivalence

The rows are the 163 distinct (state, event) pairs of the 500-seed simulation (12800 steps), as `tool/examples/accepted_job/check.py` derives them, none of which traps, followed by 6 rows that do (no input, a state without an event, a truncated state, an undeclared event tag, symbols in no table, `INT64_MAX` everywhere).

| Compared | Result |
|---|---|
| `/node/node < /node/vectors.txt` on the console against the native `node` on this Mac | 169 of 169 lines equal, 6 of them `trap 2`, `trap 2`, `trap 2`, `trap 1`, `trap 1`, `trap 1`; exit status 0. The only normalization is the terminal's `\r\n` to `\n` |
| The same through `sha256sum` inside the guest (no terminal) | `27239072…25305321`, the digest of the native output |
| All 500 recorded traces through `/node/history` (976 state lines for boots and crashes, 5913 steps; the driver carries the state between steps) | 5913 of 5913 lines equal, digest `0cb4cfad…fd30d4e4`. Each expected line is the native node's output, asserted at build time to decode to the post-state and effects the simulator recorded for that step |
| The same two comparisons with the files on the 9p mount | equal |

### Determinism

One session: the five console lines of `SESSION` in guest_node_check.py, 40 virtual seconds, 6082 node steps, 227744 console bytes. Each process is fresh.

| Run | Boots | Result |
|---|---|---|
| initramfs, JIT off | 3 | identical: memory `444366e2…`, console `450a710c…`, 38082 import calls (`d510dcc4…`), 195301657 instructions, 999 idle waits |
| initramfs, JIT on | 3 | identical: memory `576c5472…`, 38133 import calls, 41 blocks compiled, 10 freed, 0 refused. Console and instruction count as with the JIT off |
| initramfs, clock driven by instructions (`--ns-per-insn 5`) | 2 | identical: memory `8eb0e0b2…`, 116555 import calls, 195751999 instructions |
| 9p mount, SDK timers virtual, JIT off | 3 | identical: memory `3abd7228…`, 41006 import calls, 195367115 instructions |
| 9p mount, SDK timers virtual, JIT on | 2 | identical: memory `23c25209…`, 41052 import calls. Console and instruction count as with the JIT off |

Every line of every session was delivered to an idle guest, and no timer was left to the real clock.

Controls removed again (`--fault`, one boot each against a good one; in the recorded run): `entropy` changes memory, imports and import calls; `date` changes memory, imports, import calls and instruction count; `jitter` changes memory, console, imports, import calls and instruction count; `idle-real` leaves a timer to the real clock. All four are rejected. Which digests move varies from run to run for `entropy` and `jitter`, since what they let in is real; memory and imports moved every time.

### Teeth

| Change | What the comparison reports |
|---|---|
| Node built from the mutant `reply-before-ack` of spec.shen, same rows | 12 of 169 lines differ from the native node, first at line 4: guest `2 0 0 2 0 0 5 0 0 7 22 0 1 2 14 0 0 7 22 15 0 6 0`, native `2 0 0 2 0 0 5 0 0 7 22 0 1 1 14 0 0 7 22`. The guest's lines equal the native mutant's |
| One byte of vectors.txt (offset 3566, the first digit from the middle on that the native output depends on) | memory, console, imports, import calls and instruction count change; line 89 differs from the expected output; the guest still equals the native node on the changed rows |

A first choice of byte (offset 3564) changed every digest but no output line: that digit is a field the step does not read on that row.

### Cost of a step

`perf.txt` is the 163 distinct rows 200 times over (32600 rows). The cost per row is the difference between `/node/node < /node/perf.txt > /dev/null` and `/node/node < /dev/null`, taken from `marks`. The step alone is the difference between 2001 passes and 1 pass of `/node/bench` over the 163 rows (326000 steps). Wall times are of one run on this machine and move by 10 to 20 % between runs; instruction counts do not move.

| | guest instructions | interpreter | JIT | native on this Mac |
|---|---|---|---|---|
| One row through the line executable (parse, step, print, flush) | 14071.1 | 38.8 µs | 18.1 µs (19.6 µs on the first run, which compiles) | 1.72 µs |
| One step alone | 355.6 | 790 ns | 290 ns (322 ns on the first run) | 245 ns |
| Boot to the first prompt | 3750863 | | | |
| An empty run of the node (exec to exit) | 58020 to 58975 | | | |

For comparison, the same step called from Python as the simulator calls it: 1.17 µs through the native library and 16.6 µs through wasmtime, both dominated by the call path, not by the step. The timing session (1667772817 guest instructions in all) took 4.45 s from its first console line to its end on the interpreter and 2.07 s with the JIT, about 375 and 806 million guest instructions per second. One whole process, boot and a 12 virtual second session of 338 rows, took 1.25 s including `nix develop`.

So a node step inside the guest costs about what a wasmtime step costs the simulator today, when the node is spoken to over stdio; by that rate the 12800 steps of the 500-seed run would be about 0.23 s with the JIT (arithmetic, not a measurement). The price of whole-system simulation is the boot and the kernel around the step, not the step.

The two runs of the same command in one boot differ by 132 instructions (458776604 and 458776472); both numbers are the same in every boot and with the JIT. The native figure for the step alone was not broken down: it is higher per instruction than the guest's JIT, which suggests a cost outside the step (Darwin's `setjmp` is the suspect, unverified).

### What this does not show

* **The node here is a filter on stdin and stdout.** The real `tool/host` (sockets, an fsync'd log, timers, the event log) was not run in the guest. It needs Python and wasmtime for aarch64-linux: both are in cache.nixos.org for the flake's nixpkgs (`python3-3.14.7`, closure 238 MB; `python3.14-wasmtime-48.0.0`, closure 289 MB), which does not fit a 128 MiB guest's initramfs and would need a disk image or the 9p mount and more memory. Whether wasmtime's own code generator runs under the guest's emulated ARMv8.0 CPU was not tried.
* **No disk, no network.** The node's effects were printed, not performed. Sibling probes cover the block and the network device.
* **One example, one mutant, one CPU, one machine.** `accepted_job` and `reply-before-ack` only; digests were not compared across machines or Node versions.
* **The import log does not digest vsock payloads**, only the calls' arguments and return values (host.mjs was left as it is). The memory digest covers what the guest received.
* **Virtual SDK timers fire only at clock reads.** A timer due while the guest computes without reading the clock fires late in virtual time, though at the same point in every run. Only the mount's one retry exercised this; `vm.exec`, `vm.spawn` and writes to the mount were not used.
* **The driver of histories replays what the simulator decided.** It shows the compiled step agrees with the record on every step of 500 histories when it carries its own state; it does not schedule, crash or drop anything inside the guest.

## 9. Network: one guest, then three on one switch

```sh
nix develop .#unkernel -c python3 experiments/unkernel_probe/net_check.py            # 39 processes, about 45 s; `nix flake check` runs it (unkernel-net-probe)
nix develop .#unkernel -c python3 experiments/unkernel_probe/net_check.py --record   # rewrite evidence/net_*
```

**Verdict, single guest: deterministic with the four controls of section 3 plus one delivery rule.** Three fresh processes agree on memory, console, import log, instruction count and the ordered frame trace, with frames reaching an idle guest and a running one, with the JIT on, and under random real delays in the host.

**Verdict, several guests: deterministic in one thread.** Three kernels in one thread, run one at a time by a coordinator that always resumes the guest furthest behind in virtual time, agree across three fresh processes on every guest's memory, console, import log and instruction count and on the global frame trace. The same holds with seeded delay, drop, duplication and a partition at the switch. One guest per worker thread was not built.

All numbers are from one `net_check.py --record` on aarch64-darwin, in [evidence/net_summary.json](evidence/net_summary.json), which names the package and the hashes of the sources it was recorded from.

### What the pinned package gives

* The base system's busybox has `ip`, `ifconfig`, `route`, `arp`, `arping`, `ping`, `nc` (`-l` and `-u` work), `wget`, `udhcpc`, `traceroute`, `netstat` and `nslookup`. `eth0` is up at boot, and three DHCP discovers leave it (at 1.6 ms, 1.0 s and 2.0 s of virtual time; the frames carry the vendor string `udhcp 1.37.0`). Nothing here answers them. The scripts set the address with `ip addr add`.
* `net:` accepts a raw-frame `NetDevice` of our own: `send(frame)` is called synchronously inside the import `wasm_driver_net_send`, and `Kernel.receiveFrame(frame)` queues a frame, sets the IRQ bit and wakes an idle wait. The SDK's `NetStack` is not used, so none of its code is involved.
* **Two kernels in one thread work.** [multi_stock.mjs](multi_stock.mjs) creates two at once on the stock host; both boot and answer a typed line (`{"created":2,"answered":[true,true]}`). The package's README says "Only one kernel can run per thread".
* **The SDK's worker mode refuses a device of our own.** `createKernel({ worker: true, net: device })` fails with `BootError: createKernel({ worker: true }): net is a NetDevice or NetBackend object, which cannot cross to the worker; pass a spec instead: { kind: "net", backend: { kind: "direct" } } on Node, or { kind: "net", backend: { kind: "wisp", servers } }` (`checkWorkerOptions`, `dist/_chunks/core.mjs:14826`). That was tried once by hand and is not in net_check.py. The SDK's worker also runs the package's own entry, in a thread where host.mjs has replaced nothing. Worker threads of our own, each installing the controls and calling `createKernel` itself, were not tried.

### The delivery rule

The suspected source of nondeterminism was when a received frame lands relative to guest instructions. Under the controlled host the guest never returns to the event loop while it can run, so a frame can only land inside an import, and which import is ours to choose:

* every frame for a guest gets a virtual due time when it is sent: the sender's virtual time plus a fixed latency (200000 ns unless stated);
* it is handed over by `receiveFrame` in the first import of the receiver at which its virtual clock has reached that time: a clock read if the guest is running, the end of the idle wait if it is not (the wait's jump is cut short at the due time);
* never at the start of an idle wait: an IRQ bit set after the kernel decided to idle and before the SDK's wait exists would wake nothing.

host.mjs got a `device` hook for this and logs a digest of every frame sent and taken (9 added lines). [net.mjs](net.mjs) is the single guest. [netlib.mjs](netlib.mjs) has the frame trace and the scripted peer: 10.9.0.1, answering ARP, ICMP echo, UDP port 7, and TCP port 7007 with fixed initial sequence numbers (one answer, then FIN). [switch.mjs](switch.mjs) is the coordinator and the switch. No host socket is opened, and no timer is left to the real clock (`realTimers` is 0 in every record).

A running guest gets a frame at its next clock read, which in the single-guest busy run was up to 853810 ns after the due time.

### Single guest ([net_workload.json](net_workload.json), [net_busy.json](net_busy.json))

The guest sets 10.9.0.2/24, pings the peer three times, sends a UDP line with `nc -u` and a TCP line with `nc`, and prints the answers (`echo:hello-udp`, `echo:hello-tcp`), its neighbour table and `/proc/net/dev`. The busy script pings five times at 0.2 s, and later does the TCP exchange, in the background while the shell runs a counting loop, with `--ns-per-insn 5`.

| Run | Boots | Result |
|---|---|---|
| idle, to virtual 14 s | 3 fresh | identical: memory `9b695641…`, console `092c297e…` (6221 bytes), imports `3b047807…`, trace `d67404f9…`; 14342 import calls, 5319189 instructions, 554 idle waits, 13 frames sent and 8 received (7 at the end of an idle wait, 1 at a clock read) |
| busy, to virtual 10 s | 3 fresh | identical: memory `fe2623aa…`, trace `bd4cdd62…`; 15795 import calls, 419464567 instructions, 14 frames sent, 9 received, all 9 at a clock read of a running guest |
| busy, JIT on | 2 fresh | identical: memory `96445ace…`, 15952 import calls; console, instruction count and trace as with the JIT off |
| attack: a random real delay of 0 to 3 ms (busy wait, `crypto.randomInt`) in every send and every delivery; idle and busy | 1 each | identical to the rows above in all five values and the trace |
| host fault `yield` (the SDK's own `sched_yield`, which returns to the event loop) | 1 | not rejected: only the import log differs |
| host fault `math-random` | 1 | not rejected: nothing differs |
| host faults `idle-real`, `entropy` | 1 each | rejected, as in section 4 |
| **fault `real-latency`**: the due time also gets 0 to 1 ms from the real clock | 2 fresh | **rejected**: memory, console, imports, instruction count and trace differ between the two |

The first divergence under `real-latency` ([evidence/net_divergence.txt](evidence/net_divergence.txt)) is the first frame for the guest, the ARP reply: delivered at virtual 2020827666 ns in one boot and 2020456291 ns in the other. In the import log it is line 4697, the first clock read after the idle wait that the frame ended. So the delivery rule is needed, and it is the only control the network added.

### Three guests ([multi_workload.json](multi_workload.json), [multi_busy.json](multi_busy.json))

[switch.mjs](switch.mjs) keeps host.mjs's controls per guest, each with a virtual clock of its own; the replaced globals answer for the guest whose import is executing. A guest runs until it parks, at an idle wait or at `sched_yield` (both are JSPI imports: returning a pending promise suspends the guest). The coordinator then resumes the guest with the lowest virtual time, an idle guest counting as being at its next event (timer, console input or frame); ties go to the lower index. Guests are started the same way, so the second kernel is created when the first parks for the first time. Exactly one guest executes at a time, and which one runs next is a function of virtual state only. The switch sends a frame to the port that has the destination address, or to every other port if it is a broadcast.

A (10.9.0.11) pings B three times and sends B a line over TCP; B (`nc -l -p 7007`) answers with a line of its own; C pings A and B twice each. In the busy script A runs a 20000-iteration loop while B pings it five times, then A talks to C over TCP, with `--ns-per-insn 5`.

| Run | Runs | Result |
|---|---|---|
| idle, to virtual 12 s | 3 fresh | identical: memories `2a79d9a6…`, `850960e5…`, `850fec92…`, trace `216c86c8…`; 43 frames sent, 55 deliveries, 1341 changes of guest; 12751 / 11369 / 11794 import calls; every frame delivered at its due time |
| one guest busy, to virtual 8 s | 3 fresh | identical: memories `879d3862…`, `8641569b…`, `ba3b4464…`, trace `9ffdd187…`; A retired 301886186 instructions and was parked at `sched_yield` 66 times; 10 frames reached A at a clock read |
| one guest busy, JIT on | 2 fresh | identical; consoles, instruction counts and trace as with the JIT off |
| attack: random real delays of 0 to 3 ms at every send, delivery and change of guest; idle and busy | 1 each | identical to the rows above |
| fault `real-latency` | 2 fresh | rejected: most of the 16 compared values differ (15 in one recording); the traces differ from line 4, the first delivery |
| seeded switch faults (`--drop 0.1 --dup 0.1 --jitter 300000 --partition 4500:5600`), seed 7 | 2 fresh | identical: trace `c1f0e2ff…`; 55 sent, 62 delivered, 10 dropped, 5 duplicated, 2 cut by the partition. A's ping reports 33 % loss, C's a duplicate, and the TCP exchange still completes with its SYN and two FINs dropped: the guests retransmit under the virtual clock ([evidence/net_multi_faults_seed7.trace](evidence/net_multi_faults_seed7.trace)) |
| the same with seed 8 | 1 | another trace (`03319f06…`: 45 sent, 55 delivered, 5 dropped, 3 duplicated, 2 cut) |

**The latency is not exact for a busy guest.** The kernel calls `sched_yield` only every 20 ms of guest time (`KW_YIELD_NS`, a constant in the kernel's wasm architecture patch), so a busy guest runs up to 20 ms past the others before the coordinator can park it, and a frame sent to it in that window arrives late. The largest lag in the busy run is 19646000 ns, against a latency of 200000 ns. The run is still deterministic, since the lag is a function of virtual state. With `--latency 25000000`, above the 20 ms quantum, the largest lag is 866530 ns (one run). A small bound at a small latency needs `KW_YIELD_NS` to be settable by the host, which is a kernel patch and a rebuild of the wasm; that was not attempted (no source build here).

### Teeth

| Change | Values that change |
|---|---|
| single: one bit of the peer's MAC address in the ARP reply | memory, console, imports, trace |
| single: one bit of the UDP answer (its last byte) | console, imports, import calls, trace; **not the final memory** |
| single: one bit of the padding of an ICMP reply | imports, trace; **not memory, not console** |
| single: one frame delivered 1000 ns (one clock step) later | memory, console, imports, import calls, trace |
| three guests: one bit of one frame (the last byte of delivery 40, for C) | C's memory and imports, trace |
| three guests: that frame 1000 ns later | C's memory, console and imports, trace |

Two rows say something about the witness: the memory at the end did not keep those bytes, so the memory digest alone misses a changed payload. The frame trace and the import log (which has a digest of each frame the guest takes) catch both.

### Wall time

One recording, interpreter unless stated. Wall time runs from the start of the script to the end record, so it includes compiling the kernel.

| Run | Virtual | Wall ms | ms per virtual s | per guest |
|---|---|---|---|---|
| single, idle | 14 s | 169, 189, 176 | 12 | |
| single, busy (419 M instructions) | 10 s | 2155, 2182, 2222 | 216 | |
| single, busy, JIT | 10 s | 917, 953 | 92 | |
| three guests, idle | 12 s | 469, 456, 466 | 39 | 13 |
| three guests, one busy (302 M instructions) | 8 s | 1848, 2073, 2133 | 231 | 77 |
| three guests, one busy, JIT | 8 s | 1110, 1110 | 139 | 46 |

Each guest has 128 MiB of linear memory. The whole check took 45 s in the dev shell and 85 s as the flake check.

### What this does not show

* **One guest per worker thread** (guests in parallel, windows of virtual time) was not built. The one-thread coordinator runs at the speed of one core.
* **More than three guests, longer runs, larger transfers.** The largest exchange is one TCP segment each way: no window, congestion or segmentation-offload behaviour was exercised.
* **Independence from the coordinator's rule.** With a lag above zero the result depends on the order in which guests run. It is the same in every run because the rule is fixed; other rules were not compared.
* **Snapshot and restore with the network device**, and a snapshot of several guests at one virtual time.
* **The SDK's own `NetStack`** (DHCP, DNS, outbound TCP through host sockets) is not used and was not tested.
* **IPv6 and a learning switch.** Each port has one fixed address.
* **Other machines and Node versions**, as in section 5.

## 10. Disk

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
