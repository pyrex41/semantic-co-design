"""Build the accepted_job node for the unkernel guest and derive the rows it is fed.

    nix develop .#uk-node -c python3 experiments/unkernel_probe/guest_node_build.py OUT [--quick]

Writes into OUT:
  node.c               what tool/compile.py generates from spec.shen
  node                 the native line executable (clang, this machine): the reference
  node.wasm            the freestanding module (NODE_TARGET=wasm), for the timing comparison
  node.aarch64         the same node.c with -DNODE_MAIN as a static aarch64-linux program ($ZIG cc)
  node.aarch64.2       the same command again with an empty compiler cache (reproducibility)
  history.aarch64      guest_node_history.c, the driver of recorded histories, built the same way
  vectors.txt          the distinct (state, event) rows of the 500-seed simulation, then rows that trap
  expected.txt         the native executable's output on vectors.txt
  perf.txt             the distinct rows repeated, for the timing
  distinct.txt, bench, bench.aarch64   the distinct rows alone, and guest_node_bench.c (the step in a loop)
  history.txt          all 500 recorded traces as driver input (see guest_node_history.c)
  history_expected.txt what the driver must print: each step as the simulator recorded it
  mutant/              node.c, node, node.aarch64 and expected.txt of one mutant of the specification
  build.json           toolchain, flags, sizes and digests
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from tool import check, sim  # noqa: E402
from tool.compile import DEPTH, build  # noqa: E402
from tool.examples.accepted_job import check as job  # noqa: E402
from tool.examples.accepted_job.workload import Clients  # noqa: E402
from tool.schema import Schema  # noqa: E402

# ARMv8.0-A without extensions (the guest's emulated CPU), musl, static, no build id, no paths.
TARGET = "aarch64-linux-musl"
FLAGS = ["-std=c11", "-O2", f"-fbracket-depth={2 * DEPTH}", "-static", "-mcpu=generic", "-s",
         "-Wl,--build-id=none", "-ffile-prefix-map={dir}=."]
MUTANT = "reply-before-ack"
PERF_REPEAT = 200  # perf.txt: the distinct rows this many times over


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cross(zig, out, source, exe, defines, cache, fresh=True):
    """One static aarch64-linux executable; `cache` is the compiler's own cache directory."""
    if fresh:
        shutil.rmtree(cache, ignore_errors=True)
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(cache / "global"), ZIG_LOCAL_CACHE_DIR=str(cache / "local"))
    cmd = [zig, "cc", "-target", TARGET, *[f.format(dir=out) for f in FLAGS], *defines, str(source), "-o", str(exe)]
    subprocess.run(cmd, check=True, timeout=600, env=env, cwd=out)
    return cmd


def lines(rows):
    return "".join(" ".join(map(str, row)) + "\n" for row in rows)


def trap_rows(schema, vectors):
    """Rows no step is defined for. Each must come back as a `trap N` line, not end the program."""
    first = list(vectors[0])
    width = schema.state_width
    unknown_event = first[:width] + [999] + first[width + 1:]
    return [[],                       # no state at all: the first read is out of range
            first[:width],            # a state and no event
            first[:3],                # a truncated state
            unknown_event,            # an event tag that is no declared event
            [-1] * len(first),        # symbols that are in no table
            [2 ** 63 - 1] * len(first)]


def history(schema, native, traces):
    """Recorded traces as driver input: `s <state>` at each boot and after each crash, `e <event>`
    per step. The driver carries the state from one step to the next. The expected line of a step
    is the native node's output vector, which is asserted here to be the simulator's record of
    that step (post-state and effects)."""
    feed, expected, resets = [], [], 0
    for trace in traces:
        state = None
        for step in trace["steps"]:
            if "pre" not in step:
                continue
            pre = list(schema.encode_state(sim.freeze(step["pre"])))
            if pre != state:  # boot, or a crash lost the volatile state: the simulator says what it is now
                feed.append("s " + " ".join(map(str, pre)))
                resets += 1
            row = schema.encode_input(pre, sim.freeze(step["event"]))
            feed.append("e " + " ".join(map(str, row[schema.state_width:])))
            out = native.raw(row)
            after, effects = schema.decode_output(out)
            assert sim.freeze((schema.decode_state(after), effects)) == sim.freeze((step["post"], step["effects"])), \
                f"seed {trace['seed']} step {step['i']}: the recorded trace and the node disagree"
            expected.append(" ".join(map(str, out)))
            state = list(after)
    return "\n".join(feed) + "\n", "\n".join(expected) + "\n", resets


