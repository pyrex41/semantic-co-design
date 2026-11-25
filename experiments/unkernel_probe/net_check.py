"""Rerun the unkernel network probes and compare the digests.

    nix develop .#unkernel -c python3 experiments/unkernel_probe/net_check.py [--record]

One guest against a scripted peer (net.mjs), then three guests on one virtual switch
(switch.mjs). Every boot is a fresh `node --experimental-wasm-jspi` process against the pinned
package ($UNKERNEL) with a time limit; nothing touches a host socket. --record rewrites
evidence/net_*. Without it the fresh digests are also compared with evidence/net_summary.json,
which must have been recorded from these sources and this package.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
KEYS = ("memory", "console", "imports", "importCalls", "icount")
SOURCES = ("host.mjs", "net.mjs", "netlib.mjs", "switch.mjs", "multi_stock.mjs", "net_check.py",
           "net_workload.json", "net_busy.json", "multi_workload.json", "multi_busy.json")
BOUND = ("unkernel", "kernel_wasm_sha256", "sources")
TIMEOUT = 150
failures = []
scratch = None
serial = 0


def run(script, *args, files=()):
    """One fresh process; its record, with the paths of the files it was asked to write."""
    global serial
    serial += 1
    out = scratch / f"r{serial}.json"
    extra = [x for kind in files for x in (f"--{kind}", str(scratch / f"r{serial}.{kind}"))]
    subprocess.run(["node", "--experimental-wasm-jspi", str(HERE / script), *map(str, args), *extra, "--out", str(out)],
                   check=True, timeout=TIMEOUT)
    r = json.loads(out.read_text())[0]
    r["_files"] = {kind: scratch / f"r{serial}.{kind}" for kind in files}
    return r


def flat(r):
    """What is compared: per guest the five digests and counters, and the frame trace."""
    if "guests" in r:
        d = {f"g{i}.{k}": g[k] for i, g in enumerate(r["guests"]) for k in KEYS}
    else:
        d = {k: r[k] for k in KEYS}
    d["trace"] = r["trace"]
    return d


def differing(records):
    f = [flat(r) for r in records]
    return [k for k in f[0] if any(x[k] != f[0][k] for x in f)]


def problems(records):
    """Why these boots do not count as the same controlled run; empty if they do."""
    found = [f"differ on {k}" for k in differing(records)]
    for r in records:
        for g in r.get("guests", [r]):
            if not (isinstance(g["icount"], int) and g["icount"] > 0):
                found.append("instruction count not read")
            if g["framesUndelivered"]:
                found.append("frames left undelivered")
        if r["realTimers"]:
            found.append("a timer was left to the real clock")
    return sorted(set(found))


def expect(ok, what):
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def brief(r):
    drop = ("_files", "counts", "node")
    b = {k: v for k, v in r.items() if k not in drop and v is not None}
    if "guests" in b:
        b["guests"] = [{k: v for k, v in g.items() if k != "counts"} for g in b["guests"]]
    return b


def first_difference(a, b, context=2):
    """The first line on which two text files differ, with a few lines before it."""
    x, y = Path(a).read_text().splitlines(), Path(b).read_text().splitlines()
    n = next((i for i in range(min(len(x), len(y))) if x[i] != y[i]), min(len(x), len(y)))
    lines = [f"lines: {len(x)} and {len(y)}; first difference at line {n + 1}"]
    lines += [f"  = {line}" for line in x[max(0, n - context):n]]
    lines += [f"  A {line}" for line in x[n:n + 2]] + [f"  B {line}" for line in y[n:n + 2]]
    return "\n".join(lines) + "\n"


def console(r, guest=None):
    path = r["_files"]["console"]
    return Path(f"{path}.g{guest}" if guest is not None else path).read_text(errors="replace")


def main():
    global scratch
    record = "--record" in sys.argv
    scratch = Path(tempfile.mkdtemp(prefix="unkernel-net."))
    package = Path(os.environ["UNKERNEL"])
    lock = HERE.parent.parent / "flake.lock"
    summary = {"unkernel": json.loads((package / "package.json").read_text())["version"],
               "kernel_wasm_sha256": hashlib.sha256((package / "dist" / "unkernel.wasm").read_bytes()).hexdigest(),
               "sources": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in SOURCES},
               "environment": {"system": f"{platform.machine()}-{platform.system().lower()}", "node": shutil.which("node"),
                               "nixpkgs": json.loads(lock.read_text())["nodes"]["nixpkgs"]["locked"]["rev"] if lock.exists() else None}}
    started = time.time()
    try:
        # ---- One guest, scripted peer.
        work = ("--script", HERE / "net_workload.json", "--end", 14000)
        busy = ("--script", HERE / "net_busy.json", "--end", 10000, "--ns-per-insn", 5)
        idle = [run("net.mjs", *work, files=("console", "trace", "log")) for _ in range(3)]
        expect(not problems(idle), "single: 3 fresh boots are identical (memory, console, imports, instructions, frame trace)" + "; ".join(problems(idle)))
        text = console(idle[0])
        expect(all(s in text for s in ("3 packets received", "ping=0", "echo:hello-udp", "udp=0", "echo:hello-tcp", "tcp=0")),
               "single: ARP, 3 pings, the UDP exchange and the TCP exchange all succeeded in the guest")
        expect(idle[0]["frames"] == {"tx": 13, "rx": 8}, f"single: 13 frames sent, 8 received ({idle[0]['frames']})")
        ran = [run("net.mjs", *busy, files=("console", "trace")) for _ in range(3)]
        expect(not problems(ran), "single, guest busy: 3 fresh boots are identical" + "; ".join(problems(ran)))
        expect(ran[0]["framesFrom"]["clock"] > 0 and "5 packets received" in console(ran[0]) and "tcp=0" in console(ran[0]),
               f"single, guest busy: frames reached a running guest ({ran[0]['framesFrom']}), largest lag {ran[0]['maxLagNs']} ns")
        jit = [run("net.mjs", *busy, "--jit", 1) for _ in range(2)]
        expect(not problems(jit) and all(jit[0][k] == ran[0][k] for k in ("console", "icount", "trace")),
               "single, JIT on: 2 fresh boots are identical; console, instructions and trace as with the JIT off")
        # Attack: real random delays in the host must change nothing.
        spun = [run("net.mjs", *work, "--net-fault", "spin"), run("net.mjs", *busy, "--net-fault", "spin")]
        expect(not problems([idle[0], spun[0]]) and not problems([ran[0], spun[1]]),
               "single, attack: random real delays (0..3 ms) at every send and delivery change nothing")
        # The delivery rule is needed: arrival times that depend on the real clock.
        loose = [run("net.mjs", *work, "--net-fault", "real-latency", files=("trace", "log")) for _ in range(2)]
        expect(set(differing(loose)) >= {"memory", "imports", "trace"} and set(differing([idle[0], loose[0]])) >= {"memory", "imports", "trace"},
               f"single, fault real-latency: rejected, two boots differ on {differing(loose)}")
        divergence = "single guest, --net-fault real-latency, two boots\n\nframe trace\n" + first_difference(loose[0]["_files"]["trace"], loose[1]["_files"]["trace"])
        divergence += "\nimport log\n" + first_difference(loose[0]["_files"]["log"], loose[1]["_files"]["log"])
        host_faults = {}
        for fault, rejected in (("yield", False), ("math-random", False), ("idle-real", True), ("entropy", True)):
            r = run("net.mjs", *work, "--host-fault", fault)
            # As in check.py: the import log alone (what sched_yield returned) does not reject a boot.
            found = [x for x in problems([idle[0], r]) if x != "differ on imports"]
            host_faults[fault] = {"problems": found, "differ": differing([idle[0], r])}
            expect(bool(found) == rejected, f"single, host fault {fault}: {'rejected' if found else 'not rejected'} ({'; '.join(found) or 'differs on ' + str(differing([idle[0], r]))})")
        # Teeth.
        teeth = {}
        for name, args, must in (("one bit of the peer's address in the ARP reply", ("--flip", 0, "--flip-at", 27), {"memory", "console", "imports", "trace"}),
                                 ("one bit of the UDP answer", ("--flip", 4), {"console", "imports", "trace"}),
                                 ("one bit of an ICMP reply's padding", ("--flip", 2), {"imports", "trace"}),
                                 ("one frame 1000 ns later", ("--late", 2), {"memory", "console", "imports", "trace"})):
            d = differing([idle[0], run("net.mjs", *work, *args)])
            teeth[name] = d
            expect(set(d) >= must, f"single, teeth: {name} changes {d}")

        # ---- Several guests.
        stock = json.loads(subprocess.run(["node", "--experimental-wasm-jspi", str(HERE / "multi_stock.mjs")], check=True, timeout=TIMEOUT,
                                          capture_output=True, text=True).stdout.strip().splitlines()[-1])
        expect(stock.get("answered") == [True, True], f"two stock kernels in one thread both boot and answer ({stock})")
        mwork = ("--script", HERE / "multi_workload.json", "--end", 12000)
        mbusy = ("--script", HERE / "multi_busy.json", "--end", 8000, "--ns-per-insn", 5)
        midle = [run("switch.mjs", *mwork, files=("console", "trace")) for _ in range(3)]
        expect(not problems(midle), "three guests: 3 fresh runs are identical for every guest and the global frame trace" + "; ".join(problems(midle)))
        a, b, c = (console(midle[0], i) for i in range(3))
        expect("3 packets received" in a and "reply-from-b" in a and "tcp=0" in a and "hello-from-a" in b and c.count("2 packets received") == 2,
               "three guests: A pinged B, A and B exchanged a message over TCP, C pinged A and B")
        mran = [run("switch.mjs", *mbusy, files=("console", "trace")) for _ in range(3)]
        expect(not problems(mran), "three guests, one busy: 3 fresh runs are identical" + "; ".join(problems(mran)))
        g0 = mran[0]["guests"][0]
        expect(g0["parks"] > 0 and g0["framesFrom"]["clock"] > 0 and "5 packets received" in console(mran[0], 1) and "reply-from-c" in console(mran[0], 0),
               f"three guests, one busy: the busy guest was parked {g0['parks']} times and got frames while running; largest lag {max(g['maxLagNs'] for g in mran[0]['guests'])} ns")
        wide = run("switch.mjs", *mbusy, "--latency", 25000000)
        mjit = [run("switch.mjs", *mbusy, "--jit", 1) for _ in range(2)]
        expect(not problems(mjit) and mjit[0]["trace"] == mran[0]["trace"] and all(x["console"] == y["console"] and x["icount"] == y["icount"] for x, y in zip(mjit[0]["guests"], mran[0]["guests"])),
               "three guests, JIT on: 2 fresh runs are identical; consoles, instructions and trace as with the JIT off")
        mspun = [run("switch.mjs", *mwork, "--net-fault", "spin"), run("switch.mjs", *mbusy, "--net-fault", "spin")]
        expect(not problems([midle[0], mspun[0]]) and not problems([mran[0], mspun[1]]),
               "three guests, attack: random real delays at every send, delivery and switch of guest change nothing")
        mloose = [run("switch.mjs", *mwork, "--net-fault", "real-latency", files=("trace",)) for _ in range(2)]
        expect("trace" in differing(mloose) and "g0.memory" in differing(mloose), f"three guests, fault real-latency: rejected, two runs differ on {len(differing(mloose))} of 16 values")
        divergence += "\nthree guests, --net-fault real-latency, two runs\n\nframe trace\n" + first_difference(mloose[0]["_files"]["trace"], mloose[1]["_files"]["trace"])
        faulty = ("--drop", 0.1, "--dup", 0.1, "--jitter", 300000, "--partition", "4500:5600")
        seeded = [run("switch.mjs", *mwork, "--fault-seed", s, *faulty, files=("console", "trace")) for s in (7, 7, 8)]
        f = seeded[0]["frames"]
        expect(not problems(seeded[:2]) and all(f.get(k, 0) > 0 for k in ("drop", "dup", "cut")),
               f"three guests, seeded switch faults: the same seed gives the same digests and trace ({f})")
        expect("trace" in differing([seeded[0], seeded[2]]), f"three guests, seeded switch faults: another seed gives another trace ({seeded[2]['frames']})")
        expect("reply-from-b" in console(seeded[0], 0) and "hello-from-a" in console(seeded[0], 1),
               "three guests, seeded switch faults: the TCP exchange still completes (the guests retransmit)")
        for name, args in (("one bit of one frame", ("--flip", 40)), ("one frame 1000 ns later", ("--late", 40))):
            d = differing([midle[0], run("switch.mjs", *mwork, *args)])
            teeth["three guests: " + name] = d
            expect("trace" in d and any(k.endswith(".memory") for k in d), f"three guests, teeth: {name} changes {d}")

        summary.update({
            "single": {"idle": brief(idle[0]), "busy": brief(ran[0]), "jit": brief(jit[0])},
            "multi": {"idle": brief(midle[0]), "busy": brief(mran[0]), "jit": brief(mjit[0]), "latency25ms": brief(wide),
                      "faults_seed7": brief(seeded[0]), "faults_seed8": brief(seeded[2])},
            "teeth": teeth,
            # Not compared with the record: wall times, and runs that are meant to depend on the real clock.
            "unstable": {"host_faults": host_faults, "two_stock_kernels_one_thread": stock, "real_latency_single": [brief(r) for r in loose], "real_latency_multi": [brief(r) for r in mloose],
                         "wallMs": {"single_idle": [r["wallMs"] for r in idle], "single_busy": [r["wallMs"] for r in ran], "single_jit": [r["wallMs"] for r in jit],
                                    "multi_idle": [r["wallMs"] for r in midle], "multi_busy": [r["wallMs"] for r in mran], "multi_jit": [r["wallMs"] for r in mjit]}},
        })

        def stable(s):
            """The part of a summary that must be the same in every run: no wall times."""
            def strip(o):
                if isinstance(o, dict):
                    return {k: strip(v) for k, v in o.items() if not k.startswith("wallMs")}
                return [strip(v) for v in o] if isinstance(o, list) else o
            return strip({k: v for k, v in s.items() if k not in ("unstable", "environment")})

        target = EVIDENCE / "net_summary.json"
        if record:
            target.write_text(json.dumps(summary, indent=1) + "\n")
            (EVIDENCE / "net_divergence.txt").write_text(divergence)
            for name, src in (("net_single.trace", idle[0]["_files"]["trace"]), ("net_single_busy.trace", ran[0]["_files"]["trace"]),
                              ("net_multi.trace", midle[0]["_files"]["trace"]), ("net_multi_busy.trace", mran[0]["_files"]["trace"]),
                              ("net_multi_faults_seed7.trace", seeded[0]["_files"]["trace"]), ("net_single.console", idle[0]["_files"]["console"])):
                shutil.copyfile(src, EVIDENCE / name)
            for i in range(3):
                shutil.copyfile(f"{midle[0]['_files']['console']}.g{i}", EVIDENCE / f"net_multi.g{i}.console")
            print(f"recorded {target}")
        else:
            recorded = json.loads(target.read_text())
            found = [f"evidence was recorded from another {k}" for k in BOUND if summary[k] != recorded.get(k)]
            expect(not found, "evidence/net_summary.json is from these sources and this package" + "".join(f"; {x}" for x in found))
            if summary["environment"] == recorded.get("environment"):
                now, then = stable(summary), stable(recorded)
                bad = [k for k in now if now[k] != then.get(k)]
                expect(not bad, "fresh digests are the recorded ones" + "".join(f"; {k} differs" for k in bad))
            else:
                print(f"skip  evidence digests not compared: recorded on {recorded.get('environment')}, this is {summary['environment']}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"{serial + 1} processes, {time.time() - started:.0f} s")
    if failures:
        sys.exit(f"{len(failures)} comparison(s) failed")
    print("all comparisons hold")


if __name__ == "__main__":
    main()
