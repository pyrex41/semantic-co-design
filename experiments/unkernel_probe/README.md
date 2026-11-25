# unkernel probe: is Linux-as-wasm a deterministic simulation substrate?

[unkernel](https://github.com/pithings/unkernel) is Linux built as one wasm module, with aarch64 user programs emulated inside it, driven by a JavaScript host under Node. This probe asks one question:

> With the host's clock and entropy under our control, do two boots of the same guest running the same scripted workload reach bit-identical state?

**Verdict: deterministic with the listed controls**, for one CPU, console-only I/O, no disk, no network, no vsock. Section 8 adds the network device: one guest against a scripted peer, and three guests on one virtual switch, are deterministic too, with one more control (when a frame is handed to the guest). Under the controlled host every boot of the workload ends with the same 128 MiB of linear memory, the same console bytes, the same sequence of host import calls and the same count of retired guest instructions: across processes, with the JIT off and on, and across snapshot and restore. That holds when every input reaches an idle guest (the main workload) and when input and the snapshot request reach a guest in the middle of a busy loop (a second, shorter workload). Under the stock host two boots differ, as expected. Nothing inside the pinned package is edited; the controls are about 230 lines wrapped around it.

Four controls carry the result: the virtual clock, the virtual date, the idle wait taken off the real clock, and seeded entropy. Removing any one of them makes check.py fail. Two more are in the shim but are not shown to be needed by these workloads: `sched_yield` returning at once, and the seeded `Math.random` (section 3).

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

* **Disks, vsock and SMP were off** (the network device is section 8). The SDK completes block I/O from promises and raises the IRQ whenever they settle; `exec`/`spawn` and host mounts run over vsock the same way. Under this controlled host the guest never returns to the event loop while it can run, so such a completion would be seen only at an idle wait, and the shim's "idle with no timer means quiescent" rule would be wrong. Each device needs its own rule for when a completion is delivered in virtual time. SMP runs CPUs on threads and is out of scope.
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

It looks feasible; this section is the argument made before it was built. Section 8 builds the one-thread design and runs it.

* The SDK's `NetDevice` is two calls: the guest's frames arrive at `send(frame)` synchronously inside an import, and `Kernel.receiveFrame(frame)` queues a frame and sets an IRQ bit. A switch is a function from (sender, frame, virtual send time) to (receiver, virtual delivery time).
* Each guest is deterministic given the virtual times at which its inputs arrive. Section 4 shows that for console input, both for an idle guest and, in the running-guest rows, for a guest interrupted inside one busy loop under the instruction-driven clock. A frame would be delivered from the same two imports, through `receiveFrame` instead of `sendInput`; that path was not run.
* "One kernel per thread" then gives two designs. In one thread, the shim parks a guest by returning a pending promise from `sched_yield` or the idle wait (both are JSPI imports; a busy guest yields every 20 ms of its own time) and always resumes the guest whose clock is furthest behind; this is a sequential discrete-event loop and is deterministic by construction, at the speed of one core. With one worker thread per guest, the guests run in parallel and block in their clock import (`Atomics.wait` is allowed in a worker) at the end of each window of virtual time; frames are exchanged between windows, which is deterministic as long as the link latency is at least one window (conservative parallel simulation with lookahead).
* What is unknown: whether two kernels in one thread interleave correctly (the SDK's notes say its API tests run concurrently, its README says only one kernel can run per thread), the cost of 128 MiB of linear memory per guest, and whether the guest's network stack has a time source this probe did not exercise.

## 8. Network: one guest, then three on one switch

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
