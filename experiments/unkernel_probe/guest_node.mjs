// One boot of the pinned unkernel under the controlled host (host.mjs) with files of ours in the
// guest: the compiled node as a static aarch64-linux program, and the rows it is fed.
//
//   node --experimental-wasm-jspi guest_node.mjs --deliver initrd --file /node/node=OUT/node.aarch64 \
//        --file /node/vectors.txt=OUT/vectors.txt --script session.json --end 20000 --out a.json
//
// --deliver initrd   the files are a cpio archive appended to the kernel's bundled initramfs, so
//                    they are in the root file system before the first instruction; the guest
//                    machine is as in run.mjs (vsock off, no disk, no network).
// --deliver mount    the files are a host file system (the SDK's MemoryFs) mounted at the first
//                    --file's directory, served over 9p on vsock by the SDK (`mounts`); vsock is
//                    on. It needs --host-timers virtual (below) to be mounted at all.
// --deliver none     no files (the machine of run.mjs; the instruction baseline).
//
// A script is [[virtual ms, "console input"], ...] as in run.mjs. The record adds `marks`: the
// instruction count, the real time and the virtual time at each delivery of a script line, so the
// cost of the command between two lines can be read off. Nothing in the pinned package is edited.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { icount, install } from "./host.mjs";

const argv = process.argv.slice(2), arg = {}, files = [];
for (let i = 0; i < argv.length; i += 2) {
	if (!argv[i].startsWith("--") || i + 1 >= argv.length) throw new Error(`expected --key value at "${argv[i]}"`);
	if (argv[i] === "--file") {
		const at = argv[i + 1].indexOf("=");
		files.push([argv[i + 1].slice(0, at), readFileSync(argv[i + 1].slice(at + 1))]);
	} else arg[argv[i].slice(2)] = argv[i + 1];
}
const num = (k, d) => (arg[k] === undefined ? d : Number(arg[k]));
const deliver = arg.deliver ?? "initrd";
const script = arg.script ? JSON.parse(readFileSync(arg.script, "utf8")) : [];
const sha = (b) => createHash("sha256").update(b).digest("hex");

