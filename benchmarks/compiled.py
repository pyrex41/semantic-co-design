"""Compare compiled implementations on identical, runtime-supplied benchmark rows."""
import argparse
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import tempfile
import time

from examples.decoder.decoder import decode, frame
from examples.query.query import oracle
from examples.railway.railway import replay
from examples.vm.oracle import run
from specializer import build

HERE = Path(__file__).resolve().parent
DIRECT = HERE / "direct"
FIXTURES = {"vm": "checksum", "decoder": "grammar", "railway": "layout", "query": "join"}


def corpus(name, fixed, size, rng):
    rows = []
    for i in range(size):
        if name == "vm":
            row = [rng.randrange(256) for _ in range(32)]
        elif name == "railway":
            row = [rng.randrange(8) for _ in range(32)]
        elif name == "query":
            row = [2, 8, 8]
            for _ in range(16):
                row.extend([rng.randrange(8), rng.randrange(100)])
        else:
            packet = frame(fixed, 1 + i % 2, [rng.randrange(256) for _ in range(i % 33)])
            if i % 8 == 1:
                packet[-1] ^= 1
            elif i % 8 == 2:
                packet[0] = 0
            row = [0]
            while packet:
                n = min(len(packet), rng.randint(1, 8))
                row.extend([n, *packet[:n]])
                packet = packet[n:]
        rows.append(row)
    return rows


def execute(cmd, data=None):
    result = subprocess.run(cmd, input=data, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"{cmd}: {result.stderr[-1500:]} {result.stdout[-500:]}")
    return result.stdout


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def inputs(name, rows, answers):
    text = [str(len(rows))]
    for row, answer in zip(rows, answers):
        text.append(" ".join(map(str, [len(row), len(answer), *row])))
    wire = "\n".join(text) + "\n"
    return wire, str(list(FIXTURES).index(name)) + "\n" + wire


def sample(binary, data, expected, shen=False):
    start = time.perf_counter_ns()
    lines = execute([binary], data).splitlines()
    wall_ms = (time.perf_counter_ns() - start) / 1e6
    elapsed = float(lines[0])
    if shen:
        if not all(re.fullmatch(r"\[[\d\s\-]*\]", line) for line in lines[1:]):
            raise ValueError(f"invalid Shen output: {str(lines)[:180]}")
        actual = [list(map(int, line[1:-1].split())) for line in lines[1:]]
    else:
        actual = [list(map(int, line.split())) for line in lines[1:]]
    if actual != expected:
        index = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b), min(len(actual), len(expected)))
        raise ValueError(f"{binary} row {index}: got {actual[index:index+1]}, expected {expected[index:index+1]}")
    return {"cpu_us_per_row": elapsed * 1e6 / len(expected), "batch_wall_ms": wall_ms}


def compare(out, size, trials, seed):
    jit = os.getenv("SHEN_JIT", "shen-jit")
    maudec = os.getenv("MAUDEC", "maudec")
    cc, cxx = os.getenv("CLANG", "clang"), os.getenv("CXX", "clang++")
    shen = out / "shen"
    execute([jit, "build", DIRECT / "bench.shenmod", "-o", shen])
    translated = out / "maude.cpp"
    translated.write_text(execute([maudec, DIRECT / "kernels.maude", "-t", "cpp"]))
    direct_sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted(DIRECT.iterdir()) if p.is_file()}
    report = {"platform": platform.platform(), "machine": platform.machine(),
              "cpu": execute(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
                     if platform.system() == "Darwin" else platform.processor(),
              "source_sha256": direct_sources, "rows_per_trial": size,
              "trials": trials, "seed": seed, "clang": execute([cc, "--version"]).splitlines()[0],
              "clangxx": execute([cxx, "--version"]).splitlines()[0],
              "shen_jit": execute([jit, "--version"]).strip(),
              "maude": execute([os.getenv("MAUDE", "maude"), "--version"]).strip(),
              "maudec_cpp_sha256": hashlib.sha256(translated.read_bytes()).hexdigest(), "fixtures": {}}
    for name, fixture in FIXTURES.items():
        fixed = json.loads((HERE.parent / "examples" / name / f"{fixture}.json").read_text())
        rows = corpus(name, fixed, size, random.Random(f"{seed}-{name}"))
        oracle_fn = {"vm": partial(run, fixed), "decoder": partial(decode, fixed),
                     "railway": partial(replay, layout=fixed), "query": partial(oracle, fixed)}[name]
        expected = [oracle_fn(row) for row in rows]
        wire, shen_wire = inputs(name, rows, expected)
        binaries = {"shen_jit": shen}
        generated = []
        for lane in ("specialized_shen", "specialized_maude", "direct_c", "direct_maude"):
            directory = out / name / lane
            directory.mkdir(parents=True)
            source = DIRECT / "kernels.c"
            if lane.startswith("specialized"):
                source = directory / "program.c"
                build(name, lane.split("_")[1], fixed, directory / "cli", emit_c=source)
                generated.append(source.read_bytes())
            cpp = lane == "direct_maude"
            if cpp:
                source = translated
            binary = directory / "timed"
            execute([cxx if cpp else cc, "-std=c++20" if cpp else "-std=c11", "-O2",
                     f'-DKERNEL="{source}"', f"-DBENCH_{name.upper()}",
                     DIRECT / ("maude_timer.cpp" if cpp else "timer.c"), "-o", binary])
            binaries[lane] = binary
        if name == "decoder":
            # Isolate the cost of clearing the full 65,536-word bank for this
            # fixed grammar. Keep its allocation, access bounds and code unchanged.
            old = "memset(bank_0, 0, sizeof bank_0);"
            code = generated[0].decode()
            if code.count(old) != 1 or fixed[3] != 32:
                raise ValueError("decoder reset probe needs review for this grammar/backend")
            probe = out / name / "reset-probe.c"
            probe.write_text(code.replace(old, "memset(bank_0, 0, 32 * sizeof bank_0[0]);"))
            binary = out / name / "reset-probe"
            execute([cc, "-std=c11", "-O2", f'-DKERNEL="{probe}"', DIRECT / "timer.c", "-o", binary])
            binaries["decoder_reset_probe"] = binary
        samples = {lane: [] for lane in binaries}
        order_rng = random.Random(seed)
        for _ in range(trials):
            order = list(binaries)
            order_rng.shuffle(order)
            for lane in order:
                is_shen = lane == "shen_jit"
                samples[lane].append(sample(binaries[lane], shen_wire if is_shen else wire, expected, is_shen))
        report["fixtures"][name] = {"fixture": fixture, "input_sha256": digest(rows), "output_sha256": digest(expected),
            "specialized_c_identical": generated[0] == generated[1],
            "median_cpu_us_per_row": {k: statistics.median(s["cpu_us_per_row"] for s in v) for k,v in samples.items()},
            "samples": samples}
        print(name, report["fixtures"][name]["median_cpu_us_per_row"], flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=512)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--seed", type=int, default=5145)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.rows <= 4096 or not 1 <= args.trials <= 50:
        parser.error("rows must be in 1..4096 and trials in 1..50")
    with tempfile.TemporaryDirectory() as work:
        report = compare(Path(work), args.rows, args.trials, args.seed)
    args.json.write_text(json.dumps(report, indent=2) + "\n")
