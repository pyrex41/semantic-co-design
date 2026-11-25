// Controlled host for unkernel: installed before the SDK is imported, it replaces what the
// SDK reaches through globals and wraps the kernel's imports at WebAssembly.instantiate.
// Nothing inside the pinned package is edited.
//
//   stock       every import is logged and otherwise passed through.
//   controlled  one virtual clock (integer ns) is the only time there is: it moves by `stepNs`
//               on each guest clock read (and by `nsPerInsn` for each user instruction retired
//               since the last read, if set) and jumps to the deadline when the guest idles;
//               entropy is a seeded generator; sched_yield returns at once; console input and
//               the snapshot request are delivered when the virtual clock passes their times.
//
// cfg.fault removes one control again (check.py's seeded faults): entropy, date, jitter,
// idle-real, icount, yield, math-random.
//
// cfg.devices (optional, disk.mjs) are host devices that complete in virtual time. Each has
// dueNs() (its next virtual deadline, or Infinity), pump(vns, from) (called wherever input is
// delivered) and waiting() (true while it owes the guest an event that only the host's event
// loop will deliver; an idle wait then returns to the event loop with the clock standing still).
import { createHash } from "node:crypto";
import { openSync, writeSync, closeSync } from "node:fs";

const CHECKPOINT = 1 << 16;

/** xorshift128+ over 32-bit halves is overkill here; splitmix32 is enough for a byte stream. */
function prng(seed) {
	let s = seed >>> 0;
	return () => {
		s = (s + 0x9e3779b9) >>> 0;
		let z = s;
		z = Math.imul(z ^ (z >>> 16), 0x85ebca6b);
		z = Math.imul(z ^ (z >>> 13), 0xc2b2ae35);
		return (z ^ (z >>> 16)) >>> 0;
	};
}

/** Retired guest instructions: kwemu's counter, 32 bytes into the cpu that wasm_kw_cpu points at. */
let blind = false; // the seeded fault "icount"
export function icount(vm, memory) {
	if (blind) return null;
	const g = vm?.kernel?.exports?.wasm_kw_cpu;
	if (!(g instanceof WebAssembly.Global)) return null;
	const dv = new DataView(memory.buffer), at = Number(g.value);
	const cpu = vm.kernel.memory64 ? Number(dv.getBigUint64(at, true)) : dv.getUint32(at, true);
	return Number(dv.getBigUint64(cpu + 32, true));
}

