// Ethernet frames for the network probes: a scripted peer (ARP, ICMP echo, UDP echo, a
// one-exchange TCP server), the frame trace, and the seeded generator. No sockets, no clock:
// everything here is a function of the frames it is given.
import { createHash } from "node:crypto";

export const sha = (b) => createHash("sha256").update(b).digest("hex");
export const macOf = (i) => Uint8Array.from([2, 0, 0, 0, 0, i]);
export const ipOf = (s) => Uint8Array.from(s.split(".").map(Number));

export function prng(seed) {
	let s = seed >>> 0;
	return () => {
		s = (s + 0x9e3779b9) >>> 0;
		let z = s;
		z = Math.imul(z ^ (z >>> 16), 0x85ebca6b);
		z = Math.imul(z ^ (z >>> 13), 0xc2b2ae35);
		return (z ^ (z >>> 16)) >>> 0;
	};
}

/** Internet checksum of buf[from..to), added to `sum`. */
function csum(buf, from, to, sum = 0) {
	for (let i = from; i < to; i += 2) sum += (buf[i] << 8) | (i + 1 < to ? buf[i + 1] : 0);
	while (sum >> 16) sum = (sum & 0xffff) + (sum >> 16);
	return sum;
}
const put16 = (b, at, v) => { b[at] = (v >> 8) & 0xff; b[at + 1] = v & 0xff; };
const put32 = (b, at, v) => { put16(b, at, v >>> 16); put16(b, at + 2, v & 0xffff); };
const get16 = (b, at) => (b[at] << 8) | b[at + 1];
const get32 = (b, at) => ((b[at] << 24) | (b[at + 1] << 16) | (b[at + 2] << 8) | b[at + 3]) >>> 0;
const same = (a, at, b) => b.every((x, i) => a[at + i] === x);

/** What a frame is, in a few words, for the readable trace. */
export function describe(f) {
	const t = get16(f, 12);
	if (t === 0x0806) return get16(f, 20) === 1 ? `arp who-has ${f.slice(38, 42).join(".")}` : `arp ${f.slice(28, 32).join(".")} is-at`;
	if (t !== 0x0800) return `ethertype ${t.toString(16)}`;
	const ihl = (f[14] & 15) * 4, p = 14 + ihl, proto = f[23];
	const ends = `${f.slice(26, 30).join(".")}>${f.slice(30, 34).join(".")}`;
	if (proto === 1) return `icmp ${f[p] === 8 ? "echo" : f[p] === 0 ? "reply" : f[p]} ${ends} seq ${get16(f, p + 6)}`;
	if (proto === 17) return `udp ${ends} ${get16(f, p)}>${get16(f, p + 2)} len ${get16(f, p + 4) - 8}`;
	if (proto === 6) {
		const fl = f[p + 13], names = ["F", "S", "R", "P", "A"].filter((_, i) => fl & (1 << i)).join("");
		return `tcp ${ends} ${get16(f, p)}>${get16(f, p + 2)} ${names} len ${get16(f, 16) - ihl - (f[p + 12] >> 4) * 4}`;
	}
	return `ip proto ${proto} ${ends}`;
}

/** The scripted peer: one address, answers ARP, ICMP echo, UDP port 7, and TCP port 7007. */
export class Peer {
	constructor({ mac = macOf(0xfe), ip = ipOf("10.9.0.1") } = {}) {
		this.mac = mac;
		this.ip = ip;
		this.tcp = new Map();
		this.id = 1;
	}

	ip4(req, proto, body) {
		const f = new Uint8Array(34 + body.length);
		f.set(req.subarray(6, 12), 0);
		f.set(this.mac, 6);
		put16(f, 12, 0x0800);
		f[14] = 0x45;
		put16(f, 16, 20 + body.length);
		put16(f, 18, this.id++);
		f[22] = 64;
		f[23] = proto;
		f.set(this.ip, 26);
		f.set(req.subarray(26, 30), 30);
		put16(f, 24, ~csum(f, 14, 34) & 0xffff);
		f.set(body, 34);
		if (proto !== 1) { // pseudo header: addresses, protocol, length
			const at = 34 + (proto === 6 ? 16 : 6);
			put16(f, at, ~csum(f, 34, f.length, csum(f, 26, 34, proto + body.length)) & 0xffff || 0xffff);
		}
		return f;
	}

