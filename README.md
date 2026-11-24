# Semantic co-design

Write executable behavior in [Shen](logic/accepted-job.shen) or [Maude](logic/accepted-job.maude), then specialize it into a native executable. The example accepts jobs, replays receipts, and handles cancellation. Each invocation loads one job from SQLite and commits its new state before reporting the result.

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

## Change the behavior

Edit the transition in either language and rerun its build command. The default cancellation policy withdraws pending work. Pass `--policy keep` before `--compile` to keep the obligation after a cancellation. This example specializes all seven states and four events, with two digest symbols and a runtime uint32 job key.

Run `nix flake check` to compile both language specs and check all 112 native transitions across the two policies. The check also changes a rule in each source and verifies the resulting native behavior. A second replay checks the [JSON frontend's typed, durable job](native/accepted-job.json) with full runtime SHA-256 digests.

## Project documents

- [Goal and evidence](GOAL.md) records what the prototype establishes and which claims remain open.
- [Research plan](RESEARCH.md) explains the specialization, correspondence and learning questions.
- [Experiments and reproduction commands](EXPERIMENTS.md) covers retry witnesses, independent Fenrir and Capcov controls, performance measurements and the learning pilot.