def main(argv):
    out = Path(argv[0]).resolve()
    quick = "--quick" in argv  # one cross build of each program: no second build, no mutant
    zig = os.environ["ZIG"]
    out.mkdir(parents=True, exist_ok=True)
    schema = Schema(ROOT / "tool" / "examples" / "accepted_job" / "node.json")
    os.environ["NODE_TARGET"] = "wasm"
    native = build(schema, out)
    os.environ.pop("NODE_TARGET")

    cache = out / "zig-cache-1"
    cmd = cross(zig, out, out / "node.c", out / "node.aarch64", ["-DNODE_MAIN"], cache)
    cross(zig, out, HERE / "guest_node_history.c", out / "history.aarch64",
          [f"-DSTATE_WIDTH={schema.state_width}", f"-DNODE_C=\"{out / 'node.c'}\""], cache, fresh=False)
    if not quick:
        cross(zig, out, out / "node.c", out / "node.aarch64.2", ["-DNODE_MAIN"], out / "zig-cache-2")
        cross(zig, out, HERE / "guest_node_bench.c", out / "bench.aarch64", [f"-DNODE_C=\"{out / 'node.c'}\""], cache, fresh=False)
        subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-O2", f"-fbracket-depth={2 * DEPTH}",
                        f"-DNODE_C=\"{out / 'node.c'}\"", str(HERE / "guest_node_bench.c"), "-o", str(out / "bench")],
                       check=True, timeout=300)

    inits = check.inits_of(schema, 1)
    traces = job.seeded(native, inits, range(job.SEEDS))
    steps = sum(len(t["steps"]) for t in traces)
    vectors = [list(schema.encode_input(schema.encode_state(state), event))
               for state, event in check.step_pairs(traces)]
    traps = trap_rows(schema, vectors)
    (out / "vectors.txt").write_text(lines(vectors + traps))
    (out / "perf.txt").write_text(lines(vectors) * PERF_REPEAT)
    (out / "distinct.txt").write_text(lines(vectors))
    run = lambda exe, text: subprocess.run([str(exe)], input=text, text=True, capture_output=True,
                                           check=True, timeout=120).stdout
    (out / "expected.txt").write_text(run(native.executable, (out / "vectors.txt").read_text()))
    got = (out / "expected.txt").read_text().splitlines()
    assert len(got) == len(vectors) + len(traps), "native: one line per row"

    feed, expected, resets = history(schema, native, traces)
    (out / "history.txt").write_text(feed)
    (out / "history_expected.txt").write_text(expected)

    mutant = {}
    if not quick:
        # The same rows through a node built from a mutant of the specification.
        source = job.mutate(schema.source, job.MUTANTS[MUTANT])
        broken = build(schema, out / "mutant", source)
        cross(zig, out / "mutant", out / "mutant" / "node.c", out / "mutant" / "node.aarch64", ["-DNODE_MAIN"], cache, fresh=False)
        text = run(broken.executable, (out / "vectors.txt").read_text())
        (out / "mutant" / "expected.txt").write_text(text)
        mutant = {"mutant": MUTANT, "mutant_node_c_sha256": sha(out / "mutant" / "node.c"),
                  "mutant_node_aarch64_sha256": sha(out / "mutant" / "node.aarch64"),
                  "mutant_expected_sha256": sha(out / "mutant" / "expected.txt"),
                  "mutant_native_lines_differing": sum(a != b for a, b in zip(text.splitlines(), got))}

    version = subprocess.run([zig, "version"], capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    facts = {
        "zig": version, "zig_path": zig, "target": TARGET,
        "command": " ".join(c.replace(str(out), "OUT").replace(zig, "$ZIG") for c in cmd),
        "node_c_sha256": sha(out / "node.c"), "node_c_lines": (out / "node.c").read_text().count("\n"),
        "node_aarch64_sha256": sha(out / "node.aarch64"), "node_aarch64_bytes": (out / "node.aarch64").stat().st_size,
        "node_aarch64_second_build_sha256": None if quick else sha(out / "node.aarch64.2"),
        "history_aarch64_sha256": sha(out / "history.aarch64"), "history_aarch64_bytes": (out / "history.aarch64").stat().st_size,
        "wasm_sha256": sha(out / "node.wasm"), "wasm_bytes": (out / "node.wasm").stat().st_size,
        "seeds": job.SEEDS, "simulated_steps": steps, "distinct_rows": len(vectors), "trap_rows": len(traps),
        "rows": len(vectors) + len(traps),
        "native_trap_lines": sum(line.startswith("trap") for line in got),
        "native_trap_lines_among_distinct": sum(line.startswith("trap") for line in got[:len(vectors)]),
        "vectors_sha256": sha(out / "vectors.txt"), "expected_sha256": sha(out / "expected.txt"),
        "perf_rows": len(vectors) * PERF_REPEAT, "perf_sha256": sha(out / "perf.txt"),
        "history_traces": len(traces), "history_steps": expected.count("\n"), "history_state_lines": resets,
        "history_traces_digest": hashlib.sha256("".join(sim.digest(t) for t in traces).encode()).hexdigest(),
        "history_sha256": sha(out / "history.txt"), "history_expected_sha256": sha(out / "history_expected.txt"),
        **mutant,
    }
    (out / "build.json").write_text(json.dumps(facts, indent=1) + "\n")
    print(json.dumps(facts, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
