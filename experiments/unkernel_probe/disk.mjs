// One boot of the pinned unkernel with a kwblk disk (/dev/wda) under the controlled host
// (host.mjs), and a record of digests: the guest's, as run.mjs gives them, and the disk's.
//
//   node --experimental-wasm-jspi disk.mjs --script disk_workload.json --end 40000 --out a.json
//
// The disk is a BlockDevice of our own, handed to the SDK's public createKernel({ disks }):
// an in-memory image, a log of every request, and a second image that holds only what the
// guest has flushed (what a power cut would leave). --completion says when a request completes:
//
//   sync       inside the submit import (the request never becomes asynchronous)
//   microtask  from an already resolved promise: the SDK's asynchronous path (IRQ 3), with the
//              completion landing at the guest's next suspension
//   virtual    from a promise resolved when the virtual clock passes submit + --latency-us
//              (plus a seeded 0..--latency-spread-us): the deterministic asynchronous device
//   real       from a real timer of random length up to --real-max-ms: the seeded fault, an
//              asynchronous device left to the host's event loop. An idle guest that is owed
//              such a completion waits for it with the virtual clock standing still
//              (--real-starve 1: the idle wait jumps the clock as if nothing were owed)
//
// --host-delay-ms N burns a random 0..N ms of real time inside every request (an attack on
// the result: host timing may not matter). --backend cow uses the SDK's own in-memory CowDisk
// instead (its completions are its own). --tamper-write K flips one bit of the K-th write as
// stored; --flip-image OFF flips one byte of the initial image. --image/--gen start from a
// saved image; --image-out/--durable-out save the final one and the flushed one; --crash-at-op N
// ends the run at the N-th disk request (a power cut: --durable-out is what survives);
// --snap-at/--snap-out/--restore as in run.mjs (the image is saved at the seal).
import { createHash, randomInt } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { serialize, deserialize } from "node:v8";
import { icount, install } from "./host.mjs";

const argv = process.argv.slice(2), arg = {};
for (let i = 0; i < argv.length; i += 2) {
	if (!argv[i].startsWith("--") || i + 1 >= argv.length) throw new Error(`expected --key value at "${argv[i]}"`);
	arg[argv[i].slice(2)] = argv[i + 1];
}
const num = (k, d) => (arg[k] === undefined ? d : Number(arg[k]));
const mode = arg.mode ?? "controlled", completion = arg.completion ?? "sync", backend = arg.backend ?? "mem";
const script = arg.script ? JSON.parse(readFileSync(arg.script, "utf8")) : [];
const sha = (b) => createHash("sha256").update(b).digest("hex");
const OPS = ["read", "write", "flush", "discard", "zeroes"];
const F_FLUSH = 2, F_DISCARD = 4, F_WRITE_ZEROES = 8;

let host; // set below; the disk reads the virtual clock through it
const vns = () => (mode === "controlled" ? host.state().vns : Math.round(host.realNow() * 1e6));
const burn = (maxMs) => {
	if (!maxMs) return;
	const until = host.realNow() + randomInt(0, maxMs * 1000 + 1) / 1000; // real entropy, on purpose
	while (host.realNow() < until);
};

/** splitmix32, as in host.mjs: the seeded part of the virtual latency. */
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

const BLOCK = 4096;
/** A sparse image: 4 KiB blocks by index; an absent block reads as zeros. */
class Sparse {
	constructor(size, blocks) {
		this.size = size;
		this.blocks = new Map(blocks);
	}
	copy() {
		return new Sparse(this.size, [...this.blocks].map(([b, d]) => [b, d.slice()]));
	}
	get(off, out) {
		for (let o = 0; o < out.length; ) {
			const b = Math.floor((off + o) / BLOCK), at = (off + o) % BLOCK, n = Math.min(BLOCK - at, out.length - o);
			const blk = this.blocks.get(b);
			if (blk) out.set(blk.subarray(at, at + n), o);
			else out.fill(0, o, o + n);
			o += n;
		}
	}
	put(off, data) {
		for (let o = 0; o < data.length; ) {
			const b = Math.floor((off + o) / BLOCK), at = (off + o) % BLOCK, n = Math.min(BLOCK - at, data.length - o);
			let blk = this.blocks.get(b);
			if (!blk) this.blocks.set(b, (blk = new Uint8Array(BLOCK)));
			blk.set(data.subarray(o, o + n), at);
			o += n;
		}
	}
	/** The canonical bytes of the content (the SDK's kwsparse: size, the non-zero blocks in order). */
	encode() {
		return encodeKwsparse({ size: this.size, blockSize: BLOCK, blocks: this.blocks });
	}
}

