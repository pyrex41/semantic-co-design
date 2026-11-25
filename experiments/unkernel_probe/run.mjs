// Boots of the pinned unkernel under the stock or the controlled host (host.mjs).
// Writes a JSON list, one record of digests and counters per boot, to --out and exits.
//
//   node --experimental-wasm-jspi run.mjs --mode controlled --script workload.json --end 22000 --out a.json
//
// A script is [[ms, "console input"], ...]: virtual ms under the controlled host, wall ms
// under the stock one. --repeat N boots N times in this process. --dump writes the final
// linear memory, --pages its per-page digests, --console the console bytes, --log every
// import call (each of the last boot). --snap-at/--snap-out save a snapshot instead of
// running to --end; --restore resumes one. --fault removes one control (see host.mjs).
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { serialize, deserialize } from "node:v8";
import { icount, install } from "./host.mjs";

// Every option takes one value: --key value, pairwise, so a value may hold anything.
const argv = process.argv.slice(2), arg = {};
for (let i = 0; i < argv.length; i += 2) {
	if (!argv[i].startsWith("--") || i + 1 >= argv.length) throw new Error(`expected --key value at "${argv[i]}"`);
	arg[argv[i].slice(2)] = argv[i + 1];
}
const num = (k, d) => (arg[k] === undefined ? d : Number(arg[k]));
const mode = arg.mode ?? "controlled";
const script = arg.script ? JSON.parse(readFileSync(arg.script, "utf8")) : [];
const sha = (b) => createHash("sha256").update(b).digest("hex");
const PAGE = 4096;

const records = [];
let consoleChunks = [], snapshotDigest, left = num("repeat", 1);

function finish(why, st) {
	const mem = new Uint8Array(st.memory.buffer);
	const pages = Buffer.alloc((mem.length / PAGE) * 8);
	for (let p = 0; p * PAGE < mem.length; p++)
		createHash("sha256").update(mem.subarray(p * PAGE, (p + 1) * PAGE)).digest().copy(pages, p * 8, 0, 8);
	if (arg.pages) writeFileSync(arg.pages, pages);
	if (arg.dump) writeFileSync(arg.dump, mem);
	const out = Buffer.concat(consoleChunks);
	if (arg.console) writeFileSync(arg.console, out);
	const stats = st.vm?.stats?.() ?? {};
	records.push({
		mode, why, fault: arg.fault, jit: !!num("jit", 0), seed: num("seed", 1), stepNs: num("step", 1000), node: process.version,
		memoryBytes: mem.length, memory: sha(mem), pages: sha(pages),
		console: sha(out), consoleBytes: out.length,
		imports: st.full.digest("hex"), importSequence: st.seq.digest("hex"), importCalls: st.calls,
		counts: st.counts, checkpoints: st.checkpoints.length, checkpointChain: sha(st.checkpoints.join("")),
		icount: icount(st.vm, st.memory), nsPerInsn: num("ns-per-insn", 0),
		virtualNs: mode === "controlled" ? st.vns : null, idles: st.idles,
		capturedTimers: st.capturedTimers, realTimers: st.realTimers,
		inputsDelivered: st.inputAt, deliveredFrom: st.delivered, snapshotFrom: st.snapFrom, snapshot: snapshotDigest,
		kernelStats: { switches: stats.switches, idles: stats.idles, yields: stats.yields },
		jitStats: stats.jit && { compiled: stats.jit.compiled, freed: stats.jit.freed, refused: stats.jit.refused, failed: stats.jit.failed, bytes: stats.jit.bytes },
	});
	st.vm?.stop();
	if (--left > 0) return void host.realSetTimeout(boot, 50);
	writeFileSync(arg.out, JSON.stringify(records, null, 1) + "\n");
	process.exit(0);
}

const host = install({
	mode, script, fault: arg.fault, seed: num("seed", 1), stepNs: num("step", 1000), nsPerInsn: num("ns-per-insn", 0),
	epochMs: 1767225600000 + num("epoch-offset", 0), // 2026-01-01T00:00:00Z, plus the time a restored machine had run
	endAtMs: mode === "controlled" && arg["snap-at"] === undefined ? num("end", 22000) : undefined,
	snapAtMs: arg["snap-at"] === undefined ? undefined : num("snap-at"),
	maxCalls: num("max-calls", 200e6), log: left === 1 ? arg.log : undefined,
	onEnd: finish,
	onSnapshot(snap) {
		const bytes = serialize(snap);
		writeFileSync(arg["snap-out"], bytes);
		snapshotDigest = { memory: sha(snap.memory), monotonicMs: snap.monotonicMs, freeBytes: snap.freeBytes ?? 0, file: sha(bytes), fileBytes: bytes.length };
		host.end("snapshot");
	},
});

const { createKernel } = await import(`${process.env.UNKERNEL}/dist/node.mjs`);
const restore = arg.restore ? deserialize(readFileSync(arg.restore)) : undefined;

async function boot() {
	if (records.length) host.begin(left === 1 ? arg.log : undefined);
	consoleChunks = [];
	const chunks = consoleChunks, st = host.state();
	const vm = st.vm = await createKernel({
		jit: !!num("jit", 0), cpus: 1, vsock: false, memoryMiB: num("mem", 128),
		restore,
		log: (m) => chunks.push(Buffer.from(`${m}\n`)),
		onOutput: (b) => chunks.push(Buffer.from(b)),
	});
	if (num("ns-per-insn", 0) && icount(vm, st.memory) === null) throw new Error("--ns-per-insn: the instruction counter cannot be read");
	// A snapshot with stop ends the kernel first; that run ends in onSnapshot.
	vm.exited.then((res) => host.state() === st && !st.snapAsked && host.end(`exit ${res.kind} ${res.code}`));
	if (mode === "stock") {
		// The stock host runs on the real clock, so the script does too.
		for (const [ms, text] of script) host.realSetTimeout(() => { st.inputAt++; vm.write(text); }, ms);
		host.realSetTimeout(() => host.state() === st && host.end("end time"), num("end", 22000));
	}
}
await boot();
