"""The compiled accepted_job node as a Linux program inside the unkernel guest: is it the node, and
is the run deterministic?

    nix develop .#uk-node -c python3 experiments/unkernel_probe/guest_node_check.py [--record] [--quick] [--keep DIR]

Builds the node for aarch64-linux (guest_node_build.py), boots the pinned unkernel under the
controlled host with the binary in the initramfs (guest_node.mjs), and fails unless everything
below is as stated. --record rewrites evidence/guest_node_*. --quick is what `nix flake check`
runs (uk-node): one build instead of two, no mutant, no timing, no 9p mount. --keep leaves
the build and every record in DIR. Every subprocess has a timeout; the whole run is bounded by
their sum (about 25 minutes) and takes about 2.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
EVIDENCE = HERE / "evidence"
KEYS = ("memory", "console", "imports", "importCalls", "icount")
# Console lines at virtual ms. Every command has finished long before the next line arrives
# (asserted: every line is delivered at an idle jump).
SESSION = [
    [3000, "sha256sum /node/node /node/history /node/vectors.txt /node/history.txt\n"],
    [5000, "echo BEGIN-ROWS; /node/node < /node/vectors.txt; echo END-ROWS $?\n"],
    [8000, "/node/node < /node/vectors.txt | sha256sum\n"],
    [10000, "echo BEGIN-HIST; /node/history < /node/history.txt; echo END-HIST $?\n"],
    [30000, "/node/history < /node/history.txt | sha256sum\n"],
]
SESSION_END = 40000
BENCH = 2000  # guest_node_bench.c: this many passes over the distinct rows, beyond the first
PERF = [
    [3000, "/node/node < /dev/null\n"],
    [5000, "/node/node < /dev/null\n"],
    [10000, "/node/node < /node/perf.txt > /dev/null\n"],
    [100000, "/node/node < /node/perf.txt > /dev/null\n"],
    [200000, "/node/node < /node/perf.txt | sha256sum\n"],
    [300000, "/node/bench 1 < /node/distinct.txt\n"],
    [301000, f"/node/bench {BENCH + 1} < /node/distinct.txt\n"],
    [400000, f"/node/bench {BENCH + 1} < /node/distinct.txt\n"],
]
PERF_END = 500000
FAULTS = ("entropy", "date", "jitter", "idle-real")
failures = []


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else Path(data).read_bytes()).hexdigest()


def require(ok, message):
    print(("ok    " if ok else "FAIL  ") + message)
    if not ok:
        failures.append(message)
    return ok


def guest(work, name, files, script, end, *more, deliver="initrd", timeout=240):
    """One boot in a fresh process. Returns (record, console bytes)."""
    path = work / f"{name}.script.json"
    path.write_text(json.dumps(script))
    cmd = ["node", "--experimental-wasm-jspi", str(HERE / "guest_node.mjs"), "--deliver", deliver]
    for guest_path, host_path in files:
        cmd += ["--file", f"{guest_path}={host_path}"]
    cmd += ["--script", str(path), "--end", str(end), "--out", str(work / f"{name}.json"),
            "--console", str(work / f"{name}.console"), *map(str, more)]
    subprocess.run(cmd, check=True, timeout=timeout)
    return json.loads((work / f"{name}.json").read_text()), (work / f"{name}.console").read_bytes()


def differing(records):
    return [k for k in KEYS if any(r[k] != records[0][k] for r in records)]


def between(console, begin, end):
    """The console lines after the line `begin` up to the line starting with `end`, and that line.
    The terminal turns each "\\n" the program writes into "\\r\\n"; exactly that is undone."""
    lines = console.decode("utf-8", "replace").split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    start = lines.index(begin)
    stop = next(i for i in range(start + 1, len(lines)) if lines[i].startswith(end))
    return lines[start + 1:stop], lines[stop]


def piped_digests(console):
    """What `... | sha256sum` printed in the guest, in order."""
    return re.findall(r"^([0-9a-f]{64})  -\r?$", console.decode("utf-8", "replace"), re.M)


def first_difference(got, want):
    for n, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return f"line {n + 1}: guest `{a}` native `{b}`"
    return None if len(got) == len(want) else f"{len(got)} lines, expected {len(want)}"


def environment():
    return {"system": f"{platform.machine()}-{platform.system().lower()}",
            "node": shutil.which("node"), "unkernel": os.environ.get("UNKERNEL"), "zig": os.environ.get("ZIG")}


def native_timing(build, facts):
    """Seconds per row for the native line executable, the native library and the wasm module."""
    sys.path.insert(0, str(ROOT))
    from tool.compile import Native
    from tool.schema import Schema
    from tool.wasm import Wasm
    schema = Schema(ROOT / "tool" / "examples" / "accepted_job" / "node.json")
    perf = (build / "perf.txt").read_bytes()

    def process(data):
        best = None
        for _ in range(7):
            t = time.perf_counter()
            out = subprocess.run([str(build / "node")], input=data, capture_output=True, check=True, timeout=120).stdout
            best = min(best or 1e9, time.perf_counter() - t)
        return best, out
    empty, _ = process(b"")
    full, out = process(perf)
    rows = [[int(x) for x in line.split()] for line in perf.decode().splitlines()[:facts["distinct_rows"]]]

    def in_process(node):
        best = None
        for _ in range(5):
            t = time.perf_counter()
            for _ in range(20):
                for row in rows:
                    node.raw(row)
            best = min(best or 1e9, (time.perf_counter() - t) / (20 * len(rows)))
        return best
    def bench(repeats):
        best = None
        for _ in range(5):
            t = time.perf_counter()
            out = subprocess.run([str(build / "bench"), str(repeats)], input=(build / "distinct.txt").read_bytes(),
                                 capture_output=True, check=True, timeout=120).stdout.decode().strip()
            best = min(best or 1e9, time.perf_counter() - t)
        return best, out
    one, first_line = bench(1)
    many, line = bench(1 + 50 * BENCH)
    _, guest_line = bench(1 + BENCH)
    library = Native(schema, build / ("node.dylib" if sys.platform == "darwin" else "node.so"))
    module = Wasm(schema, build / "node.wasm")
    return {"native_process_empty_s": empty, "native_process_perf_s": full,
            "native_line_executable_ns_per_row": (full - empty) / facts["perf_rows"] * 1e9,
            "native_library_from_python_ns_per_step": in_process(library) * 1e9,
            "wasmtime_from_python_ns_per_step": in_process(module) * 1e9,
            "native_ns_per_step": (many - one) / (50 * BENCH * facts["distinct_rows"]) * 1e9,
            "bench_lines": [first_line, guest_line, guest_line], "perf_output_sha256": sha(out)}


def main(argv):
    record, quick = "--record" in argv, "--quick" in argv
    keep = Path(argv[argv.index("--keep") + 1]).resolve() if "--keep" in argv else None
    work = keep or Path(tempfile.mkdtemp(prefix="guest-node-"))
    work.mkdir(parents=True, exist_ok=True)
    build = work / "build"
    summary = {"environment": environment()}
    try:
        # 1. The binary.
        subprocess.run([sys.executable, str(HERE / "guest_node_build.py"), str(build), *(["--quick"] * quick)],
                       check=True, timeout=900, stdout=subprocess.DEVNULL)
        facts = summary["build"] = json.loads((build / "build.json").read_text())
        if not quick:
            require(facts["node_aarch64_sha256"] == facts["node_aarch64_second_build_sha256"],
                    f"build: two builds with empty compiler caches give the same {facts['node_aarch64_bytes']} bytes "
                    f"({facts['node_aarch64_sha256'][:16]})")
        require(facts["native_trap_lines"] == facts["trap_rows"] and facts["native_trap_lines_among_distinct"] == 0,
                f"rows: {facts['distinct_rows']} distinct (state, event) rows of {facts['simulated_steps']} simulated "
                f"steps, none traps natively; {facts['trap_rows']} more rows, all trap natively")

        files = [("/node/node", build / "node.aarch64"), ("/node/history", build / "history.aarch64"),
                 ("/node/vectors.txt", build / "vectors.txt"), ("/node/history.txt", build / "history.txt")]

        # 2 and 3. Delivery through the initramfs; equivalence with the native executable.
        first, console = guest(work, "a", files, SESSION, SESSION_END)
        (work / "session.console").write_bytes(console)
        text = console.decode("utf-8", "replace")
        seen = dict((name, digest) for digest, name in re.findall(r"^([0-9a-f]{64})  (/node/\S+)\r?$", text, re.M))
        require(seen == {path: sha(host) for path, host in files},
                "delivery: sha256sum in the guest gives the host's digest for each of the 4 files in the initramfs")
        require(first["deliveredFrom"] == {"clock": 0, "poll": 0, "jump": len(SESSION)} and first["why"] == "end time"
                and first["realTimers"] == 0, f"session: {len(SESSION)} lines, each delivered to an idle guest; no real timer")
        rows, tail = between(console, "BEGIN-ROWS", "END-ROWS")
        want = (build / "expected.txt").read_text().splitlines()
        diff = first_difference(rows, want)
        require(diff is None and tail == "END-ROWS 0",
                f"equivalence: the guest's {len(rows)} output lines equal the native executable's, line for line"
                + (f" [{diff}; {tail}]" if diff or tail != "END-ROWS 0" else ""))
        require(sum(line.startswith("trap") for line in rows) == facts["trap_rows"],
                f"equivalence: {facts['trap_rows']} of them are trap lines")
        hist, tail = between(console, "BEGIN-HIST", "END-HIST")
        want = (build / "history_expected.txt").read_text().splitlines()
        diff = first_difference(hist, want)
        require(diff is None and tail == "END-HIST 0",
                f"history: {facts['history_traces']} recorded traces through the driver in the guest: {len(hist)} steps "
                f"equal the simulator's record, line for line" + (f" [{diff}; {tail}]" if diff or tail != "END-HIST 0" else ""))
        piped = piped_digests(console)
        require(piped == [facts["expected_sha256"], facts["history_expected_sha256"]],
                "equivalence: sha256sum of each output inside the guest (no terminal in between) is the digest of "
                "the native output")
        summary["session"] = {"rows": len(rows), "history_steps": len(hist), "piped_sha256": piped}

        # 4. Determinism: fresh processes, interpreter and JIT, and the instruction-driven clock.
        runs = [first] + [guest(work, f"a{n}", files, SESSION, SESSION_END)[0] for n in (2, 3)]
        require(not differing(runs), f"determinism, JIT off: 3 fresh processes identical: memory {first['memory'][:16]}, "
                f"console {first['console'][:16]} ({first['consoleBytes']} bytes), {first['importCalls']} import calls "
                f"({first['imports'][:16]}), {first['icount']} instructions" + f" [differ: {differing(runs)}]" * bool(differing(runs)))
        jit = [guest(work, f"j{n}", files, SESSION, SESSION_END, "--jit", 1)[0] for n in (1, 2, 3)]
        require(not differing(jit) and jit[0]["jitStats"]["compiled"] > 0,
                f"determinism, JIT on: 3 fresh processes identical: memory {jit[0]['memory'][:16]}, "
                f"{jit[0]['importCalls']} import calls, {jit[0]['jitStats']['compiled']} blocks compiled, "
                f"{jit[0]['jitStats']['refused']} refused" + f" [differ: {differing(jit)}]" * bool(differing(jit)))
        require(jit[0]["console"] == first["console"] and jit[0]["icount"] == first["icount"],
                "JIT on and off: the same console bytes and the same instruction count")
        timed = [guest(work, f"t{n}", files, SESSION, SESSION_END, "--ns-per-insn", 5)[0] for n in (1, 2)]
        require(not differing(timed) and timed[0]["memory"] != first["memory"],
                f"determinism, clock driven by instructions (5 ns each): 2 fresh processes identical: memory "
                f"{timed[0]['memory'][:16]}, {timed[0]['icount']} instructions")
        strip = lambda r: {k: v for k, v in r.items() if k != "marks"}
        summary["controlled"] = {"jit_off": strip(first), "jit_on": strip(jit[0]), "ns_per_insn_5": strip(timed[0])}

        # The controls, each removed again: the comparison must report it.
        summary["faults"] = {}
        for fault in FAULTS:
            bad = guest(work, f"f-{fault}", files, SESSION, SESSION_END, "--fault", fault)[0]
            keys = differing([first, bad]) + ["a timer left to the real clock"] * (bad["realTimers"] > 0)
            summary["faults"][fault] = keys
            require(bool(keys), f"control removed ({fault}): reported, {', '.join(keys) or 'NOTHING differs'}")

        # 7. Teeth: one byte of the input, the first digit from the middle on that the native node's
        # output depends on.
        original = (build / "vectors.txt").read_bytes()
        want = (build / "expected.txt").read_text().splitlines()
        run = lambda data: subprocess.run([str(build / "node")], input=data, capture_output=True, check=True,
                                          timeout=120).stdout.decode().splitlines()
        for at in range(len(original) // 2, len(original)):
            vectors = bytearray(original)
            if chr(vectors[at]) in "012345678":
                vectors[at] += 1
                if run(bytes(vectors)) != want:
                    break
        (work / "vectors.changed.txt").write_bytes(vectors)
        changed_files = [f if f[0] != "/node/vectors.txt" else (f[0], work / "vectors.changed.txt") for f in files]
        changed, changed_console = guest(work, "c", changed_files, SESSION, SESSION_END)
        keys = differing([first, changed])
        changed_rows, _ = between(changed_console, "BEGIN-ROWS", "END-ROWS")
        diff = first_difference(changed_rows, want)
        summary["teeth"] = {"input_byte": {"offset": at, "differ": keys, "first": diff,
                                           "lines_differing": sum(a != b for a, b in zip(changed_rows, want))}}
        require({"memory", "console", "imports"} <= set(keys) and diff is not None
                and changed_rows == run(bytes(vectors)),
                f"teeth: one byte of vectors.txt changed (offset {at}): {', '.join(keys)} change; the line comparison "
                f"reports {diff}; the guest still equals the native node on the changed rows")

        if not quick:
            # 7. Teeth: a node built from a mutant of the specification, on the same rows.
            mutant_files = [f if f[0] != "/node/node" else (f[0], build / "mutant" / "node.aarch64") for f in files]
            mutant, mutant_console = guest(work, "m", mutant_files, SESSION, SESSION_END)
            got, _ = between(mutant_console, "BEGIN-ROWS", "END-ROWS")
            diff = first_difference(got, want)
            count = sum(a != b for a, b in zip(got, want))
            theirs = (build / "mutant" / "expected.txt").read_text().splitlines()
            summary["teeth"]["mutant"] = {"name": facts["mutant"], "lines_differing": count, "first": diff,
                                          "differ": differing([first, mutant])}
            require(diff is not None and count == facts["mutant_native_lines_differing"] and got == theirs,
                    f"teeth: the mutant {facts['mutant']} in the guest is caught: {count} of {len(want)} lines differ "
                    f"from the native node ({diff}), and equal the native mutant's")

            # 6. Cost of a step.
            perf_files = [("/node/node", build / "node.aarch64"), ("/node/perf.txt", build / "perf.txt"),
                          ("/node/bench", build / "bench.aarch64"), ("/node/distinct.txt", build / "distinct.txt")]
            native = native_timing(build, facts)
            cost = summary["cost"] = {"rows": facts["perf_rows"], "bench_steps": BENCH * facts["distinct_rows"], **native}
            for label, more in (("interpreter", []), ("jit", ["--jit", 1])):
                rec, perf_console = guest(work, f"p-{label}", perf_files, PERF, PERF_END, *more)
                m = rec["marks"]
                span = lambda i, key: m[i + 1][key] - m[i][key]
                bench = re.findall(r"^bench steps .*?(?=\r?$)", perf_console.decode("utf-8", "replace"), re.M)
                require(rec["deliveredFrom"]["clock"] == 0 and piped_digests(perf_console) == [native["perf_output_sha256"]]
                        and bench == native["bench_lines"],
                        f"cost ({label}): each command ran alone; the guest's output on {facts['perf_rows']} rows has "
                        f"the native output's digest; the step loop prints the native checksums")
                cost[label] = {
                    "empty_run_insns": [span(0, "insns"), span(1, "insns")],
                    "perf_run_insns": [span(2, "insns"), span(3, "insns")],
                    "insns_per_row": (span(3, "insns") - span(1, "insns")) / facts["perf_rows"],
                    "wall_ns_per_row_first_run": (span(2, "wallNs") - span(0, "wallNs")) / facts["perf_rows"],
                    "wall_ns_per_row_second_run": (span(3, "wallNs") - span(1, "wallNs")) / facts["perf_rows"],
                    "import_calls_perf_run": span(3, "calls"),
                    "bench_insns": [span(5, "insns"), span(6, "insns"), span(7, "insns")],
                    "insns_per_step": (span(7, "insns") - span(5, "insns")) / cost["bench_steps"],
                    "wall_ns_per_step_first_run": (span(6, "wallNs") - span(5, "wallNs")) / cost["bench_steps"],
                    "wall_ns_per_step_second_run": (span(7, "wallNs") - span(5, "wallNs")) / cost["bench_steps"],
                    "session_wall_s": (m[-1]["wallNs"] - m[0]["wallNs"]) / 1e9, "session_insns": m[-1]["insns"],
                    "boot_insns": m[0]["insns"], "jit": rec["jitStats"],
                }
            i, j = cost["interpreter"], cost["jit"]
            require(i["insns_per_row"] == j["insns_per_row"] and i["insns_per_step"] == j["insns_per_step"],
                    f"cost: {i['insns_per_row']:.1f} guest instructions per row through the line executable (parse, "
                    f"step, print, flush), {i['insns_per_step']:.1f} per step alone; the same with the JIT")
            print(f"      wall per row: interpreter {i['wall_ns_per_row_second_run']:.0f} ns, JIT "
                  f"{j['wall_ns_per_row_second_run']:.0f} ns (first run {j['wall_ns_per_row_first_run']:.0f} ns); native line "
                  f"executable {native['native_line_executable_ns_per_row']:.0f} ns\n"
                  f"      wall per step alone: interpreter {i['wall_ns_per_step_second_run']:.1f} ns, JIT "
                  f"{j['wall_ns_per_step_second_run']:.1f} ns (first run {j['wall_ns_per_step_first_run']:.1f} ns); native "
                  f"{native['native_ns_per_step']:.2f} ns; through Python, native library "
                  f"{native['native_library_from_python_ns_per_step']:.0f} ns and wasmtime "
                  f"{native['wasmtime_from_python_ns_per_step']:.0f} ns")

            # 2. The other delivery path of the pinned package: a host file system over 9p on vsock.
            # Left as it is, the SDK's retry of its first connection to the guest waits on the real
            # clock, which the virtual clock outruns: the mount never happens.
            bare = [guest(work, f"u{n}", files, SESSION, SESSION_END, deliver="mount") for n in (1, 2)]
            require(b"END-ROWS 0" not in bare[0][1] and bare[0][0]["realTimers"] > 0 and bare[0][0]["vsockStats"]["connected"] == 0,
                    f"9p mount, SDK timers on the real clock: not mounted, the node does not run "
                    f"({bare[0][0]['realTimers']} timers left to the real clock, vsock {bare[0][0]['vsockStats']['refused']} "
                    f"refused, {bare[0][0]['vsockStats']['connected']} connected)")
            # With the SDK's timers on the virtual clock (guest_node.mjs --host-timers virtual).
            mounted = [guest(work, f"v{n}", files, SESSION, SESSION_END, "--host-timers", "virtual", deliver="mount") for n in (1, 2, 3)]
            recs = [m[0] for m in mounted]
            (work / "mount.console").write_bytes(mounted[0][1])
            got, tail = between(mounted[0][1], "BEGIN-ROWS", "END-ROWS")
            diff = first_difference(got, want)
            require(diff is None and tail == "END-ROWS 0"
                    and piped_digests(mounted[0][1]) == [facts["expected_sha256"], facts["history_expected_sha256"]],
                    f"9p mount, SDK timers on the virtual clock: mounted; the guest's {len(got)} lines equal the native "
                    f"executable's; both piped digests are the native ones" + f" [{diff}; {tail}]" * (diff is not None))
            vsock = {k: v for k, v in recs[0]["counts"].items() if "vsock" in k}
            require(not differing(recs) and recs[0]["realTimers"] == 0 and recs[0]["deliveredFrom"]["clock"] == 0,
                    f"9p mount, determinism, JIT off: 3 fresh processes identical: memory {recs[0]['memory'][:16]}, "
                    f"{recs[0]['importCalls']} import calls of which vsock {sum(vsock.values())}, {recs[0]['icount']} "
                    f"instructions; {recs[0]['hostTimers']['virtual']} SDK timers, {recs[0]['hostTimers']['fired']} fired"
                    + f" [differ: {differing(recs)}]" * bool(differing(recs)))
            mjit = [guest(work, f"w{n}", files, SESSION, SESSION_END, "--host-timers", "virtual", "--jit", 1, deliver="mount")[0] for n in (1, 2)]
            require(not differing(mjit) and mjit[0]["console"] == recs[0]["console"] and mjit[0]["icount"] == recs[0]["icount"],
                    f"9p mount, determinism, JIT on: 2 fresh processes identical: memory {mjit[0]['memory'][:16]}; console "
                    f"and instruction count as with the JIT off" + f" [differ: {differing(mjit)}]" * bool(differing(mjit)))
            summary["mount"] = {
                "real_timers": {"node_ran": b"END-ROWS 0" in bare[0][1], "identical": not differing([m[0] for m in bare]),
                                "realTimers": bare[0][0]["realTimers"], "vsock": bare[0][0]["vsockStats"]},
                "virtual_timers": {"jit_off": strip(recs[0]), "jit_on": strip(mjit[0]), "vsock_imports": vsock},
            }

        # The retained record.
        path = EVIDENCE / "guest_node_summary.json"
        sources = {name: sha(HERE / name) for name in ("host.mjs", "guest_node.mjs", "guest_node_build.py",
                                                       "guest_node_check.py", "guest_node_history.c")}
        summary["sources"] = sources
        if record:
            require(not quick, "--record needs the full run")
            for key in ("native_process_empty_s", "native_process_perf_s"):
                summary["cost"].pop(key)
            path.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
            head = console[:console.index(b"BEGIN-HIST\r\n")]
            (EVIDENCE / "guest_node_console.txt").write_bytes(head)
            print(f"recorded {path}")
        else:
            kept = json.loads(path.read_text())
            pinned = ("node_c_sha256", "node_aarch64_sha256", "history_aarch64_sha256", "vectors_sha256",
                      "expected_sha256", "history_sha256", "history_expected_sha256")
            require(all(kept["build"][k] == facts[k] for k in pinned),
                    "record: the generated C, the two aarch64 binaries, the rows and the expected outputs have the "
                    "recorded digests")
            if kept["environment"] != summary["environment"] or kept["sources"] != sources:
                print("skip  record: recorded in another environment or from other sources; no boot digest compared")
            else:
                pairs = [(kept["controlled"][name], summary["controlled"][name]) for name in summary["controlled"]]
                if not quick:
                    pairs += [(kept["mount"]["virtual_timers"][name], summary["mount"]["virtual_timers"][name])
                              for name in ("jit_off", "jit_on")]
                require(all(a[k] == b[k] for a, b in pairs for k in KEYS),
                        f"record: the fresh digests of the {len(pairs)} controlled sessions are the recorded ones")
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
