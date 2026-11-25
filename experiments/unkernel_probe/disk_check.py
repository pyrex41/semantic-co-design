"""Does the unkernel guest stay deterministic once it uses a block device? Rerun and compare.

    nix develop .#unkernel -c python3 experiments/unkernel_probe/disk_check.py [--record]

Every boot is `node --experimental-wasm-jspi disk.mjs ...` against the pinned package ($UNKERNEL),
in a fresh process with a time limit; a few run at the same time. --record rewrites
evidence/disk_*. Without it the fresh digests are also compared with evidence/disk_summary.json,
which must have been recorded from these sources and this package.
"""
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from check import expect, failures, log_diff  # noqa: E402

EVIDENCE = HERE / "evidence"
# What two boots must agree on: the guest (as in check.py) and the disk: the final image, the
# image holding only what was flushed, and the sequence of requests with their data.
KEYS = ("memory", "console", "imports", "importCalls", "icount", "disk", "diskDurable", "diskLog")
SOURCES = ("host.mjs", "disk.mjs", "disk_check.py", "check.py", "disk_workload.json", "disk_continuation.json",
           "disk_crash.json", "disk_recover.json")
UNSTABLE = ("fault_real", "environment")  # sections that are not compared with the record
LIMIT = 120  # seconds of wall time for one boot
CRASH_AT = 48  # the request before the flush that would make the third transaction's commit block durable


def differing(records, keys=KEYS):
    return [k for k in keys if any(r[k] != records[0][k] for r in records)]


def problems(records):
    """Why these boots do not count as the same run; empty if they do."""
    found = [f"differ on {k}" for k in differing(records)]
    if not all(isinstance(r["icount"], int) and r["icount"] > 0 for r in records):
        found.append("instruction count not read")
    if any(r["realTimers"] for r in records):
        found.append("a timer was left to the real clock")
    if any(r["blockStats"]["inflight"] or r["blockStats"]["unreaped"] or r.get("pendingVirtual") or r.get("pendingReal") for r in records):
        found.append("a disk request was still in flight at the end")
    return found


def stale(summary, recorded):
    """Why the recorded evidence is not what these sources give here; empty if it is."""
    found = [f"evidence was recorded from another {k}" for k in ("unkernel", "kernel_wasm_sha256", "sources") if summary[k] != recorded.get(k)]
    if found or summary["environment"] != recorded.get("environment"):
        return found
    return [f"fresh {k} is not the recorded one" for k in summary if k not in UNSTABLE and summary[k] != recorded.get(k)]


def brief(r):
    drop = ("node", "counts", "kernelStats", "importSequence", "memoryBytes", "capturedTimers", "base")
    return {k: v for k, v in r.items() if v is not None and k not in drop}


class Boots:
    """Boots of disk.mjs, each in its own process, a few at a time, every one with a time limit."""

    def __init__(self, scratch):
        self.scratch, self.pool, self.n = scratch, ThreadPoolExecutor(max_workers=4), 0

    def start(self, *args, script="disk_workload.json", keep=()):
        self.n += 1
        base = self.scratch / f"b{self.n}"
        cmd = ["node", "--experimental-wasm-jspi", str(HERE / "disk.mjs"), "--script", str(HERE / script),  # or an absolute path
               "--out", f"{base}.json", "--wall-s", str(LIMIT - 20), *map(str, args)]
        for k in keep:  # console, log, disk-log, image-out, durable-out, snap-out
            cmd += [f"--{k}", f"{base}.{k}"]

        def run():
            p = subprocess.run(cmd, timeout=LIMIT, capture_output=True, text=True)
            if p.returncode:
                return {"error": p.stderr.strip().splitlines()[-8:], "base": base}
            return dict(json.loads(Path(f"{base}.json").read_text())[0], base=base)
        return self.pool.submit(run)

    def run(self, *argsets, may_fail=False, **kw):
        records = [f.result() for f in [self.start(*a, **kw) for a in argsets]]
        if not may_fail and not ok(records):
            raise RuntimeError("a boot failed")
        return records


def ok(records):
    bad = [r["error"] for r in records if "error" in r]
    for e in bad:
        print("      boot failed:", *e, sep="\n        ")
    return not bad