class MemDisk {
	constructor(image, gen) {
		this.size = image.size;
		this.flags = F_FLUSH | (num("discard", 0) ? F_DISCARD | F_WRITE_ZEROES : 0);
		this.vol = image; // what the guest reads
		this.durable = image.copy(); // what it had flushed
		this.unflushed = []; // [off, bytes] written since the last flush
		this.log = createHash("sha256");
		this.lines = arg["disk-log"] ? [] : null;
		this.counts = { read: 0, write: 0, flush: 0, discard: 0, zeroes: 0 };
		this.bytes = { read: 0, write: 0 };
		this.seq = 0;
		this.writes = 0;
		this.pending = []; // virtual completions: { due, seq, resolve }
		this.realPending = 0;
		this.completed = 0;
		this.latency = prng(num("latency-seed", 7));
		this.gen = gen ?? 0;
		this.sealed = true;
		this.sealedImage = null;
		this.droppedAtEnd = 0;
	}
	#note(op, off, len, data) {
		const line = `${this.seq++} ${op} ${off} ${len} ${data ? sha(data).slice(0, 16) : "-"} @${vns()}`;
		this.log.update(line.slice(0, line.lastIndexOf(" @"))); // the request; its time is in the import log
		this.lines?.push(line);
		this.counts[op]++;
		// A power cut at a request: the run ends with this request applied to the image and,
		// unless it is a flush, not to the flushed one.
		if (this.seq === num("crash-at-op", 0)) host.end(`power cut at disk request ${this.seq}`);
	}
	#complete() {
		burn(num("host-delay-ms", 0));
		if (completion === "sync") return undefined;
		if (completion === "microtask") return this.#owed(Promise.resolve());
		if (completion === "virtual") {
			const spread = num("latency-spread-us", 0);
			const due = vns() + (num("latency-us", 200) + (spread ? this.latency() % (spread + 1) : 0)) * 1000;
			return new Promise((resolve) => {
				// Kept in order of (due, submission): the order completions are delivered in.
				let at = this.pending.length;
				while (at > 0 && this.pending[at - 1].due > due) at--;
				this.pending.splice(at, 0, { due, resolve });
			});
		}
		if (completion === "real") {
			return this.#owed(new Promise((resolve) => host.realSetTimeout(resolve, randomInt(0, num("real-max-ms", 20) + 1))));
		}
		throw new Error(`--completion ${completion}`);
	}
	/** A completion that only a turn of the host's event loop (or its microtasks) delivers. */
	#owed(promise) {
		this.realPending++;
		promise.then(() => this.realPending--);
		return promise;
	}
	#dirty() {
		if (this.sealed) this.gen++, (this.sealed = false);
	}
	read(off, views) {
		let o = off;
		for (const v of views) {
			this.vol.get(o, v);
			o += v.length;
		}
		this.bytes.read += o - off;
		this.#note("read", off, o - off, null);
		return this.#complete();
	}
	write(off, views) {
		this.#dirty();
		const data = Buffer.concat(views);
		if (++this.writes === num("tamper-write", 0)) data[0] ^= 1;
		this.vol.put(off, data);
		this.unflushed.push([off, data]);
		this.bytes.write += data.length;
		this.#note("write", off, data.length, data);
		return this.#complete();
	}
	flush() {
		for (const [off, data] of this.unflushed) this.durable.put(off, data);
		this.unflushed = [];
		this.#note("flush", 0, 0, null);
		return this.#complete();
	}
	discard(off, len) {
		return this.writeZeroes(off, len, "discard");
	}
	writeZeroes(off, len, op = "zeroes") {
		this.#dirty();
		const zeros = new Uint8Array(len);
		this.vol.put(off, zeros);
		this.unflushed.push([off, zeros]);
		this.#note(op, off, len, null);
		return this.#complete();
	}
	// host.mjs's device hooks: completions in virtual time.
	dueNs() {
		return this.pending.length ? this.pending[0].due : Infinity;
	}
	pump(now) {
		while (this.pending.length && this.pending[0].due <= now) this.pending.shift().resolve(), this.completed++;
	}
	waiting() {
		return this.realPending > 0 && !num("real-starve", 0);
	}
	// The SDK's snapshot rules: a disk must say which disk it is and at which write generation,
	// and seal (make durable) at the consistent point. The image itself is ours to keep.
	snapshotState() {
		return { name: "probe memdisk", identity: "probe-memdisk", gen: this.gen };
	}
	snapshotSeal() {
		for (const [off, data] of this.unflushed) this.durable.put(off, data);
		this.unflushed = [];
		this.sealed = true;
		this.sealedImage = this.vol.encode();
		return { state: this.snapshotState(), done: Promise.resolve() };
	}
}

