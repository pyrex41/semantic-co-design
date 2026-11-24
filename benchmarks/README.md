# Benchmark command

Run the bounded benchmark from the repository root with

```sh
python3 -m benchmarks.benchmark --iterations 20 --json benchmarks/latest.json
```

Each row reports a warmed source-interpreter median, a direct baseline median,
and a native median when that example publishes `native-replay.py`. Generation
time and binary size belong to the native build receipt emitted by
`python3 -m specializer build`; this command intentionally measures execution
only. Missing optional Shen or Maude installations do not invalidate source
measurements. Results include the command, iteration count, units, and a UTC
timestamp so they can be compared across machines.