export function install(cfg) {
	const controlled = cfg.mode === "controlled", fault = cfg.fault;
	blind = fault === "icount";
	const realNow = performance.now.bind(performance);
	// One run's state. begin() replaces it, so a second boot in the same process starts clean;
	// imports of an earlier kernel (it parks at its next suspension) then pass straight through.
	let st, gen = 0, logFd = null, logBuf = [];
	const begin = (log) => {
		gen++;
		logFd = log ? openSync(log, "w") : null;
		logBuf = [];
		st = {
			vns: 0, calls: 0, counts: {}, idles: 0, realTimers: 0, capturedTimers: 0, checkpoints: [],
			seq: createHash("sha256"), full: createHash("sha256"), memory: null, vm: null,
			inputAt: 0, snapAsked: false, ended: false, insns: null,
			delivered: { clock: 0, poll: 0, jump: 0 }, snapFrom: null,
			rand: prng(cfg.seed), mrand: prng(cfg.seed ^ 0x5bd1e995),
		};
		return st;
	};
	begin(cfg.log);
	const flushLog = () => {
		if (logFd !== null && logBuf.length) writeSync(logFd, logBuf.join(""));
		logBuf = [];
	};
	const record = (name, args, ret, extra) => {
		const line = `${name}(${args.join(",")})=${ret instanceof Promise ? "P" : ret}`;
		st.seq.update(line);
		st.full.update(line + extra);
		st.counts[name] = (st.counts[name] ?? 0) + 1;
		if (logFd !== null) {
			logBuf.push(`${line} ${extra}\n`);
			if (logBuf.length >= 4096) flushLog();
		}
		if (++st.calls % CHECKPOINT === 0) st.checkpoints.push(st.full.copy().digest("hex").slice(0, 16));
		if (cfg.maxCalls && st.calls >= cfg.maxCalls) end("call budget");
	};
	const end = (why) => {
		if (st.ended) return;
		st.ended = true;
		flushLog();
		if (logFd !== null) closeSync(logFd);
		logFd = null;
		cfg.onEnd(why, st);
	};

	const realSetTimeout = globalThis.setTimeout, realClearTimeout = globalThis.clearTimeout;
	let capture = null;
	if (controlled) {
		if (fault !== "math-random") Math.random = () => st.mrand() / 4294967296;
		performance.now = () => st.vns / 1e6;
		if (fault !== "date") Date.now = () => cfg.epochMs + Math.floor(st.vns / 1e6);
		// The idle wait's timer is taken while poll_oneoff runs (below); any other is the host's own.
		globalThis.setTimeout = (fn, ms, ...rest) => {
			if (capture) {
				st.capturedTimers++;
				capture.push({ fn, ms: ms ?? 0 });
				return { captured: true };
			}
			st.realTimers++;
			return realSetTimeout(fn, ms, ...rest);
		};
		globalThis.clearTimeout = (t) => (t && t.captured ? undefined : realClearTimeout(t));
	}

	/** Deliver what the script and the snapshot request have due at the current virtual time.
	 * `from` is the import it happens in: a clock read (the guest is running), the start of an
	 * idle wait, or the jump that ends one. */
	const devices = cfg.devices ?? [];
	const deviceDue = () => Math.min(Infinity, ...devices.map((d) => d.dueNs()));
	const pump = (from) => {
		const script = cfg.script;
		for (const d of devices) d.pump(st.vns, from);
		while (st.inputAt < script.length && st.vns >= script[st.inputAt][0] * 1e6) {
			if (!st.vm) throw new Error("input due before createKernel() returned");
			st.vm.write(script[st.inputAt++][1]);
			st.delivered[from]++;
		}
		if (cfg.snapAtMs !== undefined && !st.snapAsked && st.vns >= cfg.snapAtMs * 1e6) {
			st.snapAsked = true;
			st.snapFrom = from;
			st.vm.snapshot({ stop: true }).then((snap) => cfg.onSnapshot(snap, st), (e) => end(`snapshot failed: ${e.message}`));
		}
		if (cfg.endAtMs !== undefined && st.vns >= cfg.endAtMs * 1e6) end("end time");
	};

	const controlledImpl = {
		clock_time_get: (orig) => (id, precision, time) => {
			st.vns += cfg.stepNs;
			if (fault === "jitter") st.vns += Math.floor(realNow() * 1e6) & 1;
			if (cfg.nsPerInsn && st.vm) {
				// Time as a function of guest progress: each user instruction retired since the first
				// read after createKernel() returned (or after a restore) costs the same.
				const n = icount(st.vm, st.memory);
				if (n === null) throw new Error("--ns-per-insn: the instruction counter cannot be read");
				st.vns += (n - (st.insns ?? n)) * cfg.nsPerInsn;
				st.insns = n;
			}
			pump("clock");
			return orig(id, precision, time);
		},
		random_get: () => (buf, len) => {
			const u8 = new Uint8Array(st.memory.buffer);
			for (let i = 0; i < len; i++) u8[buf + i] = st.rand() & 0xff;
			return 0;
		},
		sched_yield: () => () => 0,
		poll_oneoff: (orig) => (...args) => {
			pump("poll");
			capture = [];
			let r, timers;
			try {
				r = orig(...args);
			} finally {
				timers = capture;
				capture = null;
			}
			if (!(r instanceof Promise)) return r;
			st.idles++;
			// A device owes the guest an event from the host's own event loop: the virtual clock
			// stands still and the wait is left to that event.
			if (devices.some((d) => d.waiting())) return r;
			if (st.snapAsked && !timers.length && deviceDue() === Infinity) return r; // stopped at the snapshot: parked for good
			const waitNs = timers.length ? Math.ceil(Math.min(...timers.map((t) => t.ms)) * 1e6) : Infinity;
			const next = cfg.script[st.inputAt];
			const limits = [waitNs, deviceDue() - st.vns, next ? next[0] * 1e6 - st.vns : Infinity,
				cfg.snapAtMs !== undefined && !st.snapAsked ? cfg.snapAtMs * 1e6 - st.vns : Infinity,
				cfg.endAtMs !== undefined ? cfg.endAtMs * 1e6 - st.vns : Infinity];
			const jump = Math.min(...limits);
			if (jump === Infinity) {
				end("quiescent");
				return r;
			}
			st.vns += Math.max(0, jump);
			pump("jump"); // input wakes the wait through Kernel.sendInput
			// The snapshot IRQ woke the wait already; calling its timer too is harmless.
			if (jump >= waitNs || st.snapAsked)
				for (const t of timers) {
					if (fault !== "idle-real") t.fn();
					else st.realTimers++, realSetTimeout(t.fn, 0);
				}
			return r;
		},
	};

	// What a call gave or took besides its arguments, so the log tells data apart, not only pointers.
	const iov = (args, limit) => {
		const dv = new DataView(st.memory.buffer), u8 = new Uint8Array(st.memory.buffer), h = createHash("sha256");
		for (let i = 0; i < args[2] && limit > 0; i++) {
			const p = dv.getUint32(args[1] + 8 * i, true), n = Math.min(limit, dv.getUint32(args[1] + 8 * i + 4, true));
			h.update(u8.subarray(p, p + n));
			limit -= n;
		}
		return h.digest("hex").slice(0, 16);
	};
	const extraOf = {
		clock_time_get: (args) => new DataView(st.memory.buffer).getBigUint64(args[2], true).toString(),
		random_get: (args) => Buffer.from(new Uint8Array(st.memory.buffer).slice(args[0], args[0] + Math.min(args[1], 8))).toString("hex"),
		fd_write: (args) => iov(args, Infinity),
		fd_read: (args, ret) => (ret === 0 ? iov(args, new DataView(st.memory.buffer).getUint32(args[3], true)) : ""),
		// kwblk's request: device, op, sector, bytes (the data is digested in disk.mjs's own log).
		wasm_driver_block_submit: (args) => {
			const dv = new DataView(st.memory.buffer), p = Number(args[0]);
			return `dev${dv.getUint32(p, true)} op${dv.getUint32(p + 4, true)} sector${dv.getBigUint64(p + 8, true)} bytes${dv.getBigUint64(p + 16, true)}`;
		},
	};

	const dropped = { entropy: "random_get", yield: "sched_yield" }[fault];
	const wrap = (name, fn) => {
		const impl = controlled && controlledImpl[name] && name !== dropped ? controlledImpl[name](fn) : fn;
		const extra = extraOf[name], mine = gen;
		return (...args) => {
			if (mine !== gen || st.ended) return fn(...args);
			const r = impl(...args);
			record(name, args, r, extra ? extra(args, r) : "");
			return r;
		};
	};

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
			st.memory = imports.env.memory;
			for (const ns of ["wasi_snapshot_preview1", "unkernel"])
				for (const [name, v] of Object.entries(imports[ns])) {
					if (typeof v === "function") imports[ns][name] = wrap(name, v);
					else if (inner.has(v)) imports[ns][name] = new Suspending(wrap(name, inner.get(v)));
				}
		}
		return instantiate.call(this, module, imports);
	};
	return { state: () => st, begin, end, realSetTimeout, realNow };
}
