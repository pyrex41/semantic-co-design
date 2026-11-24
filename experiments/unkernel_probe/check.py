"""Rerun the unkernel determinism probe and compare the digests.

    nix develop .#unkernel -c python3 experiments/unkernel_probe/check.py [--no-stock] [--record]

Every boot is `node --experimental-wasm-jspi run.mjs ...` against the pinned package ($UNKERNEL).
--no-stock skips the two wall-clock boots of the stock host (22 s of waiting, and the only part
whose result is expected to differ). --record rewrites evidence/.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
KEYS = ("memory", "console", "imports", "importCalls", "icount")
PAGE = 4096
failures = []


def node(*args):
    return ["node", "--experimental-wasm-jspi", str(HERE / "run.mjs"), *map(str, args)]


def boot(out, *args, timeout=240):
    """Run boots in one process and return their records."""
    subprocess.run(node(*args, "--out", out), check=True, timeout=timeout)
    return json.loads(Path(out).read_text())


def differing(records):
    """The keys on which the records are not all equal."""
    return [k for k in KEYS if any(r[k] != records[0][k] for r in records)]


def expect(ok, what):
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def brief(r):
    keep = KEYS + ("why", "mode", "jit", "seed", "stepNs", "nsPerInsn", "memoryBytes", "consoleBytes", "importSequence",
                   "virtualNs", "idles", "capturedTimers", "realTimers", "inputsDelivered", "counts", "kernelStats",
                   "jitStats", "snapshot", "node")
    return {k: r[k] for k in keep if r.get(k) is not None}


def memory_diff(a, b):
    """Pages and bytes on which two memory dumps differ, and the runs of differing pages."""
    x, y = Path(a).read_bytes(), Path(b).read_bytes()
    pages = [i for i in range(0, len(x), PAGE) if x[i:i + PAGE] != y[i:i + PAGE]]
    count = sum(p != q for i in pages for p, q in zip(x[i:i + PAGE], y[i:i + PAGE]))
    runs = []
    for i in pages:
        if runs and runs[-1][1] == i:
            runs[-1][1] = i + PAGE
        else:
            runs.append([i, i + PAGE])
    return {"pages": len(pages), "of_pages": len(x) // PAGE, "bytes": count, "of_bytes": len(x),
            "runs": len(runs), "first_runs": [[hex(s), hex(e)] for s, e in runs[:8]]}


def log_diff(a, b):
    """The first import call at which two logs differ: in what was called, and in what it gave."""
    x, y = Path(a).read_text().splitlines(), Path(b).read_text().splitlines()
    first = lambda key: next((i for i, (p, q) in enumerate(zip(x, y)) if key(p) != key(q)), None)
    call, value = first(lambda line: line.split(" ")[0]), first(lambda line: line)
    excerpt = []
    for label, at in (("first value difference", value), ("first call difference", call)):
        if at is None:
            continue
        excerpt.append(f"# {label}: call {at}")
        for i in range(max(0, at - 3), min(len(x), len(y), at + 4)):
            excerpt.append(f"{i:8d} {'=' if x[i] == y[i] else '!'} A {x[i]}")
            if x[i] != y[i]:
                excerpt.append(f"{i:8d} ! B {y[i]}")
    return {"calls": [len(x), len(y)], "first_value_difference": value, "first_call_difference": call}, "\n".join(excerpt) + "\n"


def main():
    record, stock = "--record" in sys.argv, "--no-stock" not in sys.argv
    scratch = Path(tempfile.mkdtemp(prefix="unkernel_probe."))
    work, cont = HERE / "workload.json", HERE / "continuation.json"
    package = Path(os.environ["UNKERNEL"])
    summary = {"unkernel": json.loads((package / "package.json").read_text())["version"],
               "kernel_wasm_sha256": hashlib.sha256((package / "dist/unkernel.wasm").read_bytes()).hexdigest(),
               "sources": {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                           for f in (HERE / "host.mjs", HERE / "run.mjs", work, cont)}}
    try:
        controlled = ["--mode", "controlled", "--script", work]

        # 1. Baseline: the stock host, twice, at the same time. Expected to differ.
        if stock:
            procs = [subprocess.Popen(node("--mode", "stock", "--script", work, "--out", scratch / f"stock{i}.json",
                                           "--dump", scratch / f"stock{i}.mem", "--log", scratch / f"stock{i}.log"))
                     for i in (0, 1)]
            for p in procs:
                expect(p.wait(timeout=120) == 0, "stock boot ran")
            a, b = (json.loads((scratch / f"stock{i}.json").read_text())[0] for i in (0, 1))
            logs, excerpt = log_diff(scratch / "stock0.log", scratch / "stock1.log")
            summary["stock"] = {"runs": [brief(a), brief(b)], "differ_on": differing([a, b]),
                                "memory_diff": memory_diff(scratch / "stock0.mem", scratch / "stock1.mem"), "log_diff": logs}
            # Teeth: the comparison must see a difference that is really there.
            expect("memory" in differing([a, b]), "stock host: two boots differ in memory (the comparison sees it)")
            if record:
                EVIDENCE.mkdir(exist_ok=True)
                (EVIDENCE / "stock_divergence.txt").write_text(excerpt)

        # 2. Controlled host: twice in one process, once more in a fresh one.
        base = boot(scratch / "c.json", *controlled, "--repeat", 2, "--console", scratch / "c.console")
        base += boot(scratch / "c3.json", *controlled, "--dump", scratch / "c3.mem")
        expect(not differing(base), f"controlled host: 3 boots (2 in one process, 1 fresh) agree on {', '.join(KEYS)}")
        expect(all(r["why"] == "end time" and r["inputsDelivered"] == 7 and r["realTimers"] == 0 for r in base),
               "controlled host: every input delivered, ended at the virtual end time, no host timer left to the real clock")
        summary["controlled"] = {"runs": [brief(r) for r in base], "differ_on": differing(base)}

        # 3. The same with the JIT on, and with time driven by retired instructions.
        jit = boot(scratch / "j.json", *controlled, "--jit", 1, "--repeat", 2) + boot(scratch / "j3.json", *controlled, "--jit", 1)
        expect(not differing(jit), "controlled host, JIT on: 3 boots agree")
        expect(jit[0]["jitStats"]["compiled"] > 0, "JIT on: blocks were compiled")
        expect(jit[0]["console"] == base[0]["console"] and jit[0]["icount"] == base[0]["icount"],
               "JIT on and off: same console and same instruction count")
        summary["jit"] = {"runs": [brief(r) for r in jit], "differ_on": differing(jit),
                          "differs_from_interpreter_on": differing([base[0], jit[0]])}
        insn = [boot(scratch / f"i{i}.json", *controlled, "--ns-per-insn", 5)[0] for i in (0, 1)]
        expect(not differing(insn), "controlled host, clock driven by retired instructions: 2 boots agree")
        summary["instruction_clock"] = {"runs": [brief(r) for r in insn], "differ_on": differing(insn)}

        # 4. Snapshot at a fixed virtual time, twice; restore one of them twice and run on.
        snaps = [boot(scratch / f"s{i}.json", *controlled, "--snap-at", 8000, "--snap-out", scratch / f"snap{i}.bin")[0] for i in (0, 1)]
        expect(snaps[0]["why"] == "snapshot" and snaps[0]["snapshot"] == snaps[1]["snapshot"] and not differing(snaps),
               "controlled host: two snapshots taken at virtual 8 s are the same bytes")
        offset = int(snaps[0]["snapshot"]["monotonicMs"])
        resume = ["--mode", "controlled", "--script", cont, "--restore", scratch / "snap0.bin", "--end", 7000, "--epoch-offset", offset]
        restored = [boot(scratch / f"r{i}.json", *resume, "--console", scratch / f"r{i}.console")[0] for i in (0, 1)]
        expect(not differing(restored), "controlled host: two restores of one snapshot, same continuation, agree")
        expect(restored[0]["inputsDelivered"] == 3, "restore: the continuation was delivered")
        summary["snapshot"] = {"runs": [brief(r) for r in snaps]}
        summary["restore"] = {"runs": [brief(r) for r in restored], "differ_on": differing(restored)}

        # 5. Teeth: a different seed, clock step or input byte must change the digests.
        mutant = scratch / "mutant.json"
        mutant.write_text(work.read_text().replace("boot-ok", "boot-oj"))
        teeth = {
            "seed 2": boot(scratch / "t1.json", *controlled, "--seed", 2, "--dump", scratch / "t1.mem")[0],
            "clock step 1001 ns": boot(scratch / "t2.json", *controlled, "--step", 1001)[0],
            "one input byte changed": boot(scratch / "t3.json", "--mode", "controlled", "--script", mutant)[0],
            "restore with seed 2": boot(scratch / "t4.json", *resume, "--seed", 2)[0],
        }
        summary["teeth"] = {}
        for name, r in teeth.items():
            against = restored[0] if name.startswith("restore") else base[0]
            diff = differing([against, r])
            expect("memory" in diff and "imports" in diff, f"teeth: {name} changes the digests ({', '.join(diff)})")
            summary["teeth"][name] = {"differ_on": diff, "run": brief(r)}
        summary["teeth"]["seed 2"]["memory_diff"] = memory_diff(scratch / "c3.mem", scratch / "t1.mem")
        expect("console" in summary["teeth"]["seed 2"]["differ_on"], "teeth: the seed reaches the guest's /dev/urandom output")

        if record:
            EVIDENCE.mkdir(exist_ok=True)
            (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
            shutil.copy(scratch / "c.console", EVIDENCE / "console_controlled.txt")
            shutil.copy(scratch / "r0.console", EVIDENCE / "console_restore.txt")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("FAILED" if failures else "unkernel probe: all comparisons as expected")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
