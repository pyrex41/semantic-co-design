// One guest with the kwnet device attached to a scripted peer (netlib.mjs), under the
// controlled host (host.mjs). Writes one record of digests, counters and the frame trace.
//
//   node --experimental-wasm-jspi net.mjs --script net_workload.json --end 14000 --out a.json
//
// The device is a raw-frame NetDevice of our own, not the SDK's NetStack. A frame the guest
// sends is answered by the peer at once, and each answer is given a virtual arrival time:
// the virtual time of the send plus --latency ns. It is handed to the guest (Kernel.receiveFrame)
// by host.mjs's pump, in the first import at which the virtual clock has reached that time:
// a clock read if the guest is running, the end of an idle wait if it is not. Nothing is
// delivered from the event loop.
//
// --net-fault removes or attacks that rule:
//   real-latency   the arrival time also gets 0..1 ms taken from the real clock
//   spin           a random real delay (0..3 ms busy wait) in every send and every delivery;
//                  virtual times are untouched, so this must change nothing
// --flip N flips one bit in the N-th frame for the guest (its last byte, or the byte at
// --flip-at); --late N delivers it --late-ns later.
// --host-fault is host.mjs's --fault.
import { createHash, randomInt } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { icount, install } from "./host.mjs";
import { Peer, Trace, macOf, sha } from "./netlib.mjs";

const argv = process.argv.slice(2), arg = {};
for (let i = 0; i < argv.length; i += 2) {
	if (!argv[i].startsWith("--") || i + 1 >= argv.length) throw new Error(`expected --key value at "${argv[i]}"`);
	arg[argv[i].slice(2)] = argv[i + 1];
}
const num = (k, d) => (arg[k] === undefined ? d : Number(arg[k]));
const script = JSON.parse(readFileSync(arg.script, "utf8"));
const realNow = performance.now.bind(performance), started = realNow();
const latency = num("latency", 200000), netFault = arg["net-fault"], flip = num("flip", -1), late = num("late", -1);
const spin = () => { for (const until = realNow() + randomInt(3000) / 1000; realNow() < until;); };

const peer = new Peer(), trace = new Trace(), queue = [];
const from = { clock: 0, jump: 0 };
let chunks = [], st, kernel, made = 0, maxLag = 0;

const device = {
	mac: macOf(1),
	attach(k) { kernel = k; },
	send(frame) {
		if (netFault === "spin") spin();
		trace.add("tx", "g0", st.vns, frame);
		for (const r of peer.respond(frame)) {
			const n = made++;
			let due = st.vns + latency;
			if (netFault === "real-latency") due += Math.floor(realNow() * 1e6) % 1000000;
			if (n === late) due += num("late-ns", 1000);
			if (n === flip) r[num("flip-at", r.length - 1)] ^= 1;
			queue.push({ due, frame: r });
			queue.sort((a, b) => a.due - b.due); // stable: equal times keep the order of sending
		}
	},
	nextNs: () => (queue.length ? queue[0].due : Infinity),
	pump(st, where) {
		while (queue.length && queue[0].due <= st.vns) {
			const { due, frame } = queue.shift();
			if (netFault === "spin") spin();
			trace.add("rx", "g0", st.vns, frame, `due ${due} at ${where}`);
			from[where]++;
			maxLag = Math.max(maxLag, st.vns - due);
			kernel.receiveFrame(frame);
		}
	},
};

const host = install({
	mode: "controlled", script, fault: arg["host-fault"], seed: num("seed", 1), stepNs: num("step", 1000), nsPerInsn: num("ns-per-insn", 0),
	epochMs: 1767225600000, endAtMs: num("end", 14000), maxCalls: num("max-calls", 200e6), log: arg.log, device,
	onEnd(why, st) {
		const mem = new Uint8Array(st.memory.buffer), out = Buffer.concat(chunks);
		if (arg.console) writeFileSync(arg.console, out);
		if (arg.trace) writeFileSync(arg.trace, trace.text());
		const wallMs = realNow() - started;
		writeFileSync(arg.out, JSON.stringify([{
			why, hostFault: arg["host-fault"], netFault, jit: !!num("jit", 0), seed: num("seed", 1), stepNs: num("step", 1000), nsPerInsn: num("ns-per-insn", 0),
			latencyNs: latency, flip, late, node: process.version,
			memoryBytes: mem.length, memory: sha(mem), console: sha(out), consoleBytes: out.length,
			imports: st.full.digest("hex"), importCalls: st.calls, counts: st.counts, icount: icount(st.vm, st.memory),
			virtualNs: st.vns, idles: st.idles, capturedTimers: st.capturedTimers, realTimers: st.realTimers,
			inputsDelivered: st.inputAt, deliveredFrom: st.delivered,
			trace: trace.digest(), frames: trace.counts, framesFrom: from, framesUndelivered: queue.length, maxLagNs: maxLag,
			wallMs: Math.round(wallMs), wallMsPerVirtualS: Math.round(wallMs / (st.vns / 1e9)),
		}], null, 1) + "\n");
		st.vm?.stop();
		process.exit(0);
	},
});
st = host.state();

const { createKernel } = await import(`${process.env.UNKERNEL}/dist/node.mjs`);
const vm = st.vm = await createKernel({
	jit: !!num("jit", 0), cpus: 1, vsock: false, memoryMiB: 128, net: device,
	log: (m) => chunks.push(Buffer.from(`${m}\n`)),
	onOutput: (b) => chunks.push(Buffer.from(b)),
});
vm.exited.then((res) => host.end(`exit ${res.kind} ${res.code}`));