	/** The frames this one is answered with, in order. */
	respond(f) {
		const type = get16(f, 12);
		if (type === 0x0806) {
			if (get16(f, 20) !== 1 || !same(f, 38, this.ip)) return [];
			const r = new Uint8Array(42);
			r.set(f.subarray(6, 12), 0);
			r.set(this.mac, 6);
			r.set([0x08, 0x06, 0, 1, 8, 0, 6, 4, 0, 2], 12);
			r.set(this.mac, 22);
			r.set(this.ip, 28);
			r.set(f.subarray(22, 32), 32);
			return [r];
		}
		if (type !== 0x0800 || !same(f, 30, this.ip) || (f[14] & 15) !== 5) return [];
		const end = 14 + get16(f, 16), proto = f[23];
		if (proto === 1 && f[34] === 8) {
			const body = f.slice(34, end);
			body[0] = 0;
			put16(body, 2, 0);
			put16(body, 2, ~csum(body, 0, body.length) & 0xffff);
			return [this.ip4(f, 1, body)];
		}
		if (proto === 17 && get16(f, 36) === 7) {
			const data = Buffer.concat([Buffer.from("echo:"), f.subarray(42, end)]), body = new Uint8Array(8 + data.length);
			put16(body, 0, 7);
			put16(body, 2, get16(f, 34));
			put16(body, 4, body.length);
			body.set(data, 8);
			return [this.ip4(f, 17, body)];
		}
		if (proto === 6 && get16(f, 36) === 7007) return this.segment(f, end);
		return [];
	}

	segment(f, end) {
		const key = `${f.slice(26, 30).join(".")}:${get16(f, 34)}`, flags = f[47], off = 34 + (f[46] >> 4) * 4;
		const seq = get32(f, 38), len = end - off;
		const out = (c, fl, data = new Uint8Array(0), opts = new Uint8Array(0)) => {
			const body = new Uint8Array(20 + opts.length + data.length);
			put16(body, 0, 7007);
			put16(body, 2, get16(f, 34));
			put32(body, 4, c.snd);
			put32(body, 8, c.rcv);
			body[12] = ((20 + opts.length) / 4) << 4;
			body[13] = fl;
			put16(body, 14, 65535);
			body.set(opts, 20);
			body.set(data, 20 + opts.length);
			c.snd = (c.snd + data.length + (fl & 3 ? 1 : 0)) >>> 0;
			return this.ip4(f, 6, body);
		};
		if (flags & 4) return this.tcp.delete(key), [];
		if (flags & 2) { // SYN: a fixed initial sequence number per connection, in order of arrival
			const c = { snd: (0x10000000 + this.tcp.size * 0x10000) >>> 0, rcv: (seq + 1) >>> 0, said: false };
			this.tcp.set(key, c);
			return [out(c, 0x12, undefined, Uint8Array.from([2, 4, 5, 0xb4]))];
		}
		const c = this.tcp.get(key);
		if (!c) return [];
		const res = [];
		if (len > 0 && seq === c.rcv) {
			c.rcv = (c.rcv + len) >>> 0;
			if (!c.said) { // one answer, then our FIN
				c.said = true;
				res.push(out(c, 0x19, Buffer.concat([Buffer.from("echo:"), f.subarray(off, end)])));
			} else res.push(out(c, 0x10));
		} else if (len > 0) res.push(out(c, 0x10));
		if (flags & 1 && (seq + len) >>> 0 === c.rcv) {
			c.rcv = (c.rcv + 1) >>> 0;
			res.push(out(c, 0x10));
		}
		return res;
	}
}

/** The ordered frame trace: who, direction, virtual time, bytes. Its digest covers all of it. */
export class Trace {
	constructor() {
		this.h = createHash("sha256");
		this.lines = [];
		this.counts = {};
	}
	add(kind, who, vns, frame, note = "") {
		const line = `${kind} ${who} ${vns} ${frame.length} ${Buffer.from(frame).toString("hex")}`;
		this.h.update(line + "\n");
		this.lines.push(`${kind} ${who} ${vns} ${frame.length} ${sha(frame).slice(0, 12)} ${describe(frame)}${note ? " " + note : ""}`);
		this.counts[kind] = (this.counts[kind] ?? 0) + 1;
	}
	digest() { return this.h.copy().digest("hex"); }
	text() { return this.lines.join("\n") + "\n"; }
}
