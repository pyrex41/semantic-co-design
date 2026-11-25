"""Raft on the kernel. The retained check is two commands, each bounded:

    nix develop -c python3 -m tool check raft               the specification
    nix develop -c python3 -m targets.raft.check mutants    the teeth of every check

    python3 -m targets.raft.check                    the specification (same as the first)
    python3 -m targets.raft.check hunt <mutant> [three|five] [--from=N] [--seeds=M] [--only=CHECK]
                                                     search seeds for one mutant, shrink the
                                                     failing trace and retain it in mutants.json;
                                                     CHECK is a check's name, or `paper`
    python3 -m targets.raft.check story <mutant> [three|five] [CHECK]
                                                     print a retained trace, one step per line
"""
import collections
import fcntl
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

from tool import check, maude, sim
from tool.check import CheckFailure, require
from tool.compile import build, run_init
from tool.schema import Schema
from . import lin
from .shrink import lenient, shrink
from .workload import Clients, Once, maude_once

HERE = Path(__file__).parent
RETAINED = HERE / "mutants.json"

# The clients' operations. Request numbers are positions, counted from 1.
OPS = (("put", 0, 11), ("get", 0), ("put", 0, 12), ("put", 1, 13), ("get", 0))

# Seeded, timed schedules. Every fault kind has a budget. Timers run 80% to 120% of what
# the node asked for, and each node's clock has its own rate on top of that. One message
# in twenty is late by 100 to 500 time units, which is up to several election timeouts,
# and one disk operation in twelve stalls for 20 to 150, which is longer than a round trip
# to a follower. The crash rates are low enough for the four crashes of a run to be spread
# over it: the campaign reports where they fell and requires it (`spread`).
FAULTS = {"crash": 4, "drop": 4, "dup": 2, "partition": 2, "oneway": 2}
SHAPE = dict(budget=FAULTS, fault_rate=0.01, crash_mid_rate=0.0008, restart_rate=0.05,
             delay={"timer": [80, 120], "net-slow": [100, 500], "disk-slow": [20, 150]},
             slow={"net": 5, "disk": 8})
PROFILES = {
    "three": sim.profile(nodes=3, max_steps=1000, timer_rate=[100, 90, 115], **SHAPE),
    "five": sim.profile(nodes=5, max_steps=1400, timer_rate=[100, 90, 115, 105, 95], **SHAPE),
}
# The rates of the first version of this target, under which the crashes of a run were
# spent at its start. Kept as the mutant of the campaign's `spread` requirement.
BUNCHED = sim.profile(nodes=3, max_steps=1000, timer_rate=[100, 90, 115],
                      budget={**FAULTS, "crash": 3}, fault_rate=0.02, crash_mid_rate=0.05,
                      restart_rate=0.05, delay={"timer": [80, 120]})
SEEDS = {"three": 150, "five": 60}
REPLAY_EVERY = 10
HUNT_BUDGET = 400               # seeds tried per profile before a mutant counts as not found

# Liveness: at virtual time CALM_AT the client ends all faults and starts sending one more
# put. LIVENESS_BOUND is the declared bound on the virtual time from then until its `ok`
# reaches the outside, and until every server is up, has finished recovery and has applied
# that put. Late messages and disk stalls are not faults and go on.
LIVE_OPS = (("put", 0, 11), ("get", 0))
FINAL = ("put", 1, 99)
CALM_AT = 1500
LIVENESS_BOUND = 1500
LIVE_SEEDS = {"three": 60, "five": 30}
LIVE_STEPS = {"three": 1300, "five": 2200}
LIVE_MUTANTS = {
    "held-reply-never-sent": {
        "what": "a reply waiting for the disk is never released",
        "old": "Free (and (not (= HKind none)) (<= HToken Acked1))",
        "new": "Free false"},
    "never-grants": {
        "what": "no server ever grants a vote",
        "old": "Grant (and (= T Term1)",
        "new": "Grant (and false"},
}
# A mutant that degrades instead of disabling: the cluster still answers, one server is lost.
LIVE_DEGRADED = "recovery-never-completes"

# Bounded searches: every schedule of an untimed profile. `timer` bounds how many timer
# events may fire, which is what makes the state space finite.
PUT = ("put", 1, 0, 11)
BOUNDS = {
    "2 servers, 2 timers, a put": (2, {"timer": 2}, (PUT,)),
    "2 servers, 1 timer, a put, a crash": (2, {"timer": 1, "crash": 1}, (PUT,)),
    "2 servers, 1 timer, a put, a drop": (2, {"timer": 1, "drop": 1}, (PUT,)),
    "3 servers, 1 timer, a duplicate": (3, {"timer": 1, "dup": 1}, ()),
    "3 servers, 1 timer, a crash": (3, {"timer": 1, "crash": 1}, ()),
}
CRASH, TIMERS = "2 servers, 1 timer, a put, a crash", "2 servers, 2 timers, a put"
SEARCH_MUTANTS = {"vote-before-durable": CRASH, "ack-before-durable": CRASH,
                  "recovery-forgets-vote": CRASH, "self-counts-before-durable": CRASH,
                  "leader-counts-undurable-self": CRASH, "length-before-entry": CRASH,
                  "candidate-asks-before-durable": TIMERS, "stale-vote-counted": TIMERS}

KEEP = ("boot", "restart", "deliver", "local", "crash", "sync", "error")
WITNESSES = ["wit.leader-change", "wit.conflict-repaired", "wit.log-recovered", "wit.committed",
             "wit.full", "wit.retry-skipped"]
# The five properties of Figure 3 of the paper. With linearizability and "no trap" they are
# the published properties (`paper`); every other check states a rule of Figure 2 or of the
# header of spec.shen directly.
FIGURE3 = ["inv.election-safety", "inv.leader-append-only", "inv.log-matching",
           "inv.leader-completeness", "inv.state-machine-safety"]

