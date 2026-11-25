"""Step time of the specialized native engine against the same engine run by shen-jit.

    nix develop -c python3 -m targets.workflow.bench [--json FILE]

Every lane handles the same steps: every (state, event) a node handled, in order, in the
seeded runs of the retained check. The native lane calls node_step on the recorded input
vectors (bench.c). The two Shen lanes are engine.shen plus the definition, unchanged,
applied to the same states and events as Shen lists: once loaded by `shen-jit eval`, which
is how the checks run the reference, and once built by `shen-jit build` into a sealed
executable. Each lane warms up with one pass, then times whole passes in process CPU time.
Every lane reports how many effects the steps emitted, and the counts must agree.

shen-jit is a JIT compiler, so "the Shen interpreter" here means the workflow engine
interpreting the definition, itself running as machine code.

What this does not measure: an unspecialized native engine. The compiler refuses to build
one (the check shows the refusal), so a difference between the lanes is specialization and
the two code generators together.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory

from tool import shen, sim
from tool.check import require
from tool.compile import DEPTH, build

from .check import SEEDS, seeded
from .model import HERE, Definition

TRIALS = 11
PASSES = 50
NATIVE = "native, specialized"

DRIVER = """
(define wfbench.pass
  [] Effects -> Effects
  [[State Event] | Steps] Effects ->
    (wfbench.pass Steps (+ Effects (wfbench.count (NAME.step State Event)))))

(define wfbench.count
  [_ Effects] -> (length Effects))

(define wfbench.passes
  0 _ Effects -> Effects
  N Steps Effects -> (wfbench.passes (- N 1) Steps (wfbench.pass Steps Effects)))

(define wfbench.trials
  0 _ _ -> done
  T Passes Steps ->
    (let Start (get-time run)
         Effects (wfbench.passes Passes Steps 0)
         Elapsed (- (get-time run) Start)
      (do (output "@@ ~A ~A~%" Elapsed Effects)
          (wfbench.trials (- T 1) Passes Steps))))

(define wfbench.run
  Trials Passes Steps -> (let Warm (wfbench.pass Steps 0) (wfbench.trials Trials Passes Steps)))
"""


def run(command, text=""):
    result = subprocess.run([str(c) for c in command], input=text, text=True,
                            capture_output=True, timeout=120)
    lines = [line.split()[-2:] for line in result.stdout.splitlines() if line.startswith("@@")]
    if result.returncode or len(lines) != TRIALS:
        raise RuntimeError(f"{command[0]}: {result.stderr[-1500:]} {result.stdout[-500:]}")
    return [(float(seconds), int(effects)) for seconds, effects in lines]


def summary(samples):
    ordered = sorted(samples)
    quarter = len(ordered) // 4
    return {"median": statistics.median(ordered), "min": ordered[0], "max": ordered[-1],
            "q1": ordered[quarter], "q3": ordered[-1 - quarter], "samples": samples}


def measure(name, work):
    definition = Definition(name, work / name)
    native = build(definition.schema, work / name / "build")
    schema = definition.schema
    traces = seeded(native, definition, range(SEEDS))
    steps = [(step["pre"], step["event"]) for trace in traces for step in trace["steps"]
             if "pre" in step]
    vectors = [schema.encode_input(schema.encode_state(state), event) for state, event in steps]
    effects = sum(len(step["effects"]) for trace in traces for step in trace["steps"]
                  if "pre" in step)

    clang = [os.environ.get("CLANG", "clang"), "-std=c11", "-O2", "-Wno-tautological-compare",
             f"-fbracket-depth={2 * DEPTH}"]
    timer = work / name / "bench"
    subprocess.run([*clang, str(work / name / "build" / "node.c"), str(HERE / "bench.c"),
                    "-o", str(timer)], check=True, timeout=120)
    rows = f"{len(vectors)}\n" + "".join(" ".join(map(str, row)) + "\n" for row in vectors)
    lanes = {NATIVE: run([timer, schema.input_width, schema.output_capacity,
                          schema.state_width, TRIALS, PASSES], rows)}

    module = work / name / "wfbench"
    module.mkdir()
    jit = os.environ.get("SHEN_JIT", "shen-jit")
    (module / "spec.shen").write_text(definition.text)
    (module / "driver.shen").write_text(DRIVER.replace("NAME", name))
    (module / "loaded.shen").write_text(
        f"(set wfbench.steps {shen.term(steps)})\n"
        f"(wfbench.run {TRIALS} {PASSES} (value wfbench.steps))\n")
    lanes["shen-jit eval, interpreting"] = run(
        [jit, "eval", "-l", module / "spec.shen", "-l", module / "driver.shen",
         "-l", module / "loaded.shen"])
    (module / "sealed.shen").write_text(
        f"(define wfbench.steps -> {shen.term(steps)})\n"
        f"(define wfbench.main -> (wfbench.run {TRIALS} {PASSES} (wfbench.steps)))\n")
    (module / "wfbench.shenmod").write_text(
        '(shen.module (version 1) (name wfbench) (main wfbench.main) '
        '(sources tc- "spec.shen" "driver.shen" "sealed.shen"))\n')
    subprocess.run([jit, "build", str(module / "wfbench.shenmod"), "-o", str(module / "wfbench")],
                   check=True, timeout=120, capture_output=True)
    lanes["shen-jit build, interpreting"] = run([module / "wfbench"])

    for lane, samples in lanes.items():
        require(all(count == effects * PASSES for _, count in samples),
                f"{name}: lane '{lane}' did not emit the effects the traces recorded")
    return {"steps": len(steps), "effects": effects, "stats": native.stats,
            "trace_digest": hashlib.sha256("".join(sim.digest(t) for t in traces).encode()
                                           ).hexdigest()[:16],
            "ns_per_step": {lane: summary([seconds / (PASSES * len(steps)) * 1e9
                                           for seconds, _ in samples])
                            for lane, samples in lanes.items()}}


def main(argv):
    results = {}
    with TemporaryDirectory() as tmp:
        for name in ("order", "docs"):
            results[name] = measure(name, Path(tmp))
    sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    receipt = {
        "machine": {"platform": platform.platform(), "processor": platform.processor(),
                    "python": platform.python_version()},
        "sources": {"engine.shen": sha(HERE / "engine.shen"),
                    **{f"tool/{file}": sha(HERE.parent.parent / "tool" / file)
                       for file in ("pe.py", "compile.py", "abi.c.in")}},
        "trials": TRIALS, "passes": PASSES, "results": results}
    for name, r in results.items():
        print(f"{name}: {r['steps']} steps of {SEEDS} seeded runs, {TRIALS} trials of {PASSES} "
              f"passes per lane; ns per step")
        fast = r["ns_per_step"][NATIVE]
        for lane, s in r["ns_per_step"].items():
            print(f"  {lane:30} median {s['median']:8.1f}   quartiles {s['q1']:.1f} to "
                  f"{s['q3']:.1f}   range {s['min']:.1f} to {s['max']:.1f}   "
                  f"{s['median'] / fast['median']:.2f}x native")
    if len(argv) == 2 and argv[0] == "--json":
        Path(argv[1]).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
