"""Measure warm per-row execution and end-to-end native build cost."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import tempfile
import time

from examples.decoder.decoder import decode, frame
from examples.query.query import oracle
from examples.railway.railway import replay
from examples.vm.oracle import run
from specializer import ROOT, _term, build, concrete

EXAMPLES = ROOT.parent / "examples"


def cases():
    for example, fixture in (("vm","checksum"), ("decoder","grammar"),
                             ("railway","layout"), ("query","join")):
        fixed = json.loads((EXAMPLES / example / f"{fixture}.json").read_text())
        if example == "vm":
            row, direct = list(range(32)), run
        elif example == "decoder":
            packet = frame(fixed,1,list(range(12)))
            row, direct = [4,*packet[:4],len(packet)-4,*packet[4:]], decode
        elif example == "railway":
            row, direct = [0,2,1,4,3,4,6,7], lambda static, data: replay(data,layout=static)
        else:
            row, direct = [2,3,2,1,2,1,4,2,5,1,8,1,9], oracle
        yield example, fixture, fixed, row, lambda: direct(fixed,row)


def median_us(fn, repeats):
    fn()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns()-start)/1000)
    return round(statistics.median(samples),3)


def warm_source(example, source, fixed, row, repeats, work):
    spec = EXAMPLES / example / f"spec.{source}"
    fixed, row = _term(fixed,source), _term(row,source)
    if source == "shen":
        helper = work / "timing.shen"
        helper.write_text("""(define bench.repeat
  0 _ _ -> 0
  N P D -> (do (stage-eval P D) (bench.repeat (- N 1) P D)))
""")
        cmd = [os.environ.get("SHEN_JIT","shen-jit"),"eval","-l",ROOT/"stage.shen",
               "-l",spec,"-l",helper,"-e",f"(set *p* (specialize {fixed}))",
               "-e",f"(stage-eval (value *p*) {row})","-e",
               f"(let Start (get-time run) Done (bench.repeat {repeats} (value *p*) {row}) (- (get-time run) Start))"]
        result = subprocess.run(cmd,text=True,capture_output=True,check=True,timeout=60)
        return round(1e6*float(result.stdout.strip().splitlines()[-1])/repeats,3)
    # Maude's term argument is normalized once before each timed batch.
    helper = work / "timing.maude"
    helper.write_text("""fmod TIMING is protecting SPEC .
      op timed : Nat T T -> Nat . op outputSize : T -> Nat .
      var N : Nat . vars P D : T . var V : Ts .
      eq outputSize(box(V)) = sizeOf(V) .
      eq timed(0,P,D) = 0 .
      eq timed(s N,P,D) = outputSize(run(P,D)) + timed(N,P,D) .
    endfm
""")
    cmd = [os.environ.get("MAUDE","maude"),"-no-banner","-no-advise","-batch",
           ROOT/"stage.maude",spec,helper]
    request = f"red timed(1,specialize({fixed}),{row}) .\nred timed({repeats},specialize({fixed}),{row}) .\nquit\n"
    result = subprocess.run(cmd,input=request,text=True,capture_output=True,check=True,timeout=60)
    if result.stderr or len(re.findall(r"^result (?:Zero|NzNat|Nat): \d+$",result.stdout,re.M)) != 2:
        raise ValueError(result.stderr + result.stdout[-1500:])
    cpu_ms = int(re.findall(r"rewrites: \d+ in (\d+)ms cpu",result.stdout)[-1])
    return round(1000*cpu_ms/repeats,3)


def benchmark(repeats):
    results = []
    for name,fixture,fixed,row,direct in cases():
        expected = direct()
        baseline = median_us(direct,repeats)
        for source in ("shen","maude"):
            with tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                binary = work / "native"
                assert concrete(name,source,fixed,[row]) == [expected]
                begin = time.perf_counter_ns()
                build(name,source,fixed,binary)
                cost = (time.perf_counter_ns()-begin)/1e6
                encoded = (" ".join(map(str,row))+"\n").encode()
                answer = (" ".join(map(str,expected))+"\n").encode()
                with subprocess.Popen([binary],stdin=subprocess.PIPE,stdout=subprocess.PIPE) as proc:
                    def roundtrip():
                        proc.stdin.write(encoded)
                        proc.stdin.flush()
                        assert proc.stdout.readline() == answer
                    native = median_us(roundtrip,repeats)
                    proc.stdin.close()
                    assert proc.wait(timeout=5) == 0
                results.append(dict(example=name,fixture=fixture,source=source,
                                    generation_ms=round(cost,3),binary_bytes=binary.stat().st_size,
                                    native_roundtrip_us=native,python_direct_us=baseline,
                                    source_cpu_us=warm_source(name,source,fixed,row,repeats,work)))
    return dict(host=platform.platform(),iterations=repeats,results=results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations",type=int,default=200)
    parser.add_argument("--json",type=Path)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 10000:
        parser.error("iterations must be in 1..10000")
    receipt = json.dumps(benchmark(args.iterations),indent=2)+"\n"
    if args.json:
        args.json.write_text(receipt)
    else:
        print(receipt,end="")
