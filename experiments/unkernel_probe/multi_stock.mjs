// What the pinned package does with two kernels in one thread and nothing controlled:
// both are created at once on the stock host, each is typed one line, and the consoles are
// checked for both answers. Prints one JSON line.
const { createKernel } = await import(`${process.env.UNKERNEL}/dist/node.mjs`);
const out = [[], []], t0 = performance.now();
const boot = (i) => createKernel({ jit: false, cpus: 1, vsock: false, memoryMiB: 128, log: (m) => out[i].push(Buffer.from(`${m}\n`)), onOutput: (b) => out[i].push(Buffer.from(b)) });
let res;
try {
	const vms = await Promise.all([boot(0), boot(1)]);
	setTimeout(() => vms.forEach((vm, i) => vm.write(`echo guest-$((${i}+40)); cat /proc/uptime\n`)), 2000);
	await new Promise((r) => setTimeout(r, 4000));
	const text = out.map((c) => Buffer.concat(c).toString("latin1"));
	res = { created: 2, answered: text.map((t, i) => t.includes(`guest-${i + 40}\r\n`) || t.includes(`guest-${i + 40}\n`)), consoleBytes: text.map((t) => t.length), wallMs: Math.round(performance.now() - t0) };
	for (const vm of vms) vm.stop();
} catch (e) {
	res = { error: String(e?.stack ?? e) };
}
console.log(JSON.stringify(res));
process.exit(0);
