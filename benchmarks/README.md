# Build cost and warm execution

Inside the locked Nix shell, run the following from the repository root.

```sh
python3 -m benchmarks.benchmark --iterations 500 --json /tmp/benchmark.json
```

The [ARM64 macOS receipt](arm64-darwin.json) records all eight builds. The benchmark verifies a source answer and checks each native response against the direct Python baseline. Generation time includes source startup, specialization, C emission, Clang and linking. Binary size is the on-disk executable.

Each row uses the same input for the direct, source and native measurements. The direct baseline measures median Python call time. Native time is the median completed stdin/stdout round trip with one warmed process. Source time is CPU time per interpreted row over a warmed batch inside Shen or Maude, excluding process startup. Maude reports batch CPU in whole milliseconds. These different timing boundaries matter, particularly for small inputs. The results apply to the four benchmark fixtures on the recorded host.
