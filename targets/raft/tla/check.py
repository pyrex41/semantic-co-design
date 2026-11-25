"""TLC on the published raft.tla at small bounds, with the Figure 3 properties.

TLC is not in the locked development shell. It comes from the same locked nixpkgs:

    nix develop -c nix shell --inputs-from . 'nixpkgs#tlaplus' -c python3 -m targets.raft.tla.check

What it runs, and what each run can and cannot show:

1. The published specification under the bound MAIN (two values, terms up to 2, one
   entry, one message in flight), every state, with the five properties. One further
   invariant is checked in the same run: at this bound at most one election ever happens.
   So LeaderCompleteness cannot fail in this run. The other four can: see 3.
2. Two edits that TLC rejects at that bound with all five properties checked, both by
   ElectionSafety; and the Figure 8 edit at the same bound with one value, which no bound
   this small exposes.
3. For each of LeaderAppendOnly, LogMatching and StateMachineSafety, an edit that TLC
   rejects at the bound MAIN by that property when it alone is checked. For
   LeaderCompleteness the same at a bound with a second election (terms up to 3, one
   value), at which the published specification is NOT explored to the end:
   `python3 -m targets.raft.tla.check deeper` runs it there for a bounded time and says
   whether TLC finished.
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
MAIN = {"MAXTERM": 2, "MAXLOG": 1, "MAXMESSAGES": 1, "VALUES": "v1, v2"}
ONE_VALUE = {**MAIN, "VALUES": "v1"}
TWO_ELECTIONS = {**ONE_VALUE, "MAXTERM": 3}
INVARIANTS = ["ElectionSafety", "LogMatching", "LeaderCompleteness", "StateMachineSafety"]
ACTION = "LeaderAppendOnly"
VACUITY = "AtMostOneElection"
TIMEOUT = 400

# Edits of raft.tla.
EDITS = {
    "votes-twice": (
        "                 /\\ votedFor[i] \\in {Nil, j}\n",
        ""),
    "leader-without-quorum": (
        "    /\\ votesGranted[i] \\in Quorum\n",
        ""),
    "commit-by-counting": (
        "              IF /\\ agreeIndexes /= {}\n"
        "                 /\\ log[i][Max(agreeIndexes)].term = currentTerm[i]\n",
        "              IF /\\ agreeIndexes /= {}\n"),
    "leader-overwrites-last": (
        "           newLog == Append(log[i], entry)\n",
        "           newLog == IF log[i] = << >> THEN <<entry>>\n"
        "                     ELSE [log[i] EXCEPT ![Len(log[i])] = entry]\n"),
    "vote-without-log-check": (
        "        grant == /\\ m.mterm = currentTerm[i]\n"
        "                 /\\ logOk\n",
        "        grant == /\\ m.mterm = currentTerm[i]\n"),
    "commit-without-quorum": (
        "                                Agree(index) \\in Quorum}\n",
        "                                TRUE}\n"),
}
# (edit, bound, the property TLC must report with all five checked). None marks an edit
# that the bound is too small to expose, which is reported and not hidden.
ALL_FIVE = [("votes-twice", MAIN, "ElectionSafety"),
            ("leader-without-quorum", MAIN, "ElectionSafety"),
            ("commit-by-counting", ONE_VALUE, None)]
# (property, edit, bound): with only that property checked, TLC must report it.
TEETH = [
    ("LeaderAppendOnly", "leader-overwrites-last", MAIN),
    ("LogMatching", "leader-overwrites-last", MAIN),
    ("StateMachineSafety", "leader-overwrites-last", MAIN),
    ("LeaderCompleteness", "vote-without-log-check", TWO_ELECTIONS),
]


def describe(bound):
    return (f"{bound['VALUES'].count('v')} value{'s' * (bound['VALUES'].count('v') > 1)}, max term "
            f"{bound['MAXTERM']}, log {bound['MAXLOG']}, messages {bound['MAXMESSAGES']}")


def edited(source, name):
    old, new = EDITS[name]
    if source.count(old) != 1:
        raise AssertionError(f"mutation site of {name} not found in raft.tla")
    return source.replace(old, new)


def tlc(source, bound, checked, directory, timeout=TIMEOUT):
    """Run TLC with the invariants and the action property named in `checked`. Returns
    (violated property or None, states generated, distinct states, seconds, finished)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raft.tla").write_text(source)
    (directory / "MCraft.tla").write_text((HERE / "MCraft.tla").read_text())
    config = (HERE / "MCraft.cfg").read_text()
    for key, value in bound.items():
        config = config.replace(f"%{key}%", str(value))
    invariants = [name for name in checked if name != ACTION]
    lines = (["INVARIANTS"] + [f"    {name}" for name in invariants]) if invariants else []
    lines += [f"PROPERTY {ACTION}"] if ACTION in checked else []
    (directory / "MCraft.cfg").write_text(config.replace("%CHECKED%", "\n".join(lines)))
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