def main():
    record = "--record" in sys.argv
    scratch = Path(tempfile.mkdtemp(prefix="unkernel-disk."))
    package = Path(os.environ["UNKERNEL"])
    lock = HERE.parent.parent / "flake.lock"
    summary = {"unkernel": json.loads((package / "package.json").read_text())["version"],
               "kernel_wasm_sha256": hashlib.sha256((package / "dist/unkernel.wasm").read_bytes()).hexdigest(),
               "sources": {n: hashlib.sha256((HERE / n).read_bytes()).hexdigest() for n in SOURCES},
               "environment": {"system": f"{platform.machine()}-{platform.system().lower()}",
                               "node": os.path.realpath(shutil.which("node")),
                               "nixpkgs": json.loads(lock.read_text())["nodes"]["nixpkgs"]["locked"]["rev"] if lock.exists() else None}}
    boots = Boots(scratch)
    burners = []
    try:
        # 1. Completion inside the submit import (what the SDK's own in-memory and file disks do).
        sync = boots.run([], [], [], keep=("console", "disk-log"))
        expect(ok(sync) and not problems(sync), f"sync completion: 3 fresh boots agree on {', '.join(KEYS)}")
        expect(all(r["why"] == "end time" and r["inputsDelivered"] == 7 and r["blockStats"]["async"] == 0 for r in sync),
               "sync completion: every input delivered, no request went the asynchronous way")
        expect(all(r["diskOps"]["write"] > 0 and r["diskOps"]["flush"] > 0 and r["diskOps"]["read"] > 0 for r in sync),
               "the workload reads, writes and flushes the disk")
        text = Path(f"{sync[0]['base']}.console").read_text(errors="replace")
        read_back = re.findall(r"^([0-9a-f]{64})  /(?:data|mnt)/r\b", text, re.M)
        expect(len(read_back) == 3 and len(set(read_back)) == 1 and "unmounted" in text,
               "the guest read its file back from the disk after a remount and after dropping caches (same sha256 three times)")
        summary["sync"] = {"runs": [brief(r) for r in sync], "differ_on": differing(sync)}

        cow = boots.run(["--backend", "cow"], ["--backend", "cow"])
        expect(ok(cow) and not problems(cow) and cow[0]["blockStats"]["async"] == 0,
               "the SDK's own in-memory CowDisk (it completes synchronously): 2 fresh boots agree, final image included")
        summary["sdk_cowdisk"] = {"runs": [brief(r) for r in cow]}

        # 2. The asynchronous path (the host answers 1, completes later, raises IRQ 3).
        micro = boots.run(["--completion", "microtask"], ["--completion", "microtask"])
        expect(ok(micro) and not problems(micro) and micro[0]["blockStats"]["async"] > 100,
               f"asynchronous completion from a resolved promise: 2 fresh boots agree ({micro[0]['blockStats']['async']} asynchronous requests)")
        virtual = boots.run(*[["--completion", "virtual"]] * 3, keep=("log",))
        expect(ok(virtual) and not problems(virtual) and virtual[0]["completedVirtual"] > 100,
               f"asynchronous completion at a virtual time (200 us after the submit): 3 fresh boots agree ({virtual[0]['completedVirtual']} completions)")
        spread = boots.run(*[["--completion", "virtual", "--latency-spread-us", 5000]] * 2)
        expect(ok(spread) and not problems(spread), "virtual completion with a seeded latency of 200 to 5200 us: 2 fresh boots agree")
        expect("imports" in differing([virtual[0], spread[0]]), "the seeded latency gives another run than the fixed one")
        summary["microtask"] = {"runs": [brief(r) for r in micro]}
        summary["virtual"] = {"runs": [brief(r) for r in virtual], "spread": [brief(r) for r in spread]}
        expect(all("memory" in differing([sync[0], r]) for r in (micro[0], virtual[0])),
               "the three completion rules are three different runs (the comparison tells them apart)")

        # 3. Attack: real delays of random length inside every request, on a loaded machine.
        burners = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(4)]
        slow = boots.run(["--host-delay-ms", 3], ["--completion", "microtask", "--host-delay-ms", 3],
                         ["--completion", "virtual", "--host-delay-ms", 3], ["--completion", "virtual", "--latency-spread-us", 5000, "--host-delay-ms", 3])
        for b in burners:
            b.kill()
            b.wait()
        burners = []
        for name, r, good in zip(("sync", "microtask", "virtual", "virtual with seeded latency"), slow, (sync, micro, virtual, spread)):
            expect(ok([r]) and not problems([good[0], r]),
                   f"attack, {name}: 0 to 3 ms of real delay in each request and 4 busy processes change no digest")
        summary["host_delay"] = {"runs": [brief(r) for r in slow]}

        # 4. Seeded fault: asynchronous completions left to the host's event loop (real timers of
        # random length). The same comparison must reject it.
        real = boots.run(*[["--completion", "real"]] * 3, keep=("log", "disk-log"))
        found = problems(real)
        expect(found, f"fault real: completions from real timers are rejected ({'; '.join(found)})")
        expect(all(r["why"] == "end time" and r["inputsDelivered"] == 7 and not r["blockStats"]["inflight"] for r in real),
               "fault real: each of those boots finished its workload (it is the result that differs, not a hang)")
        pair = next(((a, b) for a in real for b in real if a["imports"] != b["imports"]), None)
        excerpt = ""
        if pair:
            logs, _ = log_diff(f"{pair[0]['base']}.log", f"{pair[1]['base']}.log")
            at = logs["first_value_difference"]
            x, y = (Path(f"{r['base']}.log").read_text().splitlines() for r in pair)
            # The excerpt reaches back to the submits whose completions are reaped in another order.
            excerpt = f"# two boots with --completion real; first difference: import call {at}\n"
            for i in range(max(0, at - 12), min(len(x), len(y), at + 7)):
                excerpt += f"{i:8d} {'=' if x[i] == y[i] else '!'} A {x[i].rstrip()}\n" + (f"{i:8d} ! B {y[i].rstrip()}\n" if x[i] != y[i] else "")
            expect(x[at].startswith("wasm_driver_block_reap(") or y[at].startswith("wasm_driver_block_reap("),
                   f"fault real: the first differing import call is a block reap (call {at}: {x[at].strip()} against {y[at].strip()})")
            summary["fault_real"] = {"runs": [brief(r) for r in real], "rejected_by": found, "log_diff": logs,
                                     "first_difference": [x[at].strip(), y[at].strip()]}
        starve = boots.run(["--completion", "real", "--real-starve", 1, "--end", 4000], script="disk_recover.json")
        expect(ok(starve) and starve[0]["diskOps"] == {"read": 1, "write": 0, "flush": 0, "discard": 0, "zeroes": 0}
               and starve[0]["blockStats"]["inflight"] == 1 and starve[0]["icount"] == 0,
               "fault real without the standing clock: the first read never completes (the idle wait never returns to the event loop)")
        summary["real_starved"] = {"runs": [brief(r) for r in starve]}

        # 5. JIT on.
        jit = boots.run(["--jit", 1], ["--jit", 1], ["--jit", 1, "--completion", "virtual"], ["--jit", 1, "--completion", "virtual"])
        expect(ok(jit) and not problems(jit[:2]) and not problems(jit[2:]), "JIT on: 2 boots with sync and 2 with virtual completion agree")
        expect(all(r["jitStats"]["compiled"] > 0 for r in jit), "JIT on: blocks were compiled")
        same = [k for k in KEYS if jit[0][k] == sync[0][k]]
        expect({"console", "icount"} <= set(same), f"JIT on and off, sync completion: same {', '.join(same)}")
        summary["jit"] = {"runs": [brief(r) for r in jit], "same_as_interpreter": same,
                          "virtual_same_as_interpreter": [k for k in KEYS if jit[2][k] == virtual[0][k]]}

        # 6. Snapshot inside the concurrent write (requested from a clock read), and restore.
        mid = ["--ns-per-insn", 5, "--snap-at", 10050]
        snaps = boots.run(mid, mid, mid + ["--completion", "virtual"], mid + ["--completion", "virtual"], keep=("snap-out", "image-out"))
        summary["snapshot"], summary["restore"] = {}, {}
        for name, pair in (("sync", snaps[:2]), ("virtual", snaps[2:])):
            expect(ok(pair) and pair[0]["snapshot"] == pair[1]["snapshot"] and not problems(pair),
                   f"snapshot, {name} completion: two snapshots taken while the guest writes are the same bytes, with the same sealed disk")
            expect(all(r["why"] == "snapshot" and r["snapshotFrom"] == "clock" and r["snapshot"]["disks"][0]["gen"] == 1 for r in pair),
                   f"snapshot, {name}: requested from a clock read; the SDK recorded the disk at write generation 1")
            summary["snapshot"][name] = [brief(r) for r in pair]
        resume = lambda s, *more: ["--ns-per-insn", 5, "--restore", f"{s['base']}.snap-out", "--image", f"{s['base']}.image-out", "--gen", 1,
                                   "--end", 7000, "--epoch-offset", int(s["snapshot"]["monotonicMs"]), *more]
        restored = boots.run(resume(snaps[0]), resume(snaps[0]), resume(snaps[2], "--completion", "virtual"), resume(snaps[2], "--completion", "virtual"),
                             resume(snaps[0], "--gen", 2), resume(snaps[0], "--image", f"{snaps[2]['base']}.image-out"),
                             script="disk_continuation.json", keep=("console",), may_fail=True)
        for name, pair in (("sync", restored[:2]), ("virtual", restored[2:4])):
            expect(ok(pair) and not problems(pair) and pair[0]["inputsDelivered"] == 2 and pair[0]["diskOps"]["write"] > 0,
                   f"restore, {name} completion: two restores with the sealed image agree after writing on")
            summary["restore"][name] = [brief(r) for r in pair]
        refused = " ".join(restored[4].get("error", []))
        expect("was written after the snapshot (generation 2, the snapshot has 1)" in refused, "restore: the SDK refuses a disk at another write generation")
        expect(ok([restored[5]]) and "disk" in differing([restored[0], restored[5]]),
               "restore: another image under the same identity and generation is NOT refused (the SDK checks what the disk says, not its content)")
        summary["restore"]["other_image_same_generation"] = brief(restored[5])

        # 7. Power cut: keep only what the guest flushed, and boot a second guest on that.
        cut = boots.run(*[["--end", 10000, "--crash-at-op", CRASH_AT]] * 2, script="disk_crash.json", keep=("durable-out", "image-out", "disk-log"))
        cut_ok = ok(cut) and not differing(cut) and all(r["why"].startswith("power cut") for r in cut)
        expect(cut_ok, f"power cut at disk request {CRASH_AT}: 2 fresh boots agree (image, flushed image, requests)")
        expect(cut[0]["unflushedWrites"] == 1 and cut[0]["unflushedBytes"] == 4096 and cut[0]["disk"] != cut[0]["diskDurable"],
               "power cut: one written block (4096 bytes) had not been flushed, so the flushed image is not the written one")
        after = boots.run(*[["--end", 7000, "--image", f"{cut[0]['base']}.durable-out"]] * 2, ["--end", 7000, "--image", f"{cut[0]['base']}.image-out"],
                          script="disk_recover.json", keep=("console",))
        expect(ok(after) and not problems(after[:2]), "second guest on the flushed image: 2 fresh boots agree")
        kept, lost = (Path(f"{r['base']}.console").read_text(errors="replace") for r in (after[2], after[0]))
        expect("recovery complete" in lost and "synced-1" in lost and "fsynced-2" in lost and "can't open '/data/u3'" in lost and "unmounted" in lost,
               "second guest on the flushed image: ext4 replays its journal; the synced and the fsynced file are there, the unsynced one is not")
        expect("unsynced-3" in kept and "can't open" not in kept,
               "second guest on the image with the unflushed block kept: the unsynced file is there (the dropped block decides)")
        summary["power_cut"] = {"runs": [brief(r) for r in cut], "second_guest": [brief(r) for r in after[:2]], "second_guest_unflushed_kept": brief(after[2])}
        ops = [line.split()[1] for line in Path(f"{cut[0]['base']}.disk-log").read_text().splitlines()]
        expect(ops[-4:] == ["write", "write", "flush", "write"] and set(ops) == {"read", "write", "flush"},
               "the host sees flush requests as requests of their own; there is no FUA flag (journal commit is write, flush, write, flush)")

        # 8. Teeth: one changed write, one changed image byte, one changed input byte, another latency.
        work = (HERE / "disk_workload.json").read_text()
        (scratch / "mutant.json").write_text(work.replace("file-$f", "file_$f"))
        # The write that carries the 256 KiB file the guest reads back, by its place among the writes.
        writes = [line.split() for line in Path(f"{sync[0]['base']}.disk-log").read_text().splitlines() if line.split()[1] == "write"]
        data_write = next(i for i, w in enumerate(writes, 1) if w[3] == "262144")
        teeth = boots.run(["--tamper-write", data_write], ["--flip-image", 3 * 2**30 + 5], ["--completion", "virtual", "--latency-us", 201],
                          ["--completion", "virtual", "--latency-spread-us", 5000, "--latency-seed", 8], ["--tamper-write", 1])
        teeth += boots.run([], script=scratch / "mutant.json")
        names = ("one bit of one data write changed as stored", "one byte of the initial image changed, in a block the guest never reads",
                 "virtual latency 201 us instead of 200", "latency seed 8 instead of 7",
                 "one bit of the first write changed as stored (the guest writes that block again)", "one byte of console input changed")
        against = (sync[0], sync[0], virtual[0], spread[0], sync[0], sync[0])
        summary["teeth"] = {}
        for name, r, good in zip(names, teeth, against):
            diff = differing([good, r])
            expect(diff, f"teeth: {name}: {', '.join(diff)} differ")
            summary["teeth"][name] = {"differ_on": diff, "run": brief(r)}
        expect({"disk", "diskLog", "console"} <= set(summary["teeth"][names[0]]["differ_on"]),
               "teeth: the changed write reaches the image, the request log and (read back by the guest) the console")
        expect(summary["teeth"][names[1]]["differ_on"] == ["disk", "diskDurable"],
               "teeth: the unread image byte changes only the image digests, so the image has to be compared on its own")
        expect(summary["teeth"][names[4]]["differ_on"] == ["diskLog"],
               "teeth: a changed write that is overwritten later shows only in the request log, so the requests have to be compared too")
        expect("disk" in summary["teeth"][names[5]]["differ_on"], "teeth: the changed input byte reaches the image")

        # 9. The retained evidence is what these sources give.
        text = json.dumps(summary, indent=1) + "\n"
        summary = json.loads(text)  # as the record will read: tuples are lists
        if record:
            EVIDENCE.mkdir(exist_ok=True)
            (EVIDENCE / "disk_summary.json").write_text(text)
            (EVIDENCE / "disk_real_divergence.txt").write_text(excerpt)
            shutil.copy(f"{sync[0]['base']}.console", EVIDENCE / "disk_console_sync.txt")
            shutil.copy(f"{after[0]['base']}.console", EVIDENCE / "disk_console_after_power_cut.txt")
            shutil.copy(f"{cut[0]['base']}.disk-log", EVIDENCE / "disk_requests_power_cut.txt")
        recorded = json.loads((EVIDENCE / "disk_summary.json").read_text())
        found = stale(summary, recorded)
        expect(not found, "evidence/disk_summary.json is from these sources and this package" + "".join(f"; {f}" for f in found))
        if summary["environment"] == recorded.get("environment"):
            expect(all(summary[k] == recorded[k] for k in summary if k not in UNSTABLE),
                   "evidence/disk_summary.json: every recorded deterministic digest was reproduced")
            for src, name in ((f"{sync[0]['base']}.console", "disk_console_sync.txt"), (f"{after[0]['base']}.console", "disk_console_after_power_cut.txt"),
                              (f"{cut[0]['base']}.disk-log", "disk_requests_power_cut.txt")):
                expect(Path(src).read_bytes() == (EVIDENCE / name).read_bytes(), f"evidence/{name} is what this run gave")
        else:
            print(f"skip  evidence digests not compared: recorded on {recorded.get('environment')}, this is {summary['environment']}")
        forged = copy.deepcopy(summary)
        forged["sync"]["runs"][0]["disk"] = "0" * 64
        expect(stale(summary, forged) == ["fresh sync is not the recorded one"], "teeth: a forged image digest in the record is seen")
    finally:
        for b in burners:
            b.kill()
            b.wait()
        boots.pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(scratch, ignore_errors=True)
    print("FAILED" if failures else "unkernel disk probe: all comparisons as expected")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