/** A cpio archive in the "newc" format: directories first, every time and owner zero. */
function cpio(entries) {
	const parts = [];
	let ino = 0x100000;
	const add = (name, mode, data) => {
		const hex = (n) => n.toString(16).padStart(8, "0");
		const path = Buffer.from(`${name}\0`);
		const head = Buffer.from("070701" + [ino++, mode, 0, 0, 1, 0, data.length, 0, 0, 0, 0, path.length, 0].map(hex).join(""));
		const pad = (n) => Buffer.alloc((4 - (n % 4)) % 4);
		parts.push(head, path, pad(head.length + path.length), data, pad(data.length));
	};
	const dirs = new Set();
	for (const [path, data] of entries) {
		const names = path.replace(/^\//, "").split("/");
		for (let i = 1; i < names.length; i++) {
			const dir = names.slice(0, i).join("/");
			if (!dirs.has(dir)) dirs.add(dir), add(dir, 0o040755, Buffer.alloc(0));
		}
		add(names.join("/"), 0o100755, data);
	}
	add("TRAILER!!!", 0, Buffer.alloc(0));
	const all = Buffer.concat(parts);
	return Buffer.concat([all, Buffer.alloc((512 - (all.length % 512)) % 512)]);
}

const marks = [], consoleChunks = [];
let filesDigest = null, initrdDigest = null, mountDir = null;

function finish(why, st) {
	const mem = new Uint8Array(st.memory.buffer);
	const out = Buffer.concat(consoleChunks);
	if (arg.console) writeFileSync(arg.console, out);
	if (arg.dump) writeFileSync(arg.dump, mem);
	marks.push({ at: "end", insns: icount(real, st.memory), wallNs: Number(process.hrtime.bigint()), vns: st.vns, calls: st.calls });
	const stats = real?.stats?.() ?? {};
	const record = {
		mode: "controlled", deliver, why, fault: arg.fault, jit: !!num("jit", 0), seed: num("seed", 1), stepNs: num("step", 1000),
		node: process.version, memoryBytes: mem.length, memory: sha(mem),
		console: sha(out), consoleBytes: out.length,
		imports: st.full.digest("hex"), importSequence: st.seq.digest("hex"), importCalls: st.calls, counts: st.counts,
		icount: icount(real, st.memory), nsPerInsn: num("ns-per-insn", 0), virtualNs: st.vns, idles: st.idles,
		capturedTimers: st.capturedTimers, realTimers: virtualTimers ? 0 : st.realTimers,
		hostTimers: virtualTimers ? { virtual: timerSeq, fired: timersFired, pending: queue.filter((t) => !t.dead).length } : null,
		inputsDelivered: st.inputAt, deliveredFrom: st.delivered,
		files: files.map(([path, data]) => ({ path, bytes: data.length, sha256: sha(data) })), filesDigest, initrd: initrdDigest, mountDir,
		marks,
		kernelStats: { switches: stats.switches, idles: stats.idles, yields: stats.yields },
		jitStats: stats.jit && { compiled: stats.jit.compiled, freed: stats.jit.freed, refused: stats.jit.refused, failed: stats.jit.failed, bytes: stats.jit.bytes },
		vsockStats: stats.vsock, mountStats: stats.mounts,
	};
	real?.stop();
	writeFileSync(arg.out, JSON.stringify(record, null, 1) + "\n");
	process.exit(0);
}

// --host-timers virtual: the SDK's own timers (not the idle wait's, which host.mjs takes) run on
// the virtual clock too. Each fires at the first clock read at or after its virtual deadline, in
// order of deadline and then of creation. Installed before host.mjs, which hands it every timer
// it does not capture itself. Without it those timers run on the real clock (counted as realTimers).
const virtualTimers = arg["host-timers"] === "virtual", queue = [];
let timerSeq = 0, timersFired = 0;
if (virtualTimers) {
	const realClear = globalThis.clearTimeout;
	globalThis.setTimeout = (fn, ms, ...rest) => {
		const t = { virtual: true, fn, rest, at: host.state().vns + Math.ceil(Math.max(0, ms ?? 0) * 1e6), seq: timerSeq++, unref() { return t; }, ref() { return t; } };
		queue.push(t);
		return t;
	};
	globalThis.clearTimeout = (t) => (t && t.virtual ? void (t.dead = true) : realClear(t));
}

const host = install({
	mode: "controlled", script, fault: arg.fault, seed: num("seed", 1), stepNs: num("step", 1000), nsPerInsn: num("ns-per-insn", 0),
	epochMs: 1767225600000, endAtMs: num("end", 20000), maxCalls: num("max-calls", 200e6), log: arg.log, onEnd: finish,
});

if (virtualTimers) {
	const now = performance.now;
	let firing = false;
	performance.now = () => {
		if (!firing) {
			firing = true;
			try {
				for (;;) {
					const vns = host.state().vns;
					const due = queue.filter((t) => !t.dead && t.at <= vns).sort((a, b) => a.at - b.at || a.seq - b.seq)[0];
					if (!due) break;
					due.dead = true;
					timersFired++;
					due.fn(...due.rest);
				}
				for (let i = queue.length; i--;) if (queue[i].dead) queue.splice(i, 1);
			} finally {
				firing = false;
			}
		}
		return now();
	};
}

const sdk = await import(`${process.env.UNKERNEL}/dist/node.mjs`);
const module = await WebAssembly.compile(readFileSync(`${process.env.UNKERNEL}/dist/unkernel.wasm`));
const opts = { kernel: module, vsock: false };
if (deliver === "initrd") {
	const bundled = sdk.bundledBoot(module);
	if (!bundled.initrd || !bundled.cmdline) throw new Error("the pinned kernel has no bundled initramfs or command line");
	const extra = cpio(files);
	filesDigest = sha(extra);
	// Linux unpacks concatenated archives in order: the base system first, then ours on top of it.
	const base = Buffer.from(bundled.initrd), pad = Buffer.alloc((4 - (base.length % 4)) % 4);
	opts.initrd = new Uint8Array(Buffer.concat([base, pad, extra]));
	opts.cmdline = bundled.cmdline; // the SDK drops the bundled command line when `initrd` is given
	initrdDigest = { base: sha(base), baseBytes: base.length, total: sha(opts.initrd), totalBytes: opts.initrd.length, cmdline: bundled.cmdline };
} else if (deliver === "mount") {
	const dir = files[0][0].slice(0, files[0][0].lastIndexOf("/")) || "/";
	const fs = new sdk.MemoryFs();
	for (const [path, data] of files) {
		if (path.slice(0, path.lastIndexOf("/")) !== dir) throw new Error("--deliver mount: all files in one directory");
		fs.writeFile(path.slice(path.lastIndexOf("/") + 1), new Uint8Array(data), { mode: 0o755 });
	}
	mountDir = dir;
	opts.vsock = true;
	opts.mounts = { [dir]: { fs, readOnly: true } };
} else if (deliver !== "none") throw new Error(`--deliver ${deliver}`);

let real;
const st = host.state();
real = await sdk.createKernel({
	...opts, jit: !!num("jit", 0), cpus: 1, memoryMiB: num("mem", 128),
	log: (m) => consoleChunks.push(Buffer.from(`${m}\n`)),
	onOutput: (b) => consoleChunks.push(Buffer.from(b)),
});
// What host.mjs uses of the instance, with each delivery of input marked.
st.vm = {
	kernel: real.kernel,
	stats: () => real.stats(),
	stop: () => real.stop(),
	snapshot: (o) => real.snapshot(o),
	write(text) {
		marks.push({ at: st.inputAt - 1, insns: icount(real, st.memory), wallNs: Number(process.hrtime.bigint()), vns: st.vns, calls: st.calls });
		return real.write(text);
	},
};
real.exited.then((res) => host.end(`exit ${res.kind} ${res.code}`));