# Each mutant is one edit of spec.shen. The retained evidence in mutants.json says which
# check rejected it first, on which seed, and whether a published property ever did.
MUTANTS = {
    "vote-without-log-check": {
        "what": "grants a vote without the up-to-date check (5.4.1)",
        "old": "(raft.up-to-date? LastTerm LastIndex (raft.term-at Len Log) Len)))",
        "new": "true))"},
    "commit-by-counting": {
        "what": "commits an entry of an earlier term by counting replicas (Figure 8)",
        "old": "(and (= (raft.term-at I Log) Term)",
        "new": "(and true"},
    "vote-before-durable": {
        "what": "answers a vote request before votedFor is durable",
        "old": "(if Ready [[vote-rsp Src Term1 Grant]] [])",
        "new": "[[vote-rsp Src Term1 Grant]]"},
    "local-get": {
        "what": "a leader answers a get from its own state without confirming leadership",
        "old": "State [get Req Key] -> (raft.client State get Key 0 Req)",
        "new": "[Id N leader Term Voted Len Log Commit Applied Kv | Rest] [get Req Key] ->\n"
               "    [[Id N leader Term Voted Len Log Commit Applied Kv | Rest]\n"
               "     [[reply Req ok (raft.at Key Kv 0)]]]\n"
               "  State [get _ _] -> [State []]"},
    "votes-twice": {
        "what": "grants a vote in a term in which it already voted for another server",
        "old": "(and (or (= Voted1 -1) (= Voted1 Src))",
        "new": "(and true"},
    "ack-before-durable": {
        "what": "answers AppendEntries before the entry is durable",
        "old": "(if Ready [[app-rsp Src Term1 Fits Top]] [])",
        "new": "[[app-rsp Src Term1 Fits Top]]"},
    "commit-unchecked-suffix": {
        "what": "a follower commits up to its own last entry, not the last one the leader confirmed",
        "old": "Commit1 (if (> (raft.min LCommit Top) Commit) (raft.min LCommit Top) Commit)",
        "new": "Commit1 (if (> (raft.min LCommit Len1) Commit) (raft.min LCommit Len1) Commit)"},
    "put-twice": {
        "what": "executes every copy of a put that a client's retries left in the log",
        "old": "(if (and (= Op put) (= (raft.find Req Log 1) (+ Applied 1)))",
        "new": "(if (and (= Op put) true)"},
    # ---- found by the reviewers of this target: each survived every check of the first
    # version on seeds 0 to 149 of `three`, or was rejected by nothing retained.
    "leader-overwrites-last": {
        "what": "a leader puts a new entry in the place of its last one",
        "old": "(+ Len 1) [Term Op Key Val Req]))",
        "new": "(if (> Len 0) Len (+ Len 1)) [Term Op Key Val Req]))"},
    "no-prev-term-check": {
        "what": "a follower accepts AppendEntries without comparing prevLogTerm",
        "old": "(and (= (raft.term-at Prev Log) PrevT) (or (not Has) (< Prev 8)))))",
        "new": "(and true (or (not Has) (< Prev 8)))))"},
    "accept-stale-leader": {
        "what": "a follower treats AppendEntries of an earlier term as coming from its leader",
        "old": "Current (>= T Term)",
        "new": "Current true"},
    "ack-old-term": {
        "what": "a follower stores the entry of an AppendEntries of an earlier term",
        "old": "Fits (and Current",
        "new": "Fits (and true"},
    "stale-vote-counted": {
        "what": "a candidate counts a vote that was granted in an earlier term",
        "old": "(let Counts (and (= Role candidate) (and (= T Term) Granted))",
        "new": "(let Counts (and (= Role candidate) Granted)"},
    "stale-ack-counted": {
        "what": "a leader counts an acknowledgement that was given in an earlier term",
        "old": "(let Mine (and (= Role leader) (= T Term))",
        "new": "(let Mine (= Role leader)"},
    "recovery-forgets-vote": {
        "what": "a restarted server has no vote, whatever its disk says",
        "old": "[Id N _ Term | Rest] -> [[Id N follower Term | Rest] [[arm 0 (raft.patience Id Term)]]])",
        "new": "[Id N _ Term _ | Rest] ->\n"
               "    [[Id N follower Term -1 | Rest] [[arm 0 (raft.patience Id Term)]]])"},
    "recovery-never-completes": {
        "what": "a server that restarts with entries on its disk reads past the last one",
        "old": "(if (= Key Len)\n        (raft.boot",
        "new": "(if false\n        (raft.boot"},
    "candidate-asks-before-durable": {
        "what": "a candidate asks for votes before its vote for itself is durable",
        "old": "Asks (and Durable (and (= Role candidate) (not (raft.at Id Votes false))))",
        "new": "Asks (and (= Role candidate) (not (raft.at Id Votes false)))"},
    "leader-counts-undurable-self": {
        "what": "a leader counts its own copy of an entry the moment it appends it",
        "old": "Votes Next Match (+ Issued 2) Acked Hold]",
        "new": "Votes Next (raft.set Id Index Match) (+ Issued 2) Acked Hold]"},
    "self-counts-before-durable": {
        "what": "a leader counts its whole log, durable or not, when an acknowledgement arrives",
        "old": "Commit1 (if Got (raft.advance 8 Term N Len Log Match1 Commit) Commit)",
        "new": "Commit1 (if Got (raft.advance 8 Term N Len Log (raft.set Id Len Match1) Commit) Commit)"},
    "length-before-entry": {
        "what": "a leader writes the new length before the entry it covers",
        "old": "[[write (+ Issued 1) Index ETerm Op Key Val Req]\n"
               "       [write (+ Issued 2) 0 Term meta Voted Index 0]]))",
        "new": "[[write (+ Issued 1) 0 Term meta Voted Index 0]\n"
               "       [write (+ Issued 2) Index ETerm Op Key Val Req]]))"},
    "follower-length-before-entry": {
        "what": "a follower writes the new length before the entry it covers",
        "old": "[[write (+ Issued 1) Index ETerm Op Key Val Req]\n"
               "                        [write (+ Issued 2) 0 Term1 meta Voted1 Index 0]])",
        "new": "[[write (+ Issued 1) 0 Term1 meta Voted1 Index 0]\n"
               "                        [write (+ Issued 2) Index ETerm Op Key Val Req]])"},
    "entry-before-cut": {
        "what": "a follower writes the replacing entry before it cuts the conflicting suffix",
        "old": "[[write (+ Issued 1) 0 Term1 meta Voted1 Prev 0]\n"
               "                        [write (+ Issued 2) Index ETerm Op Key Val Req]\n",
        "new": "[[write (+ Issued 1) Index ETerm Op Key Val Req]\n"
               "                        [write (+ Issued 2) 0 Term1 meta Voted1 Prev 0]\n"},
}
REQUIRED = ["vote-without-log-check", "commit-by-counting", "vote-before-durable", "local-get"]

# A vote under a server id outside the cluster. No simulated run delivers one, so this
# mutant is judged by `outsiders`, which hands such messages to the step directly.
OUTSIDER = {"what": "a candidate counts a vote from an id that is not a member",
            "old": "(raft.vote-rsp [Id N | Rest] Src T Granted) where (raft.member? Src Id N)",
            "new": "(raft.vote-rsp [Id N | Rest] Src T Granted) where true"}


