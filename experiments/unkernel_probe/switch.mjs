// Several guests in one thread, on one virtual L2 switch, each with a virtual clock of its own,
// run one at a time by a coordinator. Writes one record: per guest the digests and counters
// that run.mjs writes, and the digest of the global frame trace.
//
//   node --experimental-wasm-jspi switch.mjs --script multi_workload.json --end 12000 --out a.json
//
// The controls are host.mjs's (clock by clock reads and, with --ns-per-insn, by instructions;
// idle wait off the real clock; seeded entropy; seeded Math.random), kept per guest: the
// globals the SDK reads answer for the guest whose import is executing.
//
// Scheduling. A guest runs until it parks: at an idle wait (poll_oneoff) or at sched_yield
// (both are JSPI imports, so returning a pending promise suspends the guest). The coordinator
// then resumes the guest that is furthest behind in virtual time; an idle guest counts as
// being at the time of its next event (its timer, its next console input, its next frame).
// Ties go to the lower index. Exactly one guest executes at any moment and the choice is a
// function of virtual state only, so the interleaving is the same in every run.
//
// The switch. A frame sent at the sender's virtual time t is due at each receiver at
// t + --latency ns (plus seeded faults). It is handed over (Kernel.receiveFrame) at the
// receiver's first clock read or idle wake at or after that time. A receiver that has
// already run past the due time gets it late; the record has the largest such lag.
//
// Faults at the switch, from --fault-seed: --drop P and --dup P (probabilities), --jitter NS
// (extra delay, uniform), --partition A:B (ms: frames to or from guest 0 sent in that window
// are dropped). Teeth: --flip N flips one bit of the N-th delivery, --late N delays it by
// --late-ns. --net-fault real-latency adds 0..1 ms of the real clock to every due time;
// --net-fault spin busy-waits a random real time at every send, delivery and park.
import { createHash, randomInt } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { icount } from "./host.mjs";
import { Trace, macOf, prng, sha } from "./netlib.mjs";

const argv = process.argv.slice(2), arg = {};
for (let i = 0; i < argv.length; i += 2) {
	if (!argv[i].startsWith("--") || i + 1 >= argv.length) throw new Error(`expected --key value at "${argv[i]}"`);
	arg[argv[i].slice(2)] = argv[i + 1];
}
const num = (k, d) => (arg[k] === undefined ? d : Number(arg[k]));
const spec = JSON.parse(readFileSync(arg.script, "utf8"));
const realNow = performance.now.bind(performance), started = realNow();
const realSetTimeout = globalThis.setTimeout, realClearTimeout = globalThis.clearTimeout;
const stepNs = num("step", 1000), nsPerInsn = num("ns-per-insn", 0), endNs = num("end", 12000) * 1e6, latency = num("latency", 200000);
const netFault = arg["net-fault"], flip = num("flip", -1), late = num("late", -1);
const dropP = num("drop", 0), dupP = num("dup", 0), jitter = num("jitter", 0);
const partition = arg.partition ? arg.partition.split(":").map((x) => Number(x) * 1e6) : null;
const frand = prng(num("fault-seed", 1)), chance = (p) => p > 0 && frand() / 4294967296 < p;
const spin = () => { for (const until = realNow() + randomInt(3000) / 1000; realNow() < until;); };
const EPOCH = 1767225600000;

const trace = new Trace();
const guests = spec.guests.map((s, i) => ({
	i, name: `g${i}`, script: s.script, state: "unborn", done: false, vns: 0, insns: null, inputAt: 0,
	rand: prng(num("seed", 1) * 1000 + i), mrand: prng((num("seed", 1) * 1000 + i) ^ 0x5bd1e995),
	queue: [], chunks: [], calls: 0, counts: {}, full: createHash("sha256"), idles: 0, parks: 0, capturedTimers: 0,
	frames: { tx: 0, rx: 0 }, from: { clock: 0, wake: 0 }, maxLag: 0, inputFrom: { clock: 0, wake: 0 },
	memory: null, vm: null, kernel: null, idle: null, resume: null, result: null,
}));
let cur = guests[0], booting = null, capture = null, deliveries = 0, realTimers = 0, switches = 0, finished = false;