const records = [];
let consoleChunks = [], snapshotDigest, disk, sdkDisk;

function finish(why, st) {
	const mem = new Uint8Array(st.memory.buffer);
	const out = Buffer.concat(consoleChunks);
	if (arg.console) writeFileSync(arg.console, out);
	if (arg.dump) writeFileSync(arg.dump, mem);
	const stats = st.vm?.stats?.() ?? {}, block = st.vm?.kernel?.block;
	// The image as canonical bytes. The SDK's CowDisk keeps its blocks in `mem` (it has no store here).
	const image = disk ? disk.vol.encode() : new Sparse(sdkDisk.size, [...baseBlocks, ...[...sdkDisk.mem].map(([b, d]) => [b, d.length ? d : new Uint8Array(BLOCK)])]).encode();
	const durable = disk?.durable.encode();
	if (arg["image-out"]) writeFileSync(arg["image-out"], image);
	if (arg["durable-out"] && disk) writeFileSync(arg["durable-out"], durable);
	if (arg["disk-log"] && disk) writeFileSync(arg["disk-log"], disk.lines.join("\n") + "\n");
	records.push({
		mode, why, completion, backend, fault: arg.fault, jit: !!num("jit", 0), seed: num("seed", 1), stepNs: num("step", 1000), node: process.version,
		memoryBytes: mem.length, memory: sha(mem),
		console: sha(out), consoleBytes: out.length,
		imports: st.full.digest("hex"), importSequence: st.seq.digest("hex"), importCalls: st.calls, counts: st.counts,
		icount: icount(st.vm, st.memory), nsPerInsn: num("ns-per-insn", 0),
		virtualNs: mode === "controlled" ? st.vns : null, idles: st.idles,
		capturedTimers: st.capturedTimers, realTimers: st.realTimers,
		inputsDelivered: st.inputAt, deliveredFrom: st.delivered, snapshotFrom: st.snapFrom, snapshot: snapshotDigest,
		disk: sha(image), diskImageBytes: image.length,
		diskBlocks: decodeKwsparse(image).indices.length, diskSizeBytes: (disk ?? sdkDisk).size,
		diskDurable: disk ? sha(durable) : null,
		diskLog: disk ? disk.log.digest("hex") : null,
		diskOps: disk?.counts, diskIoBytes: disk?.bytes, diskGen: disk?.gen,
		unflushedWrites: disk?.unflushed.length, unflushedBytes: disk?.unflushed.reduce((n, [, d]) => n + d.length, 0),
		pendingVirtual: disk?.pending.length, pendingReal: disk?.realPending, completedVirtual: disk?.completed,
		blockStats: block && { ...block.stats, inflight: block.inflight, unreaped: block.done.length },
		hostDelayMs: num("host-delay-ms", 0), latencyUs: completion === "virtual" ? [num("latency-us", 200), num("latency-spread-us", 0)] : undefined,
		kernelStats: { switches: stats.switches, idles: stats.idles, yields: stats.yields },
		jitStats: stats.jit && { compiled: stats.jit.compiled, freed: stats.jit.freed, refused: stats.jit.refused, failed: stats.jit.failed, bytes: stats.jit.bytes },
	});
	st.vm?.stop();
	writeFileSync(arg.out, JSON.stringify(records, null, 1) + "\n");
	process.exit(0);
}

