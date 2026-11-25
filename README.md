# Semantic co-design

This project turns executable specifications into running software. Write the rules once, run them in an interpreter to see what they do, then compile those same rules into the program you ship. The technique is **partial evaluation**. The compiler does the work it can when it knows the rules and leaves the rest for runtime.

Take a bytecode interpreter. Give the compiler the interpreter and a fixed program that sums an array. The compiler walks the bytecode at build time. The executable it produces is a loop that reads numbers and adds them. Give it `5 8 13` and it prints `26`. Change the bytecode to compute a maximum and rebuild, and the same input yields `13`. You can read the [VM specification](examples/vm/README.md) alongside the [generated C](examples/vm/residual/program.c).

## What can you build this way?

The repo includes three locally runnable applications. Their rules are written in App Shen. A shared compiler turns those rules into native extensions, and Python hosts connect them to databases, sockets and other services. The same inputs can be replayed against the interpreter and the built package. [Application guide](example/README.md) · [Language and compiler contract](example/LANGUAGE.md)

| Application | What the specification decides | How it is checked |
|---|---|---|
| [Document Desk](example/webapp/README.md) | Who can review a document, how many votes close it, and which notification to queue. | A browser drives approvals against PostgreSQL. Killing the server on either side of commit checks whether a retry creates a second notification intent. |
| [Spot trading engine](example/trading-engine/README.md) | When to admit or cancel an order, how much balance to reserve, and how a fill changes the ledger. | A simulated exchange delays and repeats results. The checker compares balances, orders and journal postings after each explored transition. |
| [Router and load balancer](example/router-nlb/README.md) | Where to forward a packet, which backend gets a new connection, and when a standby can take over its NAT binding. | Virtual Ethernet ports carry actual packet bytes. An independent GoBGP peer exercises routing, and restart tests check who owns each live tuple. |

Agreement between two executions can hide a shared bug. An independent accounting rule exposed one here. The engine could report a $100 balance while its journal recorded $101 after an adjustment arrived before the opening statement. The engine now rejects that sequence. [Executable ledger laws](example/trading-engine/invariants.shen) · [Regression and generated histories](example/trading-engine/check_reachable.py)

The [patterns catalog](experiments/patterns/README.md) follows the same question through the Gang of Four, SICP and Norvig’s PAIP: what can a library decide once, and what must it still do on every call? Start with the [three-function C client](experiments/patterns/client.c), or go directly to the book you want. The [first-projection guide](experiments/patterns/projections/README.md) shows how to run and edit all 75 pattern programs in Shen and Maude, then inspect the native functions produced by each specializer.

## Try it

With Nix installed and flakes enabled, build the sum program and inspect what the compiler kept. The locked shell supplies Python, Clang, SQLite, Shen JIT and Maude.

```sh
git clone https://github.com/pyrex41/semantic-co-design.git
cd semantic-co-design
nix develop
demo=$(mktemp -d)
python3 -m specializer build vm --source shen --static examples/vm/sum.json \
  -o "$demo/sum" --emit-ir "$demo/sum.json" --emit-c "$demo/sum.c"
printf '5 8 13\n' | "$demo/sum"
# 26
```

## Four explicitly staged specifications

Switch `--source` to `maude` to reduce the Maude equations for the same static fixture. Each native process reads one integer vector per line and emits one result vector per line. The example READMEs describe the domain-specific encodings.

| Example and author-supplied semantics | Fixed and eliminated during specialization | Dynamic input | Shared compiler work exercised | Replay |
|---|---|---|---|---|
| [VM](examples/vm/README.md) — stack-machine operations | Program and opcode dispatch | Array length and 32-bit values | Initial loop, indexed reads and modular arithmetic | `python3 -m examples.vm.check` |
| [Decoder](examples/decoder/README.md) — frame grammar and incremental transitions | Byte order, accepted kinds, length and checksum rules | Bytes and chunk boundaries | Indexed scratch writes and variable-length output | `python3 -m examples.decoder.check` |
| [Railway](examples/railway/README.md) — reservations, entry and clearance | Two-route layout | Serialized route events | Existing conditionals and scalar slots | `python3 -m examples.railway.check` |
| [Queries](examples/query/README.md) — relational operators | Schema offsets and query tree | Rows, table sizes and cutoff | Nested loops and exact signed 64-bit concrete arithmetic | `python3 -m examples.query.check` |

The [shared interface](specializer/FORMAT.md) uses scalar expressions, structured control flow, indexed storage and output. To add an example, supply the manifest and the two source files in its directory. The fifth-example check builds a temporary, unregistered specification through both language runtimes.

The [ordinary Shen interpreter experiment](experiments/ordinary_vm/README.md) derives the sum loop from recursive list processing without explicit staging primitives.

Each example retains its representative IR and C in `residual/`. The artifact check regenerates both files from each language and checks the surviving loops. The decoder has loops over chunks, bytes, and payload output. The join has a loop per input relation. The shared code has no domain-specific dispatch. The [compiled benchmark](benchmarks/README.md) compares the generated programs with standalone Shen JIT binaries, Maude compiled through C++, and handwritten C on identical input rows.

## Run a durable job from two different specs

Build the same job from each executable definition and run them against a fresh database:

```sh
demo=$(mktemp -d)
python3 logic/build.py logic/accepted-job.shen --compile "$demo/job-shen"
python3 logic/build.py logic/accepted-job.maude --compile "$demo/job-maude"
"$demo/job-shen" "$demo/jobs.sqlite" 17 submit-a
"$demo/job-maude" "$demo/jobs.sqlite" 17 submit-b
```

The first command to the job prints `pending-a receipt-a`. The second finds job 17 after restart and prints `pending-a conflict`. The binaries use the host platform and dynamically link SQLite. Python and the language runtimes are build-time dependencies.

The transition rules are written in [Shen](logic/accepted-job.shen) and [Maude](logic/accepted-job.maude).

Edit the transition in either language and rerun its build command. The default cancellation policy withdraws pending work. Pass `--policy keep` before `--compile` to keep the obligation after a cancellation. This example specializes all seven states and four events, with two digest symbols and a runtime uint32 job key.

Run `nix flake check` to compile both language specs and check all 112 native transitions across the two policies. The check also changes a rule in each source and verifies the resulting native behavior. A second replay checks the [JSON frontend's typed, durable job](native/accepted-job.json) with full runtime SHA-256 digests.

## Project documents

- [Goal and evidence](GOAL.md) records what the prototype establishes and which claims remain open.
- [Research plan](RESEARCH.md) explains the specialization, correspondence and learning questions.
- [Experiments and reproduction commands](EXPERIMENTS.md) covers retry witnesses, independent Fenrir and Capcov controls, performance measurements and the learning pilot.
