# Compiled benchmark comparisons

How much speed does specialization buy once the alternatives are compiled too? This benchmark runs the four small examples with the same fixed program, grammar, layout or query on each implementation. Every timed executable receives input data at runtime and returns every result. The Python domain oracle checks every output word after timing.

**Median µs per input row on an Apple M5, macOS arm64.** Nine trials of 1,024 rows each. Lower is faster.

| Workload | Specialized Shen | Specialized Maude | Direct Shen/JIT | Direct Maude native | Handwritten C |
|---|---:|---:|---:|---:|---:|
| VM checksum | 0.034 | 0.035 | 0.819 | 0.122 | 0.014 |
| Streaming decoder | 3.276 | 3.612 | 0.432 | 2.806 | 0.070 |
| Railway interlocking | 0.302 | 0.303 | 0.909 | 2.059 | 0.117 |
| Equality join | 0.100 | 0.098 | 0.390 | 1.824 | 0.088 |

The specialized VM, railway and join beat both direct language implementations on this corpus. The decoder is the exception. Its generated C clears a 65,536-word scratch bank for every row, even though the grammar caps payloads at 32 bytes. A diagnostic build changed only the clear length from 512 KiB to 256 bytes. Median time went from 3.276 to 0.078 µs/row and every output still matched. The table shows the shipped code. [Generated decoder](../examples/decoder/residual/program.c)

## What runs

* **Specialized Shen and specialized Maude.** Each original staged specification goes through its own language runtime and the shared specializer. The benchmark compiles the emitted C with Clang `-O2`. Both paths produced byte-identical C for all four fixtures. Their separate samples show ordinary measurement variation. [Specializer](../specializer/__init__.py)
* **Direct Shen.** Ordinary recursive Shen functions implement the fixed workloads. `shen-jit build` produces a sealed standalone application with the Shen/JIT runtime. Its timed region calls those functions directly. [Kernels](direct/kernels.shen) · [Shen/JIT v0.11](https://github.com/tizoc/shen-jit/tree/v0.11)
* **Direct Maude.** The independent maudec compiler translates functional Maude equations into C++20. Clang++ compiles that translation with `-O2` into a native executable. The timed process runs the compiled functions. maudec maps Maude integers to C++ `int`, so the checksum uses two 16-bit limbs to preserve unsigned 32-bit wrap. [Equations](direct/kernels.maude) · [Pinned compiler](https://github.com/fadoss/maudec/tree/5ea07b3586ebb39741fc0aac4f0e1c6ced0cd491)
* **Handwritten C.** Four independent kernels implement the same fixed workloads with integer arithmetic and arrays. They use the same timing driver and Clang flags as the specialized kernels. [Kernels](direct/kernels.c) · [Timing driver](direct/timer.c)

## Workload and timing

A row contains 32 input bytes for the VM checksum, a chunked frame with a 0–32-byte payload for the decoder, 32 events for the two-route interlocking, or two eight-row tables for the join. Decoder inputs include bad checksums, bad magic and empty chunks. Join keys can repeat. All lanes use the same seeded inputs in the same order. These workloads measure kernels; the three larger applications have separate end-to-end qualification.

The table reports median process CPU time per row. Each sample starts a fresh process, parses the full batch, warms the kernel with its first row, then times evaluation and retention of all results. C copies each input to the kernel buffer and each output into a preallocated row. Shen and translated Maude construct linked results, so allocation during evaluation is included. Output formatting, correctness checks, startup and compilation are outside the CPU interval. The receipt also records whole-process batch wall times, including startup and I/O. Lane order is shuffled between trials. The smallest differences are sensitive to timer resolution and host load. [Benchmark runner](compiled.py) · [Raw samples and corpus hashes](compiled-arm64-darwin.json)

## Reproduce the comparison

From the repository root, enter the locked Nix shell and install the pinned Maude translation tool into a disposable Python environment. The first installation needs network access and Git.

```sh
nix develop
bench_env=$(mktemp -d)
python3 -m venv "$bench_env/venv"
"$bench_env/venv/bin/python" -m pip install -r benchmarks/direct/requirements.txt
MAUDEC="$bench_env/venv/bin/maudec" python3 -m benchmarks.compiled \
  --rows 1024 --trials 9 --json /tmp/compiled-benchmark.json
```

## Earlier staged-evaluator benchmark

Inside the locked Nix shell, run the following from the repository root.

```sh
python3 -m benchmarks.benchmark --iterations 500 --json /tmp/benchmark.json
```

The [ARM64 macOS receipt](arm64-darwin.json) records all eight builds. The benchmark verifies a source answer and checks each native response against the direct Python baseline. Generation time includes source startup, specialization, C emission, Clang and linking. Binary size is the on-disk executable.

Each row uses the same input for the direct, source and native measurements. The direct baseline measures median Python call time. Native time is the median completed stdin/stdout round trip with one warmed process. Source time is CPU time per interpreted row over a warmed batch inside Shen or Maude, excluding process startup. Maude reports batch CPU in whole milliseconds. These different timing boundaries matter, particularly for small inputs. The results apply to the four benchmark fixtures on the recorded host.
