"""Rerun the unkernel determinism probe and compare the digests.

    nix develop .#unkernel -c python3 experiments/unkernel_probe/check.py [--no-stock] [--record]

Every boot is `node --experimental-wasm-jspi run.mjs ...` against the pinned package ($UNKERNEL).
--no-stock skips the two wall-clock boots of the stock host (22 s of waiting, and the only part
whose result is expected to differ). --record rewrites evidence/. Without --record the fresh
digests are also compared with evidence/summary.json, which must have been recorded from these
sources and this package.
"""
import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
KEYS = ("memory", "console", "imports", "importCalls", "icount")
PAGE = 4096
BOUND = ("unkernel", "kernel_wasm_sha256", "sources")  # what evidence/summary.json is recorded from
UNSTABLE = ("stock", "faults", "environment")  # sections that are not compared with the record
# Seeded faults (run.mjs --fault): a control removed again, and whether the comparison must reject it.
FAULTS = {"entropy": True, "date": True, "jitter": True, "idle-real": True, "icount": True,
          "yield": False, "math-random": False}
failures = []


def node(*args):
    return ["node", "--experimental-wasm-jspi", str(HERE / "run.mjs"), *map(str, args)]


def boot(out, *args, timeout=240):
    """Run boots in one process and return their records."""
    subprocess.run(node(*args, "--out", out), check=True, timeout=timeout)
    return json.loads(Path(out).read_text())