def edit(source, mutant):
    require(source.count(mutant["old"]) == 1, f"mutation site not unique: {mutant['what']}")
    return source.replace(mutant["old"], mutant["new"])


def mutate(source, name):
    return edit(source, MUTANTS[name])


def slim(trace):
    """The steps at which a server state or a disk can change. The invariants read only
    states, disks and effects, so the other steps (outputs, drops, client sends, cuts)
    add nothing for them; the linearizability check reads the whole trace."""
    return {**trace, "steps": [s for s in trace["steps"] if s["kind"] in KEEP]}


def judge(schema, traces, names=None, witnesses=()):
    """Per trace: ([(failed check, detail)], [witness seen])."""
    declared = schema.invariants["trace"]
    names = declared if names is None else names
    shen_names = [n for n in names if n in declared]
    values = [[]] * len(traces)
    if shen_names or witnesses:
        values = check.invariants(schema, [slim(t) for t in traces], state=[],
                                  trace=[*shen_names, *witnesses])
    out = []
    for trace, bad in zip(traces, values):
        bad = dict(bad)
        failures = [(n, bad[n]) for n in shen_names if n in bad]
        if "no-trap" in names or names is declared:
            if trace["end"].startswith("error"):
                failures.append(("no-trap", trace["end"]))
        if "linearizability" in names or names is declared:
            problem = lin.check(trace)
            if problem:
                failures.append(("linearizability", problem))
        out.append((failures, [w not in bad for w in witnesses]))
    return out


def text(value):
    return json.dumps(value) if not isinstance(value, str) else value


def story(trace):
    """A trace as lines a person can read."""
    lines = []
    for step in trace["steps"]:
        head = f"{step['i']:3} t={step['t']:<5} {step['kind']:8}"
        if "node" in step:
            head += f" n{step['node']}"
        if step.get("event") is not None:
            head += " " + " ".join(map(str, step["event"]))
        elif step["kind"] in ("partition", "oneway", "client", "drop", "dup"):
            head += " " + " ".join(map(str, step["action"][1:]))
        if "effects" in step:
            done = step["effects"][:step["applied"]]
            head += "  =>  " + "; ".join(" ".join(map(str, e)) for e in done)
            if step.get("crashed"):
                head += f"  [dies; {step['lost_writes']} writes lost]"
            post = step["post"]
            head += f"   [{post[2]} term {post[3]} voted {post[4]} len {post[5]} commit {post[7]}]"
        elif step.get("crashed"):
            head += f"  [{step['lost_writes']} writes lost]"
        lines.append(head)
    return lines


# ------------------------------------------------------------------------------ campaigns

def shape(trace):
    """Where the faults of one run fell, counted from the trace alone.

    crashes       the step index of every crash
    half-synced   syncs after which a disk held some but not all writes of one step
    torn          crashes that left a disk so: the persisted prefix ends inside a step
    stale-vote    a granted vote of an earlier term handed to a candidate
    stale-ack     a successful AppendEntries reply of an earlier term handed to a leader
    stale-append  AppendEntries of an earlier term handed to a running server; -entry: with an entry
    overtaken     AppendEntries handled after one that the same leader sent later in the same
                  term (reordering, or a late duplicate): a leader's commit index never
                  decreases within its term, so a smaller leaderCommit was sent earlier
    """
    counts, crashes, newest = collections.Counter(), [], {}
    pending = collections.defaultdict(list)     # per node: (position in its step, writes of the step)
    for step in trace["steps"]:
        node, event = step.get("node"), step.get("event") or []
        if "effects" in step:
            issued = sum(e[0] == "write" for e in step["effects"])
            done = sum(e[0] == "write" for e in step["effects"][:step["applied"]])
            pending[node] += [(n, issued) for n in range(done)]
        if step["kind"] == "sync":
            position, issued = pending[node].pop(0)
            counts["half-synced"] += position < issued - 1
        if step.get("crashed"):
            crashes.append(step["i"])
            kept = step["persisted"]
            if kept:
                position, issued = pending[node][kept - 1]
                counts["torn"] += position < issued - 1
            pending[node] = []
        if step["kind"] == "deliver" and "pre" in step:
            role, term = step["pre"][2], step["pre"][3]
            if event[0] == "on-vote-rsp" and event[2] < term and event[3] and role == "candidate":
                counts["stale-vote"] += 1
            if event[0] == "on-app-rsp" and event[2] < term and event[3] and role == "leader":
                counts["stale-ack"] += 1
            if event[0] == "on-app-req":
                if event[2] < term and role not in ("loading", "failed"):
                    counts["stale-append"] += 1
                    counts["stale-append-entry"] += bool(event[5])
                key = (node, event[1], event[2])
                counts["overtaken"] += event[11] < newest.get(key, 0)
                newest[key] = max(newest.get(key, 0), event[11])
    return counts, crashes