// The globals the SDK reads: they answer for the guest whose import is executing.
performance.now = () => cur.vns / 1e6;
Date.now = () => EPOCH + Math.floor(cur.vns / 1e6);
Math.random = () => cur.mrand() / 4294967296;
globalThis.setTimeout = (fn, ms, ...rest) => {
	if (capture) return capture.push({ fn, ms: ms ?? 0 }), cur.capturedTimers++, { captured: true };
	realTimers++;
	return realSetTimeout(fn, ms, ...rest);
};
globalThis.clearTimeout = (t) => (t && t.captured ? undefined : realClearTimeout(t));

/** The switch: every other guest's port, or the one that owns the destination address. */
function forward(g, frame) {
	if (netFault === "spin") spin();
	g.frames.tx++;
	trace.add("tx", g.name, g.vns, frame);
	const broadcast = frame[0] & 1;
	for (const to of guests) {
		if (to === g || (!broadcast && !to.mac.every((b, i) => frame[i] === b))) continue;
		if (partition && (g.i === 0 || to.i === 0) && g.vns >= partition[0] && g.vns < partition[1]) {
			trace.add("cut", to.name, g.vns, frame);
			continue;
		}
		if (chance(dropP)) {
			trace.add("drop", to.name, g.vns, frame);
			continue;
		}
		const copies = chance(dupP) ? 2 : 1;
		if (copies === 2) trace.add("dup", to.name, g.vns, frame);
		for (let copy = copies; copy > 0; copy--) {
			const n = deliveries++, f = frame.slice();
			let due = g.vns + latency + (jitter ? frand() % jitter : 0);
			if (netFault === "real-latency") due += Math.floor(realNow() * 1e6) % 1000000;
			if (n === late) due += num("late-ns", 1000);
			if (n === flip) f[f.length - 1] ^= 1;
			to.queue.push({ due, frame: f, n });
			to.queue.sort((a, b) => a.due - b.due || a.n - b.n);
		}
	}
}

/** Hand `g` what is due at its virtual time: frames, then console input. */
function pump(g, where) {
	while (g.queue.length && g.queue[0].due <= g.vns) {
		const { due, frame } = g.queue.shift();
		if (netFault === "spin") spin();
		trace.add("rx", g.name, g.vns, frame, `due ${due} at ${where}`);
		g.frames.rx++;
		g.from[where]++;
		g.maxLag = Math.max(g.maxLag, g.vns - due);
		if (g.kernel) g.kernel.receiveFrame(frame);
		else trace.add("lost", g.name, g.vns, frame); // no driver yet
	}
	while (g.inputAt < g.script.length && g.vns >= g.script[g.inputAt][0] * 1e6) {
		if (!g.vm) throw new Error(`${g.name}: input due before createKernel() returned`);
		g.vm.write(g.script[g.inputAt++][1]);
		g.inputFrom[where]++;
	}
}

/** Where an idle guest's next event is. */
const wakeAt = (g) => Math.min(g.idle.deadline, endNs,
	g.inputAt < g.script.length ? Math.max(g.vns, g.script[g.inputAt][0] * 1e6) : Infinity,
	g.queue.length ? Math.max(g.vns, g.queue[0].due) : Infinity);
const key = (g) => (g.state === "idle" ? wakeAt(g) : g.vns);

function choose() {
	let best = null;
	for (const g of guests) {
		if (g.done) continue;
		if (!best || key(g) < key(best)) best = g;
	}
	return best;
}

function markDone(g) {
	const mem = new Uint8Array(g.memory.buffer), out = Buffer.concat(g.chunks);
	g.done = true;
	g.result = {
		memoryBytes: mem.length, memory: sha(mem), console: sha(out), consoleBytes: out.length,
		imports: g.full.digest("hex"), importCalls: g.calls, counts: g.counts, icount: icount(g.vm, g.memory),
		virtualNs: g.vns, idles: g.idles, parks: g.parks, capturedTimers: g.capturedTimers,
		inputsDelivered: g.inputAt, inputFrom: g.inputFrom, frames: g.frames, framesFrom: g.from, framesUndelivered: g.queue.length, maxLagNs: g.maxLag,
	};
	if (arg.console) writeFileSync(`${arg.console}.${g.name}`, out);
}