// The initial image: a saved one (--image, kwsparse), an empty one (--template none, --disk-mib),
// or by default the ext4 file system bundled in the pinned kernel module (section
// unkernel.datadisk, label kwdata, 4 GiB sparse): the base system has no mkfs for a file system
// its kernel can mount, and this one needs no download.
const { createKernel, CowDisk, SparseBase, bundledBoot, readKwsparse, decodeKwsparse, encodeKwsparse } = await import(`${process.env.UNKERNEL}/dist/node.mjs`);
let start;
if (arg.image) start = decodeKwsparse(readFileSync(arg.image));
else if (arg.template === "none") start = { size: num("disk-mib", 16) << 20, map: () => new Map() };
else start = await readKwsparse(bundledBoot(new WebAssembly.Module(readFileSync(`${process.env.UNKERNEL}/dist/unkernel.wasm`))).datadisk);
const baseBlocks = [...start.map()].map(([b, d]) => [b, d.slice()]);
if (arg["flip-image"] !== undefined) {
	// One byte of the initial image, in a block of its own choosing (teeth).
	const off = num("flip-image"), b = Math.floor(off / BLOCK);
	let blk = baseBlocks.find(([i]) => i === b)?.[1];
	if (!blk) baseBlocks.push([b, (blk = new Uint8Array(BLOCK))]);
	blk[off % BLOCK] ^= 1;
}
if (backend === "mem") disk = new MemDisk(new Sparse(start.size, baseBlocks), arg.gen === undefined ? undefined : num("gen"));
else if (backend === "cow") sdkDisk = new CowDisk(new SparseBase(start.size, new Map(baseBlocks.map(([b, d]) => [b, d.slice()]))));
else throw new Error(`--backend ${backend}`);

host = install({
	mode, script, fault: arg.fault, seed: num("seed", 1), stepNs: num("step", 1000), nsPerInsn: num("ns-per-insn", 0),
	epochMs: 1767225600000 + num("epoch-offset", 0),
	endAtMs: mode === "controlled" && arg["snap-at"] === undefined ? num("end", 40000) : undefined,
	snapAtMs: arg["snap-at"] === undefined ? undefined : num("snap-at"),
	maxCalls: num("max-calls", 200e6), log: arg.log,
	devices: disk && mode === "controlled" ? [disk] : [],
	onEnd: (why, st) => void finish(why, st),
	onSnapshot(snap) {
		const bytes = serialize(snap);
		writeFileSync(arg["snap-out"], bytes);
		if (arg["image-out"] && disk) writeFileSync(arg["image-out"], disk.sealedImage), delete arg["image-out"];
		snapshotDigest = { memory: sha(snap.memory), monotonicMs: snap.monotonicMs, freeBytes: snap.freeBytes ?? 0, file: sha(bytes), fileBytes: bytes.length,
			disks: snap.disks, sealedDisk: disk ? sha(disk.sealedImage) : null };
		host.end("snapshot");
	},
});

// A wall-clock bound on the whole process: a run that hangs is a result, not a wait.
host.realSetTimeout(() => {
	const st = host.state();
	console.error(`disk.mjs: wall-clock limit; virtual ${st.vns} ns, ${st.calls} import calls, inputs ${st.inputAt}/${script.length}`);
	host.end("wall-clock limit");
}, num("wall-s", 150) * 1000);

const st = host.state(), chunks = consoleChunks;
const vm = st.vm = await createKernel({
	jit: !!num("jit", 0), cpus: 1, vsock: false, memoryMiB: num("mem", 128),
	disks: [disk ?? sdkDisk],
	restore: arg.restore ? deserialize(readFileSync(arg.restore)) : undefined,
	log: (m) => chunks.push(Buffer.from(`${m}\n`)),
	onOutput: (b) => chunks.push(Buffer.from(b)),
});
vm.exited.then((res) => !st.snapAsked && host.end(`exit ${res.kind} ${res.code}`));
if (mode === "stock") {
	for (const [ms, text] of script) host.realSetTimeout(() => { st.inputAt++; vm.write(text); }, ms);
	host.realSetTimeout(() => host.end("end time"), num("end", 40000));
}