def quartiles(values):
    values = sorted(values)
    return [values[len(values) * k // 4] for k in (1, 2, 3)]


def spread(crashes, late, runs, steps):
    """(quartiles of the step index of the crashes of a campaign, whether they are spread
    over the runs): half of the crashes fall after the first 30% of a run, a quarter in its
    second half, and at least half of the runs have a crash in their second half."""
    low, middle, high = quartiles(crashes)
    return [low, middle, high], middle >= steps * 3 // 10 and high >= steps // 2 and late * 2 >= runs


def campaign(schema, native, label, seeds, report, *, replay=True):
    """Seeded runs of one profile, each checked as it is produced. Returns the pairs seen
    and the counts of `shape` over all runs."""
    prof, workload = PROFILES[label], Clients(OPS)
    pairs, kinds, seen = {}, collections.Counter(), [0] * len(WITNESSES)
    answered, steps, replayed, cuts, lost = collections.Counter(), 0, 0, 0, 0
    shapes, crashes, late = collections.Counter(), [], 0
    started = time.time()
    for first in range(0, seeds, 10):
        batch = [sim.run(native, workload, prof, seed) for seed in range(first, min(first + 10, seeds))]
        for trace in batch:
            require(trace["end"] == "max_steps", f"{label} seed {trace['seed']} ended {trace['end']}")
            if replay and trace["seed"] % REPLAY_EVERY == 0:
                sim.replay(native, workload, trace)
                replayed += 1
            steps += len(trace["steps"])
            kinds.update(s["kind"] for s in trace["steps"])
            kinds.update("mid-step crash" for s in trace["steps"] if "before_crash" in s)
            lost += sum(s.get("lost_writes", 0) for s in trace["steps"])
            counts, where = shape(trace)
            shapes.update(counts)
            crashes += where
            late += any(index >= prof["max_steps"] // 2 for index in where)
            answered[sum(op["returned"] is not None for op in lin.history(trace)[0])] += 1
        cuts += sum(len(c) for c in check.effects_applied(schema, batch))
        for trace, (failures, witnessed) in zip(batch, judge(schema, batch, witnesses=WITNESSES)):
            if failures:
                raise CheckFailure(f"{label} seed {trace['seed']}: {failures[0][0]}: "
                                   f"{text(failures[0][1])[:600]}")
            seen = [a + b for a, b in zip(seen, witnessed)]
        for key, value in check.step_pairs(batch).items():
            require(pairs.setdefault(key, value) == value, "the native step gave two results")
    needed = ("crash", "restart", "drop", "dup", "partition", "oneway", "heal", "lost",
              "mid-step crash")
    require(all(kinds[k] for k in needed) and lost and shapes["overtaken"],
            f"{label}: a fault kind never happened: {dict(kinds)}, {lost} writes lost, "
            f"{shapes['overtaken']} messages overtaken")
    where, spread_out = spread(crashes, late, seeds, prof["max_steps"])
    require(spread_out, f"{label}: crashes are bunched at the start: quartiles of the step "
                        f"index {where}, {late} of {seeds} runs with a crash in their second half")
    report(f"{label}: {prof['nodes']} servers, {seeds} seeds, {steps} steps, "
           f"{time.time() - started:.0f}s; every run ends max_steps with no trap; "
           f"{replayed} traces replayed step by step and re-run from their seed, byte-identical")
    report(f"  faults: " + ", ".join(f"{kinds[k]} {k}" for k in needed)
           + f"; {lost} issued writes never reached the disk; timers skewed 80-120% on clocks at "
             f"{prof['timer_rate']}%")
    report(f"  where the crashes fell: step index quartiles {where} of {prof['max_steps']}; "
           f"{late} of {seeds} runs have a crash in their second half")
    report(f"  histories that occurred: a disk held part of a step's writes after a sync "
           f"{shapes['half-synced']} times and after a crash {shapes['torn']} times; a granted "
           f"vote of an earlier term reached a candidate {shapes['stale-vote']} times; an "
           f"acknowledgement of an earlier term reached a leader {shapes['stale-ack']} times; "
           f"AppendEntries of an earlier term reached a server {shapes['stale-append']} times, "
           f"{shapes['stale-append-entry']} with an entry; {shapes['overtaken']} AppendEntries "
           f"arrived after a later one from the same leader")
    report(f"  the {len(schema.invariants['trace'])} invariants hold on every trace and every "
           f"history is linearizable; operations "
           f"answered per run {dict(sorted(answered.items()))}; witnesses "
           + ", ".join(f"{name[4:]} {count}" for name, count in zip(WITNESSES, seen)))
    return pairs, shapes


def hunt(schema, native, label, names, budget, *, start=0, batch=10):
    """The first seed of a profile, from `start`, on which one of `names` fails (None: any
    check). Returns (seed, trace, check, detail) or None when seed `budget` is reached."""
    prof, workload = PROFILES[label], Clients(OPS)
    for first in range(start, budget, batch):
        traces = [sim.run(native, workload, prof, seed) for seed in range(first, min(first + batch, budget))]
        for trace, (failures, _) in zip(traces, judge(schema, traces)):
            failures = [f for f in failures if names is None or f[0] in names]
            if failures:
                return trace["seed"], trace, failures[0][0], failures[0][1]
    return None


def paper(schema):
    """The checks that state published properties."""
    return FIGURE3 + ["no-trap", "linearizability"]


def retained():
    return json.loads(RETAINED.read_text()) if RETAINED.exists() else {}


def retain(name, change):
    """Apply `change(entry)` to one mutant's entry of mutants.json. The file is locked, so
    that hunts for different mutants may run at the same time."""
    with open(RETAINED, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        content = handle.read()
        everything = json.loads(content) if content.strip() else {}
        entry = everything.get(name, {})
        change(entry)
        everything[name] = entry
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(everything, indent=1, sort_keys=True) + "\n")
    return entry


def slot_of(entry, only):
    return entry if only is None else entry.setdefault("by", {}).setdefault(only, {})


def record_mutant(schema, directory, name, report, labels=("three", "five"), *, only=None,
                  start=0, seeds=HUNT_BUDGET):
    """Search seeds `start` to `start + seeds`, shrink and retain the evidence for one mutant.

    By default the search is for the first failure of any check. With `only` it is for a
    failure of that one check, or of any published property (`paper`), retained separately
    under that name. A search that starts above 0 continues an earlier one that found
    nothing, so the count of seeds tried stays the number of seeds from 0.
    """
    native = build(schema, Path(directory) / name, mutate(schema.source, name), executable=False)
    names = None if only is None else paper(schema) if only == "paper" else [only]
    for label in labels:
        before = slot_of(retained().get(name, {}), only)
        tried = before.get("not_found", {}).get(label, {}).get("seeds_tried", 0)
        require(start <= tried, f"{name} on {label}: seeds {tried} to {start} were never tried")
        started = time.time()
        found = hunt(schema, native, label, names, start + seeds, start=start)
        if found is None:
            outcome = ("not_found", {"seeds_tried": start + seeds})
            report(f"{name} on {label}: no failure of {only or 'any check'} in "
                   f"seeds 0 to {start + seeds - 1} ({time.time() - started:.0f}s for the last "
                   f"{seeds})")
        else:
            seed, trace, failed, detail = found
            report(f"{name} on {label}: {failed} fails on seed {seed} after {seed + 1} seeds "
                   f"({time.time() - started:.0f}s); shrinking {len(trace['steps'])} steps")
            workload = Clients(OPS)
            fails = lambda t: any(f[0] == failed for f in judge(schema, [t], names=[failed])[0][0])
            short, candidates = shrink(native, workload, trace, fails)
            sim.replay(native, workload, short)
            detail = judge(schema, [short], names=[failed])[0][0][0][1]
            script = [[s["action"], s["draws"]] for s in short["steps"] if s["action"] != ["boot"]]
            outcome = ("found", {
                "seed": seed, "seeds_tried": seed + 1, "check": failed, "steps": len(trace["steps"]),
                "digest": sim.digest(trace), "short_steps": len(short["steps"]),
                "short_digest": sim.digest(short), "detail": text(detail)[:400],
                "script": json.dumps(script, separators=(",", ":"))})
            report(f"  shrunk to {len(short['steps'])} steps in {candidates} candidates "
                   f"({time.time() - started:.0f}s)")

        def change(entry, label=label, outcome=outcome):
            entry["what"] = MUTANTS[name]["what"]
            slot = slot_of(entry, only)
            for key in ("found", "not_found"):
                slot.setdefault(key, {}).pop(label, None)
            slot[outcome[0]][label] = outcome[1]
        retain(name, change)


def check_mutants(schema, native, directory, report):
    """Every retained piece of evidence still holds against a freshly built mutant, and
    the specification passes where the mutant fails. Returns the built mutants."""
    kept = retained()
    require(set(kept) == set(MUTANTS), "mutants.json does not cover the declared mutants: "
                                       "run `python3 -m targets.raft.check hunt <name>`: "
                                       f"{sorted(set(kept) ^ set(MUTANTS))}")
    workload, natives, alike = Clients(OPS), {}, collections.Counter()
    rejecting = collections.Counter()
    for name in MUTANTS:
        entry = kept[name]
        broken = build(schema, Path(directory) / name, mutate(schema.source, name), executable=False)
        natives[name] = broken
        groups = [("", entry)] + [(f"{only}: ", slot) for only, slot in entry.get("by", {}).items()]
        notes, evidence = [], 0
        for prefix, slot in groups:
            for label, found in sorted(slot.get("found", {}).items()):
                prof, script = PROFILES[label], json.loads(found["script"])
                trace = sim.run(broken, workload, prof, found["seed"])
                require(sim.digest(trace) == found["digest"],
                        f"{name}: seed {found['seed']} of {label} no longer gives the retained trace")
                require(judge(schema, [trace], names=[found["check"]])[0][0],
                        f"{name}: {found['check']} accepts seed {found['seed']} of {label}")
                short = sim.script(broken, workload, prof, script)
                require(sim.digest(short) == found["short_digest"],
                        f"{name}: the retained short trace does not reproduce")
                sim.replay(broken, workload, short)
                require(judge(schema, [short], names=[found["check"]])[0][0],
                        f"{name}: {found['check']} accepts the retained short trace")
                # The same seed and the same short schedule on the specification: nothing
                # fails. Where the specification takes another path, a recorded action that
                # is not enabled for it is skipped (shrink.lenient), so every mutant has a
                # like-for-like passing run.
                same = [sim.run(native, workload, prof, found["seed"])]
                try:
                    same.append(sim.script(native, workload, prof, script))
                    alike["exactly"] += 1
                except sim.Divergence:
                    taken, run = lenient(native, workload, prof, script)
                    same.append(run)
                    alike["skipping"] += 1
                    alike["skipped"] += len(script) - len(taken)
                    alike["of"] += len(script)
                require(not any(failures for failures, _ in judge(schema, same)),
                        f"{name}: the specification fails on seed {found['seed']} of {label} "
                        f"or on the short schedule")
                evidence += 1
                rejecting[found["check"]] += 1
                notes.append(f"{prefix}{label} seed {found['seed']}, {found['seeds_tried']} seeds, "
                             f"{found['check']}, shrunk to {found['short_steps']} steps")
            for label, missing in sorted(slot.get("not_found", {}).items()):
                notes.append(f"{prefix}{label} NOT FOUND in {missing['seeds_tried']} seeds")
        require(name not in REQUIRED or evidence,
                f"{name} is required and no evidence against it is retained")
        report(f"  {name}: " + "; ".join(notes))
    report(f"  on the specification each of those seeds passes every check, and so does each "
           f"short schedule: {alike['exactly']} run exactly as recorded, {alike['skipping']} "
           f"with the actions the specification does not enable skipped ({alike['skipped']} "
           f"of {alike['of']} actions)")
    return natives, rejecting


def refinement_teeth(schema, native, natives, report):
    """A library compiled from an edited specification does not pass the refinement check
    against the unedited Shen source."""
    traces = [sim.run(native, Clients(OPS), PROFILES["three"], seed) for seed in range(10)]
    pairs = check.step_pairs(traces)
    gets = [key for key in pairs if key[1][0] == "get" and key[0][2] == "leader"][:200]
    require(gets, "no get was ever handled by a leader")
    broken = natives["local-get"]
    results = {}
    for state, event in gets:
        after, effects = broken.step(schema.encode_state(state), event)
        results[(state, event)] = sim.freeze((schema.decode_state(after), effects))
    try:
        check.refinement(schema, broken, results, nodes=3)
    except CheckFailure:
        report(f"  refinement teeth: the library compiled from local-get differs from the "
               f"unedited Shen source on gets handled by a leader ({len(gets)} tried)")
    else:
        raise CheckFailure("refinement accepts a library compiled from an edited specification")


# ------------------------------------------------------------------------------- liveness

def settle(trace, request, value):
    """(virtual time faults stopped, time the final put was answered, time from which every
    server is up, running and has applied the final put); None where it did not happen."""
    calm = answered = whole = None
    for step in trace["steps"]:
        if step["kind"] == "client" and step["action"][1] == "calm":
            calm = step["t"]
        elif (step["kind"] == "output" and step["event"][:3] == ["reply", request, "ok"]
              and answered is None):
            answered = step["t"]
        states = step["world"]["states"]
        ready = all(s is not None and s[2] not in ("loading", "failed") and s[9][1] == value
                    for s in states)
        whole = (step["t"] if whole is None else whole) if ready else None
    return calm, answered, whole


def liveness(schema, native, label, seeds, report=None):
    """After faults stop, the final put is answered within LIVENESS_BOUND, and within the
    same bound every server is up, out of recovery and has applied it. Returns the seeds
    on which the answer was late, the seeds on which the cluster was not whole in time,
    and both lists of times."""
    prof = sim.profile(**{**PROFILES[label], "max_steps": LIVE_STEPS[label],
                          "budget": {**PROFILES[label]["budget"], "dup": 1}})
    workload = Clients(LIVE_OPS, sends=1, calm_at=CALM_AT, final=FINAL)
    late, partial, times, wholes = [], [], [], []
    for seed in range(seeds):
        trace = sim.run(native, workload, prof, seed)
        require(not trace["end"].startswith("error"), f"liveness {label} seed {seed}: {trace['end']}")
        problem = lin.check(trace)
        require(problem is None, f"liveness {label} seed {seed}: {problem}")
        calm, answered, whole = settle(trace, len(LIVE_OPS) + 1, FINAL[2])
        require(calm is not None, f"liveness {label} seed {seed}: faults never stopped")
        if answered is None or answered - calm > LIVENESS_BOUND:
            late.append(seed)
        else:
            times.append(answered - calm)
        if whole is None or whole - calm > LIVENESS_BOUND:
            partial.append(seed)
        else:
            wholes.append(max(whole - calm, 0))
    if report:
        middle = lambda values: sorted(values)[len(values) // 2]
        report(f"liveness, {label}: faults stop at virtual time {CALM_AT} on {seeds} seeds; the put "
               f"sent then is answered on {len(times)} of them, after {min(times)} to {max(times)} "
               f"time units (median {middle(times)}); every server is up, out of recovery and "
               f"has applied it on {len(wholes)} of them, after {min(wholes)} to {max(wholes)} "
               f"(median {middle(wholes)}); declared bound {LIVENESS_BOUND}")
    return late, partial, times, wholes


# -------------------------------------------------------------------------- bounded search

def unsafe(observation):
    """A reason why one reachable world is wrong, or None. Stated over what an observation
    holds: the memory of every server, the durable rows of every disk, the replies that got
    out. These restate, per world, what the Shen invariants say over histories."""
    states = {t[1]: t[2] for t in observation if t[0] == "state"}
    rows = {t[1]: {row[0]: row[1:] for row in t[2]} for t in observation if t[0] == "rows"}
    running = {n: s for n, s in states.items() if s != "down" and s[2] not in ("loading", "failed")}
    majority = len(states) // 2 + 1
    meta = {n: (r[0][0], r[0][2], r[0][3]) if 0 in r else (0, -1, 0) for n, r in rows.items()}
    for n, state in states.items():
        if state != "down" and state[2] == "failed":
            return f"server {n} stopped: a row inside its recorded length is missing"
    for n, (_, _, length) in meta.items():
        if any(key not in rows[n] for key in range(1, length + 1)):
            return f"the disk of server {n} records {length} entries and lacks one of them"
    leaders = [(s[3], n) for n, s in running.items() if s[2] == "leader"]
    if len({term for term, _ in leaders}) != len(leaders):
        return f"two leaders in one term: {leaders}"
    for term, leader in leaders:
        voters = sum(1 for t, voted, _ in meta.values() if t > term or (t == term and voted == leader))
        if voters < majority:
            return f"server {leader} leads term {term} with {voters} durable votes"
        own, log = running[leader][12][leader], running[leader][6]
        if own > meta[leader][2] or any(rows[leader].get(i + 1) != log[i] for i in range(own)):
            return f"server {leader} counts {own} entries of its own, which its disk does not hold"
    for a, x in running.items():
        if not x[8] <= x[7] <= x[5]:
            return f"server {a}: applied {x[8]}, commit {x[7]}, length {x[5]}"
        for b, y in running.items():
            if a < b:
                same = True
                for i in range(min(x[5], y[5])):
                    same = same and x[6][i] == y[6][i]
                    if x[6][i][0] == y[6][i][0] and not same:
                        return f"log matching: servers {a} and {b} at index {i + 1}"
                    if i < min(x[7], y[7]) and x[6][i] != y[6][i]:
                        return f"servers {a} and {b} committed different entries at {i + 1}"
    for thing in observation:
        if thing[0] == "got" and thing[1][0] == "reply" and thing[1][2] == "ok":
            stored = sum(1 for n, r in rows.items()
                         if any(key in r and r[key][1] == "put" and r[key][4] == thing[1][1]
                                for key in range(1, meta[n][2] + 1)))
            if stored < majority:
                return f"request {thing[1][1]} was answered with its entry durable on {stored} disks"
    return None


def search(schema, native, label, *, source=None):
    nodes, budget, requests = BOUNDS[label]
    prof = sim.profile(nodes=nodes, timed=False, observe_states=True, budget=budget)
    return check.maude_agreement(schema, native, Once(requests), maude_once(requests, nodes), prof,
                                 source=source, max_states=250000, timeout=300)


def bounded(schema, native, report):
    """Every bound on the specification. Returns the pairs executed."""
    pairs = {}
    for label in BOUNDS:
        started = time.time()
        mine, reached, facts = search(schema, native, label)
        require(facts["maude_trap"] is None, f"{label}: a reachable step has no result")
        require(mine == reached, f"{label}: Maude and the simulator disagree: {len(reached - mine)} "
                                 f"observations only in Maude, {len(mine - reached)} only simulated")
        bad = [reason for reason in map(unsafe, reached) if reason]
        require(not bad, f"{label}: {bad[:1]}")
        pairs.update(facts["pairs"])
        leaders = sum(1 for o in reached if any(t[0] == "state" and t[2] != "down"
                                                and t[2][2] == "leader" for t in o))
        answered = sum(1 for o in reached if any(t[0] == "got" for t in o))
        report(f"  {label}: Maude {facts['maude_states']} states, simulator {facts['sim_states']}; "
               f"the same {len(reached)} observations, {leaders} with a leader, {answered} with "
               f"the put answered; all safe ({time.time() - started:.0f}s)")
    return pairs


def bounded_mutants(schema, native, natives, report):
    """Each search mutant in its bound: Maude and the simulator still agree with each other,
    and some reachable world is unsafe."""
    reference = {label: search(schema, native, label)[1] for label in set(SEARCH_MUTANTS.values())}
    for label in reference:
        require(not any(map(unsafe, reference[label])), f"{label}: the specification is unsafe")
    for name, label in SEARCH_MUTANTS.items():
        source = mutate(schema.source, name)
        mine, reached, facts = search(schema, natives[name], label, source=source)
        reasons = collections.Counter(reason for reason in map(unsafe, reached) if reason)
        require(mine == reached and reasons and reached != reference[label],
                f"the bounded search does not reject {name}")
        report(f"  mutant {name}, {label}: Maude and the simulator agree on {len(reached)} "
               f"observations, {sum(reasons.values())} of them unsafe "
               f"({reasons.most_common(1)[0][0]})")


# ------------------------------------------------------------------- directed schedules

def directed_schedules():
    from . import directed, figure8
    return {
        "figure 8": (figure8.schedule, {}),
        "lost vote": (figure8.lost_vote, {}),
        "forgotten vote": (directed.forgotten_vote, {}),
        "unwritten candidacy": (directed.unwritten_candidacy, {}),
        "stale vote": (directed.stale_vote, {}),
        "stale leader": (directed.stale_leader, {}),
        "slow disk": (directed.slow_disk, {}),
        "lost acknowledgement": (directed.lost_ack, {}),
        "torn append, leader": (directed.torn_append, {"leader": True}),
        "torn append, follower": (directed.torn_append, {}),
        "torn cut": (directed.torn_cut, {}),
        "stale acknowledgement": (directed.stale_ack, {}),
    }


# (schedule, mutant, the checks that must all reject the mutant on it)
DIRECTED = [
    ("figure 8", "commit-by-counting",
     ["inv.leader-completeness", "inv.state-machine-safety", "inv.commit-rule"]),
    ("lost vote", "vote-before-durable",
     ["inv.election-safety", "inv.replies-are-durable", "inv.one-vote-per-term"]),
    ("forgotten vote", "recovery-forgets-vote",
     ["inv.election-safety", "inv.one-vote-per-term", "inv.recovery-restores-the-disk"]),
    ("unwritten candidacy", "candidate-asks-before-durable",
     ["inv.election-safety", "inv.replies-are-durable", "inv.one-vote-per-term"]),
    ("stale vote", "stale-vote-counted", ["inv.election-safety", "inv.leaders-are-elected"]),
    ("stale leader", "accept-stale-leader",
     ["inv.leader-completeness", "inv.state-machine-safety", "inv.match-is-durable"]),
    ("stale leader", "ack-old-term",
     ["inv.leader-completeness", "inv.state-machine-safety", "inv.match-is-durable"]),
    ("slow disk", "leader-counts-undurable-self",
     ["inv.leader-completeness", "inv.commits-are-durable", "inv.match-is-durable"]),
    ("slow disk", "self-counts-before-durable",
     ["inv.leader-completeness", "inv.commits-are-durable"]),
    ("lost acknowledgement", "ack-before-durable",
     ["inv.leader-completeness", "inv.replies-are-durable", "inv.commits-are-durable"]),
    ("torn append, leader", "length-before-entry",
     ["inv.recovery-completes", "inv.disk-is-a-held-state"]),
    ("torn append, follower", "follower-length-before-entry",
     ["inv.recovery-completes", "inv.disk-is-a-held-state"]),
    ("torn cut", "entry-before-cut",
     ["inv.log-matching", "inv.leader-completeness", "inv.state-machine-safety",
      "inv.disk-is-a-held-state"]),
    ("stale acknowledgement", "stale-ack-counted",
     ["inv.leader-completeness", "inv.commits-are-durable", "inv.match-is-durable"]),
]


def directed(schema, native, natives, report):
    """Hand-written schedules: harmless on the specification, rejected on the mutant."""
    schedules, rejecting = directed_schedules(), collections.Counter()
    good = {}
    for label, (schedule, options) in schedules.items():
        good[label] = schedule(native, **options)
        failures = judge(schema, [good[label]])[0][0]
        require(not failures, f"the schedule `{label}` breaks the specification: {failures[:1]}")
    require(all(s.get("post", [0] * 8)[7] == 0 for s in good["figure 8"]["steps"]),
            "the specification commits an entry of an earlier term in the Figure 8 schedule")
    sizes = ", ".join(f"{label} {len(trace['steps'])} steps" for label, trace in good.items())
    report(f"  the specification passes every check on all {len(schedules)} schedules ({sizes})")
    for label, name, needed in DIRECTED:
        schedule, options = schedules[label]
        bad = schedule(natives[name], **options)
        failed = [check_name for check_name, _ in judge(schema, [bad])[0][0]]
        require(all(check_name in failed for check_name in needed),
                f"the schedule `{label}` on {name} fails only {failed}")
        rejecting.update(failed)
        report(f"  {name}, schedule `{label}` ({len(bad['steps'])} steps, replays): fails "
               + ", ".join(failed))
    return rejecting


# ------------------------------------------------------------ messages from non-members

def outsiders(schema, native, directory, report):
    """A message that does not name another member of the cluster as its sender changes
    nothing. No simulated run contains one (the simulator fills in the true sender), so the
    step is handed them directly, in the states in which they would matter."""
    def run(library, state, event):
        after, effects = library.step(schema.encode_state(state), event)
        return sim.freeze((schema.decode_state(after), effects))

    def reach(library, events):
        state = schema.decode_state(library.init(0, 3))
        for event in events:
            state = run(library, state, event)[0]
        return state

    boot = [("restart",), ("loaded", 0, 0, 0, 0, "none", 0, 0, 0)]
    stand = boot + [("alarm", 0), ("written", 1)]
    lead = stand + [("on-vote-rsp", 1, 1, True), ("put", 1, 0, 11), ("written", 2), ("written", 3)]
    states = {"follower": reach(native, boot), "candidate": reach(native, stand),
              "leader": reach(native, lead)}
    require([states[role][2] for role in states] == list(states), "outsiders: setup")
    messages = [("on-vote-req", 9, 1, 1), ("on-vote-rsp", 1, True),
                ("on-app-req", 9, 0, 0, True, 9, "put", 0, 55, 7, 0),
                ("on-app-rsp", 1, True, 1), ("on-app-rsp", 9, True, 1)]
    pairs = {}
    for state in states.values():
        for sender in (-1, 0, 3, 4, 5):         # server 0 of 3: itself, and ids that are no member
            for kind, *fields in messages:
                event = (kind, sender, *fields)
                result = run(native, state, event)
                require(result == (state, ()), f"outsiders: {event} changed {state[2]} {state[0]}: {result}")
                pairs[(state, event)] = result
    check.refinement(schema, native, pairs, nodes=3)
    vote = ("on-vote-rsp", 4, 1, True)
    broken = build(schema, Path(directory) / "outsider", edit(schema.source, OUTSIDER),
                   executable=False)
    elected = run(broken, reach(broken, stand), vote)[0]
    require(elected[2] == "leader", "outsiders: the mutant was expected to be elected by id 4")
    report(f"messages from non-members: {len(pairs)} messages under the ids -1, 0, 3, 4 and 5, "
           f"handed to server 0 of 3 as follower, candidate and leader, change nothing and "
           f"cause no effect, natively and in shen-jit; mutant ({OUTSIDER['what']}): a vote "
           f"under id 4 makes it leader of a cluster of three")


# ------------------------------------------------------------------------------ commands

def main(report=print, record=False):
    """The specification: compiled, simulated, refined, searched, live."""
    schema = Schema(HERE / "node.json")
    count, wrong = lin.self_test()
    require(not wrong, f"the linearizability checker is wrong on {wrong}")
    report(f"linearizability checker: {count} hand-written histories judged correctly, "
           f"6 of them not linearizable")
    with TemporaryDirectory() as tmp:
        native = build(schema, Path(tmp) / "spec")
        for nodes in (3, 5):
            for node in range(nodes):
                require(run_init(native.executable, node, nodes) == native.init(node, nodes),
                        "the executable and the library disagree on the initial state")
        report(f"compiled: {native.stats['ir_nodes']} IR nodes, {native.stats['slots']} slots, "
               f"{native.stats['blocks']} residual blocks, {native.stats['c_lines']} lines of C; "
               f"state {schema.state_width} integers; no part of the node is interpreted")
        outsiders(schema, native, tmp, report)
        pairs, shapes = {}, collections.Counter()
        for label in PROFILES:
            found, counts = campaign(schema, native, label, SEEDS[label], report)
            pairs.update(found)
            shapes.update(counts)
        for history in ("half-synced", "torn", "stale-vote", "stale-ack", "stale-append-entry"):
            require(shapes[history], f"no seeded run contains the history `{history}`")
        started = time.time()
        items = list(pairs.items())
        for first in range(0, len(items), 20000):
            check.refinement(schema, native, dict(items[first:first + 20000]), nodes=5)
        check.refinement(schema, native, {}, nodes=3)
        report(f"refinement: native equals shen-jit on {len(pairs)} distinct (state, event) "
               f"pairs from the seeded runs and on the initial state of every server "
               f"({time.time() - started:.0f}s)")
        report("bounded search (untimed, every schedule; Maude on the translated Shen, the "
               "simulator on the native step):")
        searched = bounded(schema, native, report)
        check.refinement(schema, native, searched, nodes=3)
        started = time.time()
        sample = dict(items[::max(len(items) // 1500, 1)])
        modules = maude.step_module(schema.source)
        check.maude_refinement(modules, schema, {**searched, **sample})
        broken = maude.step_module(mutate(schema.source, "commit-by-counting"))
        try:
            check.maude_refinement(broken, schema, {**searched, **sample})
        except CheckFailure:
            pass
        else:
            raise CheckFailure("the Maude step check accepts a mutated translation")
        report(f"  refinement: native equals shen-jit on the {len(searched)} pairs the searches "
               f"executed; the Maude translation equals native on those and on {len(sample)} "
               f"pairs sampled from the seeded runs, and a mutated translation does not "
               f"({time.time() - started:.0f}s)")
        for label in PROFILES:
            late, partial, _, _ = liveness(schema, native, label, LIVE_SEEDS[label], report)
            require(not late, f"liveness, {label}: not answered within {LIVENESS_BOUND} on seeds {late}")
            require(not partial, f"liveness, {label}: a server is down, in recovery or behind "
                                 f"{LIVENESS_BOUND} after faults stopped on seeds {partial}")
    if record:
        report("record: mutant evidence is recorded one mutant at a time, to keep every "
               "command bounded: python3 -m targets.raft.check hunt <mutant> [three|five]")
    report("raft: ok (the teeth of these checks: python3 -m targets.raft.check mutants)")


def mutants(report=print):
    """The teeth: every mutant against the check that is retained as rejecting it."""
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        native = build(schema, Path(tmp) / "spec", executable=False)
        report("mutants (each rebuilt, its seed re-run, its short trace replayed and rejected):")
        natives, by_seed = check_mutants(schema, native, tmp, report)
        refinement_teeth(schema, native, natives, report)
        report("directed schedules (written by hand, not found by a seed):")
        by_hand = directed(schema, native, natives, report)
        declared = schema.invariants["trace"]
        idle = [name for name in declared if not by_seed[name] + by_hand[name]]
        require(not idle, f"no retained mutant is rejected by {idle}")
        report("  mutants rejected per invariant, on a retained seed and on a directed schedule: "
               + ", ".join(f"{name[4:]} {by_seed[name]}+{by_hand[name]}" for name in declared)
               + f"; linearizability {by_seed['linearizability']}+0")
        report("bounded search, mutants:")
        bounded_mutants(schema, native, natives, report)
        for name, mutant in LIVE_MUTANTS.items():
            stuck = build(schema, Path(tmp) / name, edit(schema.source, mutant), executable=False)
            late, _, _, _ = liveness(schema, stuck, "three", 10)
            require(len(late) == 10, f"the liveness check accepts {name} on {10 - len(late)} seeds")
            report(f"liveness, mutant {name} ({mutant['what']}): no answer within the bound on "
                   f"10 of 10 seeds")
        crashes, late = [], 0
        for seed in range(30):
            where = shape(sim.run(native, Clients(OPS), BUNCHED, seed))[1]
            crashes += where
            late += any(index >= BUNCHED["max_steps"] // 2 for index in where)
        where, spread_out = spread(crashes, late, 30, BUNCHED["max_steps"])
        require(not spread_out, "the spread requirement accepts the rates of the first version")
        report(f"campaign shape, teeth: under the rates of the first version the crashes of 30 "
               f"runs of `three` fall at step index quartiles {where} of {BUNCHED['max_steps']} "
               f"and {late} runs have one in their second half; the spread requirement of the "
               f"campaign rejects that")
        late, partial, _, _ = liveness(schema, natives[LIVE_DEGRADED], "three", 20)
        only = [seed for seed in partial if seed not in late]
        require(len(only) >= 5, f"the liveness check sees {LIVE_DEGRADED} on seeds {only} only")
        report(f"liveness, mutant {LIVE_DEGRADED} ({MUTANTS[LIVE_DEGRADED]['what']}): on 20 "
               f"seeds the put is answered in time on {20 - len(late)}, and on {len(only)} of "
               f"those a server never comes back within the bound")
    report("raft mutants: ok")


def cli(argv):
    if not argv:
        return main()
    schema = Schema(HERE / "node.json")
    if argv[0] == "mutants":
        return mutants()
    if argv[0] == "hunt":
        with TemporaryDirectory() as tmp:
            options = [a for a in argv[2:] if a.startswith("--")]
            labels = tuple(a for a in argv[2:] if not a.startswith("--")) or ("three", "five")
            number = lambda key, default: next(
                (int(o.split("=")[1]) for o in options if o.startswith(f"--{key}=")), default)
            only = next((o.split("=")[1] for o in options if o.startswith("--only=")), None)
            record_mutant(schema, tmp, argv[1], print, labels, only=only,
                          start=number("from", 0), seeds=number("seeds", HUNT_BUDGET))
    elif argv[0] == "story":
        entry = retained()[argv[1]]
        slot = entry["by"][argv[3]] if len(argv) > 3 else entry
        label = argv[2] if len(argv) > 2 else sorted(slot["found"])[-1]
        found = slot["found"][label]
        with TemporaryDirectory() as tmp:
            native = build(schema, Path(tmp) / "m", mutate(schema.source, argv[1]), executable=False)
            trace = sim.script(native, Clients(OPS), PROFILES[label], json.loads(found["script"]))
        print(f"{argv[1]} on {label}: {found['check']}: {found['detail']}")
        print("\n".join(story(trace)))
    else:
        print(__doc__)
        return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