def together(cmds, timeout):
    """Run commands at the same time and return their exit codes; none outlives the call."""
    procs = [subprocess.Popen(c) for c in cmds]
    try:
        return [p.wait(timeout=timeout) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()


def differing(records):
    """The keys on which the records are not all equal."""
    return [k for k in KEYS if any(r[k] != records[0][k] for r in records)]


def problems(records):
    """Why these boots do not count as the same controlled run; empty if they do."""
    found = [f"differ on {k}" for k in differing(records)]
    if not all(isinstance(r["icount"], int) and r["icount"] > 0 for r in records):
        found.append("instruction count not read")
    if any(r["realTimers"] for r in records):
        found.append("a timer was left to the real clock")
    return found


def stale(summary, recorded):
    """Why the recorded evidence is not what these sources give here; empty if it is."""
    found = [f"evidence was recorded from another {k}" for k in BOUND if summary[k] != recorded.get(k)]
    if found or summary["environment"] != recorded.get("environment"):
        return found
    return [f"fresh {k} is not the recorded one" for k in summary if k not in UNSTABLE and summary[k] != recorded.get(k)]


def expect(ok, what):
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def brief(r):
    keep = KEYS + ("why", "mode", "jit", "seed", "stepNs", "nsPerInsn", "memoryBytes", "consoleBytes", "importSequence",
                   "virtualNs", "idles", "capturedTimers", "realTimers", "inputsDelivered", "deliveredFrom",
                   "snapshotFrom", "fault", "counts", "kernelStats",
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
    if record and not stock:
        sys.exit("--record needs the stock boots")
    # "--" in the path on purpose: run.mjs once cut its arguments there.
    scratch = Path(tempfile.mkdtemp(prefix="unkernel--probe."))
    work, cont = HERE / "workload.json", HERE / "continuation.json"
    busy, busy_cont = HERE / "midrun.json", HERE / "midrun_continuation.json"
    package = Path(os.environ["UNKERNEL"])
    lock = HERE.parent.parent / "flake.lock"
    summary = {"unkernel": json.loads((package / "package.json").read_text())["version"],
               "kernel_wasm_sha256": hashlib.sha256((package / "dist/unkernel.wasm").read_bytes()).hexdigest(),
               "sources": {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                           for f in (HERE / "host.mjs", HERE / "run.mjs", HERE / "check.py", work, cont, busy, busy_cont)},
               "environment": {"system": f"{platform.machine()}-{platform.system().lower()}",
                               "node": os.path.realpath(shutil.which("node")),
                               "nixpkgs": json.loads(lock.read_text())["nodes"]["nixpkgs"]["locked"]["rev"] if lock.exists() else None}}
    try:
        controlled = ["--mode", "controlled", "--script", work]

        # 1. Baseline: the stock host, twice, at the same time. Expected to differ.
        if stock:
            codes = together([node("--mode", "stock", "--script", work, "--out", scratch / f"stock{i}.json",
                                   "--dump", scratch / f"stock{i}.mem", "--log", scratch / f"stock{i}.log")
                              for i in (0, 1)], timeout=120)
            expect(codes == [0, 0], "stock boots ran")
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
        expect(not problems(base), f"controlled host: 3 boots (2 in one process, 1 fresh) agree on {', '.join(KEYS)}")
        expect(all(r["why"] == "end time" and r["inputsDelivered"] == 7 for r in base),
               "controlled host: every input delivered, ended at the virtual end time")
        expect(base[0]["deliveredFrom"] == {"clock": 0, "poll": 0, "jump": 7},
               "controlled host: this workload's inputs all reach an idle guest (row 6 covers a running one)")
        summary["controlled"] = {"runs": [brief(r) for r in base], "differ_on": differing(base)}

        # 3. The same with the JIT on, and with time driven by retired instructions.
        jit = boot(scratch / "j.json", *controlled, "--jit", 1, "--repeat", 2) + boot(scratch / "j3.json", *controlled, "--jit", 1)
        expect(not problems(jit), "controlled host, JIT on: 3 boots agree")
        expect(jit[0]["jitStats"]["compiled"] > 0, "JIT on: blocks were compiled")
        expect(jit[0]["console"] == base[0]["console"] and jit[0]["icount"] == base[0]["icount"],
               "JIT on and off: same console and same instruction count")
        summary["jit"] = {"runs": [brief(r) for r in jit], "differ_on": differing(jit),
                          "differs_from_interpreter_on": differing([base[0], jit[0]])}
        insn = [boot(scratch / f"i{i}.json", *controlled, "--ns-per-insn", 5)[0] for i in (0, 1)]
        expect(not problems(insn), "controlled host, clock driven by retired instructions: 2 boots agree")
        expect("memory" in differing([base[0], insn[0]]), "clock driven by retired instructions: not the per-read clock's run")
        summary["instruction_clock"] = {"runs": [brief(r) for r in insn], "differ_on": differing(insn)}

        # 4. Snapshot at a fixed virtual time, twice; restore one of them twice and run on.
        snaps = [boot(scratch / f"s{i}.json", *controlled, "--snap-at", 8000, "--snap-out", scratch / f"snap{i}.bin")[0] for i in (0, 1)]
        expect(snaps[0]["why"] == "snapshot" and snaps[0]["snapshot"] == snaps[1]["snapshot"] and not problems(snaps),
               "controlled host: two snapshots taken at virtual 8 s are the same bytes")
        offset = int(snaps[0]["snapshot"]["monotonicMs"])
        resume = ["--mode", "controlled", "--script", cont, "--restore", scratch / "snap0.bin", "--end", 7000, "--epoch-offset", offset]
        restored = [boot(scratch / f"r{i}.json", *resume, "--console", scratch / f"r{i}.console")[0] for i in (0, 1)]
        expect(not problems(restored), "controlled host: two restores of one snapshot, same continuation, agree")
        expect(restored[0]["inputsDelivered"] == 3, "restore: the continuation was delivered")
        summary["snapshot"] = {"runs": [brief(r) for r in snaps]}
        summary["restore"] = {"runs": [brief(r) for r in restored], "differ_on": differing(restored)}

        # 5. Teeth: a different seed, clock step, input byte, input time or order, or date must change the digests.
        lines = json.loads(work.read_text())
        mutants = {"one input byte changed": json.loads(work.read_text().replace("boot-ok", "boot-oj")),
                   "first input 1 ms later": [[lines[0][0] + 1, lines[0][1]]] + lines[1:],
                   "two input lines swapped": [[lines[0][0], lines[1][1]], [lines[1][0], lines[0][1]]] + lines[2:]}
        teeth = {
            "seed 2": boot(scratch / "t1.json", *controlled, "--seed", 2, "--dump", scratch / "t1.mem")[0],
            "clock step 1001 ns": boot(scratch / "t2.json", *controlled, "--step", 1001)[0],
            "date 1 ms later": boot(scratch / "t3.json", *controlled, "--epoch-offset", 1)[0],
            "restore with seed 2": boot(scratch / "t4.json", *resume, "--seed", 2)[0],
        }
        for i, (name, script) in enumerate(mutants.items()):
            (scratch / f"mutant{i}.json").write_text(json.dumps(script))
            teeth[name] = boot(scratch / f"u{i}.json", "--mode", "controlled", "--script", scratch / f"mutant{i}.json")[0]
        summary["teeth"] = {}
        for name, r in teeth.items():
            against = restored[0] if name.startswith("restore") else base[0]
            diff = differing([against, r])
            expect("memory" in diff and "imports" in diff, f"teeth: {name} changes the digests ({', '.join(diff)})")
            summary["teeth"][name] = {"differ_on": diff, "run": brief(r)}
        summary["teeth"]["seed 2"]["memory_diff"] = memory_diff(scratch / "c3.mem", scratch / "t1.mem")
        expect("console" in summary["teeth"]["seed 2"]["differ_on"], "teeth: the seed reaches the guest's /dev/urandom output")

        # 6. Events that reach a running guest: input and the snapshot request fall inside a busy
        # loop, with the clock driven by instructions, so they are delivered from a clock read.
        mid = ["--mode", "controlled", "--script", busy, "--ns-per-insn", 5, "--end", 12000]
        running = boot(scratch / "m.json", *mid, "--repeat", 2, "--console", scratch / "m.console") + boot(scratch / "m3.json", *mid)
        expect(not problems(running), "running guest: 3 boots (2 in one process, 1 fresh) agree")
        expect(all(r["deliveredFrom"]["clock"] >= 2 and r["inputsDelivered"] == 4 for r in running),
               "running guest: at least two inputs were delivered from a clock read, inside the loop")
        running_jit = boot(scratch / "mj.json", *mid, "--jit", 1, "--repeat", 2)
        expect(not problems(running_jit) and running_jit[0]["deliveredFrom"]["clock"] >= 2, "running guest, JIT on: 2 boots agree")
        expect(running_jit[0]["console"] == running[0]["console"] and running_jit[0]["icount"] == running[0]["icount"],
               "running guest, JIT on and off: same console and same instruction count")
        mid_snaps = [boot(scratch / f"ms{i}.json", *mid[:-2], "--snap-at", 3600, "--snap-out", scratch / f"msnap{i}.bin")[0] for i in (0, 1)]
        expect(mid_snaps[0]["snapshot"] == mid_snaps[1]["snapshot"] and not problems(mid_snaps),
               "running guest: two snapshots taken inside the loop are the same bytes")
        expect(all(r["why"] == "snapshot" and r["snapshotFrom"] == "clock" for r in mid_snaps),
               "running guest: the snapshot was requested from a clock read")
        mid_resume = ["--mode", "controlled", "--script", busy_cont, "--ns-per-insn", 5, "--restore", scratch / "msnap0.bin",
                      "--end", 8000, "--epoch-offset", int(mid_snaps[0]["snapshot"]["monotonicMs"])]
        mid_restored = [boot(scratch / f"mr{i}.json", *mid_resume, "--console", scratch / f"mr{i}.console")[0] for i in (0, 1)]
        expect(not problems(mid_restored), "running guest: two restores of the mid-loop snapshot agree")
        expect(mid_restored[0]["deliveredFrom"]["clock"] >= 1 and mid_restored[0]["inputsDelivered"] == 2,
               "running guest: the restored loop took an input from a clock read")
        summary["running_guest"] = {"runs": [brief(r) for r in running], "jit": [brief(r) for r in running_jit],
                                    "snapshot": [brief(r) for r in mid_snaps], "restore": [brief(r) for r in mid_restored]}

        # 7. Seeded faults: one control removed. The comparison used above must reject the ones
        # the result rests on; the others are recorded as not needed by this workload.
        summary["faults"] = {}
        for i, (fault, needed) in enumerate(FAULTS.items()):
            found = problems([base[0], boot(scratch / f"f{i}.json", *controlled, "--fault", fault)[0]])
            if needed:
                expect(found, f"fault {fault}: rejected ({'; '.join(found)})")
            else:
                # The log may differ (a passed-through sched_yield returns a promise); the guest may not.
                expect(set(found) <= {"differ on imports"},
                       f"fault {fault}: same memory, console and instruction count, so this workload does not show the control is needed")
            summary["faults"][fault] = {"needed": needed, "rejected_by": found}
        blind = subprocess.run(node(*controlled, "--ns-per-insn", 5, "--fault", "icount", "--out", scratch / "fi.json"),
                               timeout=240, capture_output=True, text=True)
        expect(blind.returncode != 0 and "instruction counter cannot be read" in blind.stderr,
               "fault icount: an instruction-driven clock refuses to run without the counter")

        # 8. The retained evidence is what these sources give.
        if record:
            EVIDENCE.mkdir(exist_ok=True)
            (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
            shutil.copy(scratch / "c.console", EVIDENCE / "console_controlled.txt")
            shutil.copy(scratch / "r0.console", EVIDENCE / "console_restore.txt")
            shutil.copy(scratch / "m.console", EVIDENCE / "console_running_guest.txt")
            shutil.copy(scratch / "mr0.console", EVIDENCE / "console_running_guest_restore.txt")
        recorded = json.loads((EVIDENCE / "summary.json").read_text())
        found = stale(summary, recorded)
        expect(not found, "evidence/summary.json is from these sources and this package" + "".join(f"; {f}" for f in found))
        if summary["environment"] == recorded.get("environment"):
            expect(all(summary[k] == recorded[k] for k in summary if k not in UNSTABLE),
                   "evidence/summary.json: every recorded controlled digest was reproduced")
            for label, path in (("controlled", "c.console"), ("restore", "r0.console"), ("running_guest", "m.console"),
                                ("running_guest_restore", "mr0.console")):
                expect((scratch / path).read_bytes() == (EVIDENCE / f"console_{label}.txt").read_bytes(),
                       f"evidence/console_{label}.txt is the console of this run")
        else:
            print(f"skip  evidence digests not compared: recorded on {recorded.get('environment')}, this is {summary['environment']}")
        # Teeth: a changed digest and a changed source hash in the record must both be seen.
        forged, other = copy.deepcopy(summary), copy.deepcopy(summary)
        forged["controlled"]["runs"][0]["memory"] = "0" * 64
        other["sources"]["host.mjs"] = "deadbeef"
        expect(stale(summary, forged) == ["fresh controlled is not the recorded one"], "teeth: a forged digest in the record is seen")
        expect(stale(summary, other) == ["evidence was recorded from another sources"], "teeth: a record from other sources is seen")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("FAILED" if failures else "unkernel probe: all comparisons as expected")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