/** Give the processor to the guest that is furthest behind. Returns it (it may be the caller). */
function dispatch(from) {
	for (;;) {
		const g = choose();
		if (!g) return finish(), null;
		if (g === from && g.state === "run") return g;
		if (netFault === "spin") spin();
		switches++;
		if (g.state === "unborn") {
			g.state = "run";
			start(g);
		} else if (g.state === "yield") {
			g.state = "run";
			const r = g.resume;
			g.resume = null;
			cur = g;
			r(0);
		} else { // idle: move its clock to its next event and wake it
			const idle = g.idle;
			g.vns = Math.max(g.vns, wakeAt(g));
			if (g.vns >= endNs) {
				markDone(g);
				continue;
			}
			g.state = "run";
			g.idle = null;
			cur = g;
			pump(g, "wake"); // a frame or input wakes the wait through Kernel.wake
			if (g.vns >= idle.deadline) for (const t of idle.timers) t.fn();
			else if (g.kernel?.idleWake) throw new Error(`${g.name}: woken with nothing due`);
		}
		return g;
	}
}

const never = () => new Promise(() => {});
const impl = (g) => ({
	clock_time_get: (orig) => (id, precision, time) => {
		g.vns += stepNs;
		if (nsPerInsn && g.vm) {
			const n = icount(g.vm, g.memory);
			if (n === null) throw new Error("--ns-per-insn: the instruction counter cannot be read");
			g.vns += (n - (g.insns ?? n)) * nsPerInsn;
			g.insns = n;
		}
		pump(g, "clock");
		const r = orig(id, precision, time);
		if (g.vns >= endNs) markDone(g); // it runs on, unlogged, to its next park and stays there
		return r;
	},
	random_get: () => (buf, len) => {
		const u8 = new Uint8Array(g.memory.buffer);
		for (let i = 0; i < len; i++) u8[buf + i] = g.rand() & 0xff;
		return 0;
	},
	sched_yield: () => () => {
		if (dispatch(g) === g) return 0;
		g.parks++;
		g.state = "yield";
		return new Promise((r) => (g.resume = r));
	},
	poll_oneoff: (orig) => (...args) => {
		capture = [];
		let r, timers;
		try {
			r = orig(...args);
		} finally {
			timers = capture;
			capture = null;
		}
		if (!(r instanceof Promise)) return r;
		g.idles++;
		g.state = "idle";
		g.idle = { timers, deadline: timers.length ? g.vns + Math.ceil(Math.min(...timers.map((t) => t.ms)) * 1e6) : Infinity };
		dispatch(g);
		return r;
	},
});

const digest = (g, p, n) => createHash("sha256").update(new Uint8Array(g.memory.buffer).subarray(Number(p), Number(p) + Number(n))).digest("hex").slice(0, 16);
const iov = (g, args, limit) => {
	const dv = new DataView(g.memory.buffer), u8 = new Uint8Array(g.memory.buffer), h = createHash("sha256");
	for (let i = 0; i < args[2] && limit > 0; i++) {
		const p = dv.getUint32(args[1] + 8 * i, true), n = Math.min(limit, dv.getUint32(args[1] + 8 * i + 4, true));
		h.update(u8.subarray(p, p + n));
		limit -= n;
	}
	return h.digest("hex").slice(0, 16);
};
const extraOf = {
	clock_time_get: (g, args) => new DataView(g.memory.buffer).getBigUint64(args[2], true).toString(),
	random_get: (g, args) => Buffer.from(new Uint8Array(g.memory.buffer).slice(args[0], args[0] + Math.min(args[1], 8))).toString("hex"),
	fd_write: (g, args) => iov(g, args, Infinity),
	fd_read: (g, args, ret) => (ret === 0 ? iov(g, args, new DataView(g.memory.buffer).getUint32(args[3], true)) : ""),
	wasm_driver_net_send: (g, args) => digest(g, args[0], args[1]),
	wasm_driver_net_recv: (g, args, ret) => (args[0] ? digest(g, args[0], ret < args[1] ? ret : args[1]) : ""),
};

