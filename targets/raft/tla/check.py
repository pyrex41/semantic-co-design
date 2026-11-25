"""TLC on the published raft.tla at a small bound, with the Figure 3 properties.

TLC is not in the locked development shell. It comes from the same locked nixpkgs:

    nix develop -c nix shell --inputs-from . 'nixpkgs#tlaplus' -c python3 -m targets.raft.tla.check

Four runs: the published specification under the bound (no property may fail), two edited
copies that TLC must reject, so that the run has teeth, and the Figure 8 edit, which this
bound is too small to expose.
"""
import hashlib
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

HERE = Path(__file__).parent
PUBLISHED = "2cf54dbedc5d81f3b108596a24644279a3bbc94cc0980108ee51590c7461cc3b"
BOUND = {"MAXTERM": 2, "MAXLOG": 1, "MAXMESSAGES": 1}
TIMEOUT = 400

# Edits of raft.tla. `expect` is the property TLC must report under BOUND; None marks an
# edit that this bound is too small to expose, which is reported and not hidden.
MUTANTS = {
    "votes-twice": {
        "old": "                 /\\ votedFor[i] \\in {Nil, j}\n",
        "new": "",
        "expect": "ElectionSafety"},
    "leader-without-quorum": {
        "old": "    /\\ votesGranted[i] \\in Quorum\n",
        "new": "",
        "expect": "ElectionSafety"},
    "commit-by-counting": {
        "old": "              IF /\\ agreeIndexes /= {}\n"
               "                 /\\ log[i][Max(agreeIndexes)].term = currentTerm[i]\n",
        "new": "              IF /\\ agreeIndexes /= {}\n",
        "expect": None},
}


def tlc(source, bound, directory, timeout=TIMEOUT):
    """Run TLC. Returns (violated property or None, states generated, distinct states,
    seconds, finished)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raft.tla").write_text(source)
    (directory / "MCraft.tla").write_text((HERE / "MCraft.tla").read_text())
    config = (HERE / "MCraft.cfg").read_text()
    for key, value in bound.items():
        config = config.replace(f"%{key}%", str(value))
    (directory / "MCraft.cfg").write_text(config)
    started = time.time()
    try:
        proc = subprocess.run(["tlc", "-workers", "2", "-metadir", str(directory / "states"),
                               "-config", "MCraft.cfg", "MCraft.tla"], cwd=directory, text=True,
                              capture_output=True, timeout=timeout)
        output, finished = proc.stdout + proc.stderr, True
    except subprocess.TimeoutExpired as late:
        output = (late.stdout or b"").decode() if isinstance(late.stdout, bytes) else (late.stdout or "")
        finished = False
    seconds = time.time() - started
    violated = re.search(r"Invariant (\w+) is violated|Action property (\w+) is violated", output)
    counts = re.findall(r"(\d+) states generated, (\d+) distinct states found", output)
    generated, distinct = map(int, counts[-1]) if counts else (0, 0)
    if finished and not violated and "Model checking completed. No error has been found." not in output:
        raise RuntimeError(f"TLC did not finish cleanly:\n{output[-3000:]}")
    name = violated and (violated.group(1) or violated.group(2))
    return name, generated, distinct, seconds, finished


def main(report=print):
    source = (HERE / "raft.tla").read_text()
    digest = hashlib.sha256((HERE / "raft.tla").read_bytes()).hexdigest()
    if digest != PUBLISHED:
        raise AssertionError("raft.tla is not the published file")
    bound = ", ".join(f"{key.lower()[3:]} {value}" for key, value in BOUND.items())
    with TemporaryDirectory() as tmp:
        violated, generated, distinct, seconds, finished = tlc(source, BOUND, Path(tmp) / "spec")
        if violated or not finished:
            raise AssertionError(f"raft.tla under the bound: violated {violated}, finished {finished}")
        report(f"TLC, published raft.tla, 3 servers, 1 value, max {bound}: {generated} states "
               f"generated, {distinct} distinct, no violation of ElectionSafety, LogMatching, "
               f"LeaderCompleteness, StateMachineSafety or LeaderAppendOnly ({seconds:.0f}s)")
        for name, mutant in MUTANTS.items():
            if source.count(mutant["old"]) != 1:
                raise AssertionError(f"mutation site of {name} not found in raft.tla")
            violated, generated, distinct, seconds, finished = tlc(
                source.replace(mutant["old"], mutant["new"]), BOUND, Path(tmp) / name)
            if mutant["expect"] and violated != mutant["expect"]:
                raise AssertionError(f"TLC does not reject {name}: violated {violated}")
            outcome = (f"violates {violated}" if violated else
                       "NOT rejected within this bound" + ("" if finished else " (timed out)"))
            report(f"  mutant {name}: {outcome} ({distinct} distinct states, {seconds:.0f}s)")
    report("raft tla: ok")


if __name__ == "__main__":
    sys.exit(main())