def published():
    if hashlib.sha256((HERE / "raft.tla").read_bytes()).hexdigest() != PUBLISHED:
        raise AssertionError("raft.tla is not the published file")
    return (HERE / "raft.tla").read_text()


def main(report=print):
    source = published()
    with TemporaryDirectory() as tmp:
        violated, generated, distinct, seconds, finished = tlc(
            source, MAIN, INVARIANTS + [ACTION, VACUITY], Path(tmp) / "spec")
        if violated or not finished:
            raise AssertionError(f"raft.tla under the bound: violated {violated}, finished {finished}")
        report(f"TLC, published raft.tla, 3 servers, {describe(MAIN)}: {generated} states "
               f"generated, {distinct} distinct, every state explored, no violation of "
               f"ElectionSafety, LogMatching, LeaderCompleteness, StateMachineSafety or "
               f"LeaderAppendOnly ({seconds:.0f}s). {VACUITY} holds too: there is at most one "
               f"election at this bound, so LeaderCompleteness cannot fail in this run.")
        for name, bound, expect in ALL_FIVE:
            violated, generated, distinct, seconds, finished = tlc(
                edited(source, name), bound, INVARIANTS + [ACTION], Path(tmp) / name)
            if violated != expect:
                raise AssertionError(f"{name}: TLC reports {violated}, expected {expect}")
            outcome = (f"violates {violated}" if violated else
                       "NOT rejected within this bound" + ("" if finished else " (timed out)"))
            report(f"  mutant {name} at {describe(bound)}, all five checked: {outcome} "
                   f"({distinct} distinct states, {seconds:.0f}s)")
        report("each other property, checked alone against an edit at a bound where it can fail:")
        for number, (prop, name, bound) in enumerate(TEETH):
            violated, generated, distinct, seconds, finished = tlc(
                edited(source, name), bound, [prop], Path(tmp) / f"teeth{number}")
            if violated != prop:
                raise AssertionError(f"TLC does not reject {name} by {prop} at {describe(bound)}: "
                                     f"violated {violated}, finished {finished}")
            where = ("the bound of the exhaustive run" if bound == MAIN else
                     "a bound at which the published specification is not explored to the end")
            report(f"  {prop}: mutant {name} at {describe(bound)} violates it ({distinct} "
                   f"distinct states, {seconds:.0f}s); {where}")
    report("raft tla: ok")


def deeper(report=print, timeout=500):
    """The published specification at the bound with a second election, for a bounded time.
    Not part of the check: it reports whether TLC finished, and what it found if it did."""
    with TemporaryDirectory() as tmp:
        violated, generated, distinct, seconds, finished = tlc(
            published(), TWO_ELECTIONS, INVARIANTS + [ACTION], Path(tmp) / "deeper", timeout)
    if violated:
        raise AssertionError(f"raft.tla at {describe(TWO_ELECTIONS)} violates {violated}")
    report(f"published raft.tla, 3 servers, {describe(TWO_ELECTIONS)}: "
           + (f"every state explored, {generated} states generated, {distinct} distinct, no "
              f"violation of the five properties" if finished
              else f"NOT finished in {timeout}s; nothing is claimed") + f" ({seconds:.0f}s)")


if __name__ == "__main__":
    sys.exit(deeper() if sys.argv[1:] == ["deeper"] else main())