function wrap(g, name, fn) {
	const mine = impl(g)[name]?.(fn) ?? fn, extra = extraOf[name], parks = name === "sched_yield" || name === "poll_oneoff";
	let log;
	if (arg.log) log = (line) => g.logLines.push(line);
	return (...args) => {
		if (finished) return parks ? never() : fn(...args);
		if (g.done) { // past its end: other imports pass through; a park is for good, and the next guest runs
			if (!parks) return (cur = g), fn(...args);
			g.state = "parked";
			dispatch(null);
			return never();
		}
		if (g.state !== "run") throw new Error(`${g.name} executes ${name} while ${g.state} (running: ${cur.name})`);
		cur = g;
		const r = mine(...args);
		const line = `${name}(${args.join(",")})=${r instanceof Promise ? "P" : r}`, x = extra ? extra(g, args, r) : "";
		if (!g.done) {
			g.full.update(line + x);
			g.counts[name] = (g.counts[name] ?? 0) + 1;
			g.calls++;
			log?.(`${line} ${x} @${g.vns}`);
		}
		return r;
	};
}

const Suspending = WebAssembly.Suspending, inner = new WeakMap();
WebAssembly.Suspending = function (fn) {
	const s = new Suspending(fn);
	inner.set(s, fn);
	return s;
};
WebAssembly.Suspending.prototype = Suspending.prototype;
const instantiate = WebAssembly.instantiate;
WebAssembly.instantiate = function (module, imports) {
	if (imports?.wasi_snapshot_preview1 && imports.unkernel) {
		const g = booting;
		if (!g || g.memory) throw new Error("a kernel is instantiated for no guest");
		g.memory = imports.env.memory;
		for (const ns of ["wasi_snapshot_preview1", "unkernel"])
			for (const [name, v] of Object.entries(imports[ns])) {
				if (typeof v === "function") imports[ns][name] = wrap(g, name, v);
				else if (inner.has(v)) imports[ns][name] = new Suspending(wrap(g, name, inner.get(v)));
			}
	}
	return instantiate.call(this, module, imports);
};

const { createKernel } = await import(`${process.env.UNKERNEL}/dist/node.mjs`);

function start(g) {
	booting = cur = g;
	g.mac = macOf(g.i + 1);
	if (arg.log) g.logLines = [];
	createKernel({
		jit: !!num("jit", 0), cpus: 1, vsock: false, memoryMiB: num("mem", 128),
		net: { mac: g.mac, attach(k) { g.kernel = k; }, send(frame) { if (!g.done) forward(g, frame); } },
		log: (m) => g.chunks.push(Buffer.from(`${m}\n`)),
		onOutput: (b) => g.chunks.push(Buffer.from(b)),
	}).then((vm) => {
		g.vm = vm;
		vm.exited.then((res) => fail(`${g.name} exited: ${res.kind} ${res.code} ${res.reason}`));
	}, (e) => fail(`${g.name}: createKernel failed: ${e?.stack ?? e}`));
}
for (const g of guests) g.mac = macOf(g.i + 1);

function fail(why) {
	if (finished) return;
	finished = true;
	for (const g of guests) if (arg.console) writeFileSync(`${arg.console}.${g.name}`, Buffer.concat(g.chunks));
	writeFileSync(arg.out, JSON.stringify([{ failed: why, guests: guests.map((g) => ({ state: g.state, virtualNs: g.vns, calls: g.calls })) }], null, 1) + "\n");
	console.error(why);
	process.exit(3);
}

function finish() {
	if (finished) return;
	finished = true;
	if (arg.trace) writeFileSync(arg.trace, trace.text());
	if (arg.log) for (const g of guests) writeFileSync(`${arg.log}.${g.name}`, g.logLines.join("\n") + "\n");
	const wallMs = realNow() - started;
	writeFileSync(arg.out, JSON.stringify([{
		why: "end time", guests: guests.map((g) => g.result), trace: trace.digest(), frames: trace.counts,
		switches, realTimers, netFault, flip, late, latencyNs: latency, nsPerInsn, stepNs, seed: num("seed", 1), jit: !!num("jit", 0),
		faults: { seed: num("fault-seed", 1), drop: dropP, dup: dupP, jitter, partition: arg.partition },
		node: process.version, wallMs: Math.round(wallMs), wallMsPerVirtualS: Math.round(wallMs / (endNs / 1e9)),
		wallMsPerVirtualSPerGuest: Math.round(wallMs / (endNs / 1e9) / guests.length),
	}], null, 1) + "\n");
	for (const g of guests) try { g.vm?.stop(); } catch {}
	process.exit(0);
}

// Nothing may be left to the real event loop: if it drains with guests unfinished, say so.
process.on("beforeExit", () => fail(`the event loop drained: ${guests.map((g) => `${g.name} ${g.state} at ${g.vns}`).join(", ")}`));
dispatch(null);
