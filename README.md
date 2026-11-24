# Semantic co-design

Write executable behavior in [Shen](logic/accepted-job.shen) or [Maude](logic/accepted-job.maude), then specialize it into a native executable. The original AcceptedJob example accepts jobs, replays receipts, and handles cancellation. Each invocation loads one job from SQLite and commits its new state before reporting the result.

## Build and run

Start with Nix installed and flakes enabled. From a fresh checkout:

```sh
git clone https://github.com/pyrex41/semantic-co-design.git
cd semantic-co-design
nix develop
```

The locked shell supplies Python, Clang, SQLite, [tizoc/shen-jit v0.11](https://github.com/tizoc/shen-jit/releases/tag/v0.11) and Maude. Build the same job from each executable definition and run them against a fresh database:

```sh
demo=$(mktemp -d)
python3 logic/build.py logic/accepted-job.shen --compile "$demo/job-shen"
python3 logic/build.py logic/accepted-job.maude --compile "$demo/job-maude"
"$demo/job-shen" "$demo/jobs.sqlite" 17 submit-a
"$demo/job-maude" "$demo/jobs.sqlite" 17 submit-b
```

The first command to the job prints `pending-a receipt-a`. The second finds job 17 after restart and prints `pending-a conflict`. The binaries use the host platform and dynamically link SQLite. Python and the language runtimes are build-time dependencies.

## Four explicitly staged specifications

Inside `nix develop`, build a native executable from either source. For example, this prints the sum of three runtime inputs and retains both intermediate artifacts.

```sh
python3 -m specializer build vm --source shen --static examples/vm/sum.json \
  -o /tmp/vm-sum --emit-ir /tmp/vm-sum.json --emit-c /tmp/vm-sum.c
printf '5 8 13\n' | /tmp/vm-sum
```

Switch `--source` to `maude` to reduce the Maude equations for the same static fixture. Each native process reads one integer vector per line and emits one result vector per line. The example READMEs describe the domain-specific encodings.

| Example and author-supplied semantics | Fixed and eliminated during specialization | Dynamic input | Shared compiler work exercised | Replay |
|---|---|---|---|---|
| [VM](examples/vm/README.md) — stack-machine operations | Program and opcode dispatch | Array length and 32-bit values | Initial loop, indexed reads and modular arithmetic | `python3 -m examples.vm.check` |
| [Decoder](examples/decoder/README.md) — frame grammar and incremental transitions | Byte order, accepted kinds, length and checksum rules | Bytes and chunk boundaries | Indexed scratch writes and variable-length output | `python3 -m examples.decoder.check` |
| [Railway](examples/railway/README.md) — reservations, entry and clearance | Two-route layout | Serialized route events | Existing conditionals and scalar slots | `python3 -m examples.railway.check` |
| [Queries](examples/query/README.md) — relational operators | Schema offsets and query tree | Rows, table sizes and cutoff | Nested loops and exact signed 64-bit concrete arithmetic | `python3 -m examples.query.check` |

The [shared interface](specializer/FORMAT.md) uses scalar expressions, structured control flow, indexed storage and output. To add an example, supply the manifest and the two source files in its directory. The fifth-example check builds a temporary, unregistered specification through both language runtimes.

Each example retains its representative IR and C in `residual/`. The artifact check regenerates both files from each language and checks the surviving loops. The decoder has loops over chunks, bytes, and payload output. The join has a loop per input relation. The shared code has no domain-specific dispatch. For measured generation cost, binary sizes and warm timings, see the [benchmark procedure and receipt](benchmarks/README.md).

## Change the AcceptedJob behavior

Edit the transition in either language and rerun its build command. The default cancellation policy withdraws pending work. Pass `--policy keep` before `--compile` to keep the obligation after a cancellation. This example specializes all seven states and four events, with two digest symbols and a runtime uint32 job key.

Run `nix flake check` to compile both language specs and check all 112 native transitions across the two policies. The check also changes a rule in each source and verifies the resulting native behavior. A second replay checks the [JSON frontend's typed, durable job](native/accepted-job.json) with full runtime SHA-256 digests.

## Project documents

- [Goal and evidence](GOAL.md) records what the prototype establishes and which claims remain open.
- [Research plan](RESEARCH.md) explains the specialization, correspondence and learning questions.
- [Experiments and reproduction commands](EXPERIMENTS.md) covers retry witnesses, independent Fenrir and Capcov controls, performance measurements and the learning pilot.
