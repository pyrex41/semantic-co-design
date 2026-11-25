"""Raft on the kernel. Run: nix develop -c python3 -m tool check raft

    python3 -m targets.raft.check                    the retained check (same as above)
    python3 -m targets.raft.check hunt <mutant> [three|five] [--from=N] [--seeds=M] [--only=CHECK]
                                                     search seeds for one mutant, shrink the
                                                     failing trace and retain it in mutants.json
    python3 -m targets.raft.check story <mutant>     print a retained trace, one step per line
"""
import collections
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
from .shrink import shrink
from .workload import Clients, Once, maude_once

HERE = Path(__file__).parent
RETAINED = HERE / "mutants.json"

# The clients' operations. Request numbers are positions, counted from 1.
OPS = (("put", 0, 11), ("get", 0), ("put", 0, 12), ("put", 1, 13), ("get", 0))

# Seeded, timed schedules. Every fault kind has a budget; timers run 80% to 120% of what
# the node asked for, and each node's clock has its own rate on top of that.
FAULTS = {"crash": 3, "drop": 4, "dup": 2, "partition": 2, "oneway": 2}
PROFILES = {
    "three": sim.profile(nodes=3, budget=FAULTS, max_steps=1000, fault_rate=0.02,
                         restart_rate=0.05, delay={"timer": [80, 120]},
                         timer_rate=[100, 90, 115]),
    "five": sim.profile(nodes=5, budget=FAULTS, max_steps=1400, fault_rate=0.02,
                        restart_rate=0.05, delay={"timer": [80, 120]},
                        timer_rate=[100, 90, 115, 105, 95]),
}
SEEDS = {"three": 150, "five": 60}
REPLAY_EVERY = 10
HUNT_BUDGET = 400               # seeds tried per profile before a mutant counts as not found

# Liveness: at virtual time CALM_AT the client ends all faults and starts sending one more
# put. LIVENESS_BOUND is the declared bound on the virtual time from then until its `ok`.
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

# Bounded searches: every schedule of an untimed profile. `timer` bounds how many timer
# events may fire, which is what makes the state space finite. Larger bounds were tried
# and exceed 250,000 states: 3 servers with one timer and a put, 3 servers with two timers.
# 2 servers with one timer, a put and a duplicate is 117,530 states in the simulator and
# is left out for time.
PUT = ("put", 1, 0, 11)
BOUNDS = {
    "2 servers, 2 timers, a put": (2, {"timer": 2}, (PUT,)),
    "2 servers, 1 timer, a put, a crash": (2, {"timer": 1, "crash": 1}, (PUT,)),
    "2 servers, 1 timer, a put, a drop": (2, {"timer": 1, "drop": 1}, (PUT,)),
    "3 servers, 1 timer, a duplicate": (3, {"timer": 1, "dup": 1}, ()),
    "3 servers, 1 timer, a crash": (3, {"timer": 1, "crash": 1}, ()),
}
SEARCH_MUTANTS = {"vote-before-durable": "2 servers, 1 timer, a put, a crash",
                  "ack-before-durable": "2 servers, 1 timer, a put, a crash"}

KEEP = ("boot", "restart", "deliver", "local", "crash", "sync", "error")
WITNESSES = ["wit.leader-change", "wit.conflict-repaired", "wit.log-recovered", "wit.committed",
             "wit.full", "wit.retry-skipped"]

# Each mutant is one edit of spec.shen. `expect` lists the checks of which at least one
# must reject it; the retained evidence in mutants.json says which one did, on which seed.
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
}
REQUIRED = ["vote-without-log-check", "commit-by-counting", "vote-before-durable", "local-get"]


def mutate(source, name):
    mutant = MUTANTS[name]
    require(source.count(mutant["old"]) == 1, f"mutation site of {name} is not unique")
    return source.replace(mutant["old"], mutant["new"])


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

def campaign(schema, native, label, seeds, report, *, replay=True):
    """Seeded runs of one profile, each checked as it is produced. Returns the pairs seen."""
    prof, workload = PROFILES[label], Clients(OPS)
    pairs, kinds, seen = {}, collections.Counter(), [0] * len(WITNESSES)
    answered, steps, replayed, cuts, lost, overtaken = collections.Counter(), 0, 0, 0, 0, 0
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
            overtaken += reordered(trace)
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
    require(all(kinds[k] for k in needed) and lost and overtaken,
            f"{label}: a fault kind never happened: {dict(kinds)}, {lost} writes lost, "
            f"{overtaken} messages overtaken")
    report(f"{label}: {prof['nodes']} servers, {seeds} seeds, {steps} steps, "
           f"{time.time() - started:.0f}s; every run ends max_steps with no trap; "
           f"{replayed} traces replayed step by step and re-run from their seed, byte-identical")
    report(f"  faults: " + ", ".join(f"{kinds[k]} {k}" for k in needed)
           + f"; {lost} issued writes never reached the disk; {overtaken} AppendEntries arrived "
             f"after a later one from the same leader; timers skewed 80-120% on clocks at "
             f"{prof['timer_rate']}%")
    report(f"  the {len(schema.invariants['trace'])} invariants hold on every trace and every "
           f"history is linearizable; operations "
           f"answered per run {dict(sorted(answered.items()))}; witnesses "
           + ", ".join(f"{name[4:]} {count}" for name, count in zip(WITNESSES, seen)))
    return pairs, dict(zip(WITNESSES, seen))


def reordered(trace):
    """How many AppendEntries were handled after one that the same leader sent later in the
    same term: reordering, or a late duplicate. A leader's commit index never decreases
    within its term, so a smaller leaderCommit than one already seen was sent earlier."""
    newest, count = {}, 0
    for step in trace["steps"]:
        event = step.get("event") or []
        if step["kind"] == "deliver" and event[:1] == ["on-app-req"]:
            key = (step["node"], event[1], event[2])
            count += event[11] < newest.get(key, 0)
            newest[key] = max(newest.get(key, 0), event[11])
    return count


def hunt(schema, native, label, names, budget, *, start=0, batch=10):
    """The first seed of a profile, from `start`, on which one of `names` fails. Returns
    (seed, trace, check, detail) or None when seed `budget` is reached."""
    prof, workload = PROFILES[label], Clients(OPS)
    for first in range(start, budget, batch):
        traces = [sim.run(native, workload, prof, seed) for seed in range(first, min(first + batch, budget))]
        for trace, (failures, _) in zip(traces, judge(schema, traces)):
            failures = [f for f in failures if names is None or f[0] in names]
            if failures:
                return trace["seed"], trace, failures[0][0], failures[0][1]
    return None


def record_mutant(schema, directory, name, report, labels=("three", "five"), *, only=None,
                  start=0, seeds=HUNT_BUDGET):
    """Search seeds `start` to `start + seeds`, shrink and retain the evidence for one mutant.

    By default the search is for the first failure of any published property (`paper`).
    With `only` it is for a failure of that one check, retained separately under its
    name: the commit rule, or a property that is slower to fail than the one found first.
    A search that starts above 0 continues an earlier one that found nothing, so the
    count of seeds tried stays the number of seeds from 0.
    """
    source = mutate(schema.source, name)
    native = build(schema, Path(directory) / name, source, executable=False)
    retained = json.loads(RETAINED.read_text()) if RETAINED.exists() else {}
    entry = retained.get(name, {})
    entry["what"] = MUTANTS[name]["what"]
    slot = entry if only is None else entry.setdefault("by", {}).setdefault(only, {})
    for label in labels:
        tried = slot.setdefault("not_found", {}).get(label, {}).get("seeds_tried", 0)
        require(start <= tried, f"{name} on {label}: seeds {tried} to {start} were never tried")
        slot.setdefault("found", {}).pop(label, None)
        slot["not_found"].pop(label, None)
        started = time.time()
        found = hunt(schema, native, label, [only] if only else paper(schema), start + seeds,
                     start=start)
        if found is None:
            slot["not_found"][label] = {"seeds_tried": start + seeds}
            report(f"{name} on {label}: no failure of {only or 'any published property'} in "
                   f"seeds 0 to {start + seeds - 1} ({time.time() - started:.0f}s for the last "
                   f"{seeds})")
            continue
        seed, trace, failed, detail = found
        report(f"{name} on {label}: {failed} fails on seed {seed} after {seed + 1} seeds "
               f"({time.time() - started:.0f}s); shrinking {len(trace['steps'])} steps")
        workload = Clients(OPS)
        fails = lambda t: any(f[0] == failed for f in judge(schema, [t], names=[failed])[0][0])
        short, tried = shrink(native, workload, trace, fails)
        sim.replay(native, workload, short)
        detail = judge(schema, [short], names=[failed])[0][0][0][1]
        slot["found"][label] = {
            "seed": seed, "seeds_tried": seed + 1, "check": failed, "steps": len(trace["steps"]),
            "digest": sim.digest(trace), "short_steps": len(short["steps"]),
            "short_digest": sim.digest(short), "detail": text(detail)[:400],
            "script": [[s["action"], s["draws"]] for s in short["steps"] if s["action"] != ["boot"]]}
        report(f"  shrunk to {len(short['steps'])} steps in {tried} candidates "
               f"({time.time() - started:.0f}s)")
    retained[name] = entry
    RETAINED.write_text(json.dumps(retained, indent=1, sort_keys=True) + "\n")
    return entry


RULE = ["inv.commit-rule"]


def paper(schema):
    """The checks that state published properties: everything but the commit rule."""
    return [n for n in schema.invariants["trace"] if n not in RULE] + ["no-trap", "linearizability"]


def check_mutants(schema, native, directory, report):
    """Every retained piece of evidence still holds against a freshly built mutant, and
    the specification passes where the mutant fails."""
    retained = json.loads(RETAINED.read_text())
    require(set(retained) == set(MUTANTS), "mutants.json does not cover the declared mutants: "
                                           "run `python3 -m targets.raft.check hunt <name>`")
    workload, natives = Clients(OPS), {}
    for name in MUTANTS:
        entry = retained[name]
        broken = build(schema, Path(directory) / name, mutate(schema.source, name), executable=False)
        natives[name] = broken
        groups = [("", entry)] + [(f"{check_name}: ", slot)
                                  for check_name, slot in entry.get("by", {}).items()]
        notes, evidence = [], 0
        for prefix, slot in groups:
            for label, found in slot.get("found", {}).items():
                prof = PROFILES[label]
                trace = sim.run(broken, workload, prof, found["seed"])
                require(sim.digest(trace) == found["digest"],
                        f"{name}: seed {found['seed']} of {label} no longer gives the retained trace")
                require(judge(schema, [trace], names=[found["check"]])[0][0],
                        f"{name}: {found['check']} accepts seed {found['seed']} of {label}")
                short = sim.script(broken, workload, prof, found["script"])
                require(sim.digest(short) == found["short_digest"],
                        f"{name}: the retained short trace does not reproduce")
                sim.replay(broken, workload, short)
                require(judge(schema, [short], names=[found["check"]])[0][0],
                        f"{name}: {found['check']} accepts the retained short trace")
                # The same seed, and the same short schedule where it is one, on the
                # specification: nothing fails.
                same = [sim.run(native, workload, prof, found["seed"])]
                try:
                    same.append(sim.script(native, workload, prof, found["script"]))
                except sim.Divergence:
                    pass
                require(not any(failures for failures, _ in judge(schema, same)),
                        f"{name}: the specification fails on seed {found['seed']} of {label}")
                evidence += 1
                notes.append(f"{prefix}{label} seed {found['seed']}, {found['seeds_tried']} seeds"
                             + ("" if prefix else f", {found['check']}")
                             + f", shrunk to {found['short_steps']} steps")
            for label, missing in slot.get("not_found", {}).items():
                notes.append(f"{prefix}{label} NOT FOUND in {missing['seeds_tried']} seeds")
        require(name not in REQUIRED or evidence,
                f"{name} is required and no evidence against it is retained")
        report(f"  {name}: " + "; ".join(notes))
    return natives


def refinement_teeth(schema, natives, pairs, report):
    """A library compiled from an edited specification does not pass the refinement check
    against the unedited Shen source."""
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

def settle(trace, request):
    """(virtual time faults stopped, virtual time the final put was answered), or None."""
    calm = answered = None
    for step in trace["steps"]:
        if step["kind"] == "client" and step["action"][1] == "calm":
            calm = step["t"]
        elif (step["kind"] == "output" and step["event"][:3] == ["reply", request, "ok"]
              and answered is None):
            answered = step["t"]
    return calm, answered


def liveness(schema, native, label, seeds, report=None):
    """After faults stop, the final put is answered within LIVENESS_BOUND. Returns the
    seeds on which it was not, and the times it took on the others."""
    prof = sim.profile(**{**PROFILES[label], "max_steps": LIVE_STEPS[label],
                          "budget": {**PROFILES[label]["budget"], "dup": 1}})
    workload = Clients(LIVE_OPS, sends=1, calm_at=CALM_AT, final=FINAL)
    late, times = [], []
    for seed in range(seeds):
        trace = sim.run(native, workload, prof, seed)
        require(not trace["end"].startswith("error"), f"liveness {label} seed {seed}: {trace['end']}")
        problem = lin.check(trace)
        require(problem is None, f"liveness {label} seed {seed}: {problem}")
        calm, answered = settle(trace, len(LIVE_OPS) + 1)
        require(calm is not None, f"liveness {label} seed {seed}: faults never stopped")
        if answered is None or answered - calm > LIVENESS_BOUND:
            late.append(seed)
        else:
            times.append(answered - calm)
    if report:
        report(f"liveness, {label}: faults stop at virtual time {CALM_AT} on {seeds} seeds; the put "
               f"sent then is answered on {len(times)} of them, after {min(times)} to {max(times)} "
               f"time units (median {sorted(times)[len(times) // 2]}); declared bound {LIVENESS_BOUND}")
    return late, times


# -------------------------------------------------------------------------- bounded search

def unsafe(observation):
    """A reason why one reachable world is wrong, or None. Stated over what an observation
    holds: the memory of every server, the durable rows of every disk, the replies that got
    out. These restate, per world, what the Shen invariants say over histories."""
    states = {t[1]: t[2] for t in observation if t[0] == "state"}
    rows = {t[1]: {row[0]: row[1:] for row in t[2]} for t in observation if t[0] == "rows"}
    running = {n: s for n, s in states.items() if s != "down" and s[2] != "loading"}
    majority = len(states) // 2 + 1
    meta = {n: (r[0][0], r[0][2], r[0][3]) if 0 in r else (0, -1, 0) for n, r in rows.items()}
    leaders = [(s[3], n) for n, s in running.items() if s[2] == "leader"]
    if len({term for term, _ in leaders}) != len(leaders):
        return f"two leaders in one term: {leaders}"
    for term, leader in leaders:
        voters = sum(1 for t, voted, _ in meta.values() if t > term or (t == term and voted == leader))
        if voters < majority:
            return f"server {leader} leads term {term} with {voters} durable votes"
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


def bounded(schema, native, directory, report):
    pairs, reference = {}, {}
    for label in BOUNDS:
        started = time.time()
        mine, reached, facts = search(schema, native, label)
        require(facts["maude_trap"] is None, f"{label}: a reachable step has no result")
        require(mine == reached, f"{label}: Maude and the simulator disagree: {len(reached - mine)} "
                                 f"observations only in Maude, {len(mine - reached)} only simulated")
        bad = [reason for reason in map(unsafe, reached) if reason]
        require(not bad, f"{label}: {bad[:1]}")
        pairs.update(facts["pairs"])
        reference[label] = reached
        leaders = sum(1 for o in reached if any(t[0] == "state" and t[2] != "down"
                                                and t[2][2] == "leader" for t in o))
        answered = sum(1 for o in reached if any(t[0] == "got" for t in o))
        report(f"  {label}: Maude {facts['maude_states']} states, simulator {facts['sim_states']}; "
               f"the same {len(reached)} observations, {leaders} with a leader, {answered} with "
               f"the put answered; all safe ({time.time() - started:.0f}s)")
    for name, label in SEARCH_MUTANTS.items():
        source = mutate(schema.source, name)
        broken = build(schema, Path(directory) / f"search-{name}", source, executable=False)
        mine, reached, facts = search(schema, broken, label, source=source)
        bad = sorted({reason for reason in map(unsafe, reached) if reason})
        require(mine == reached and bad and reached != reference[label],
                f"the bounded search does not reject {name}")
        report(f"  mutant {name}, {label}: Maude and the simulator agree on {len(reached)} "
               f"observations, {sum(1 for o in reached if unsafe(o))} of them unsafe ({bad[0]})")
    return pairs


def figure8(schema, native, directory, report):
    """The directed Figure 8 schedule: harmless on the specification, rejected on a node
    that commits by counting replicas."""
    from .figure8 import schedule
    good = schedule(native)
    require(not judge(schema, [good])[0][0], "the Figure 8 schedule breaks the specification")
    require(all(s.get("post", [0] * 8)[7] == 0 for s in good["steps"]),
            "the specification commits an entry of an earlier term in the Figure 8 schedule")
    broken = build(schema, Path(directory) / "figure8", mutate(schema.source, "commit-by-counting"),
                   executable=False)
    bad = schedule(broken)
    failed = [name for name, _ in judge(schema, [bad])[0][0]]
    needed = ["inv.leader-completeness", "inv.state-machine-safety", "inv.commit-rule"]
    require(all(name in failed for name in needed),
            f"the Figure 8 schedule on commit-by-counting fails only {failed}")
    report(f"  commit-by-counting, directed schedule (written by hand after Figure 8, not found by "
           f"a seed; {len(bad['steps'])} steps, replays): the specification commits nothing in "
           f"it; the mutant fails {', '.join(failed)}")
    from .figure8 import lost_vote
    good = lost_vote(native)
    require(not judge(schema, [good])[0][0], "the lost-vote schedule breaks the specification")
    broken = build(schema, Path(directory) / "lost-vote", mutate(schema.source, "vote-before-durable"),
                   executable=False)
    bad = lost_vote(broken)
    failed = [name for name, _ in judge(schema, [bad])[0][0]]
    require("inv.election-safety" in failed and "inv.replies-are-durable" in failed,
            f"the lost-vote schedule on vote-before-durable fails only {failed}")
    report(f"  vote-before-durable, directed schedule (written by hand, not found by a seed; "
           f"{len(bad['steps'])} steps, replays): the specification elects one leader in it; the "
           f"mutant fails {', '.join(failed)}")


def main(report=print, record=False):
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
        pairs = {}
        for label in PROFILES:
            found, _ = campaign(schema, native, label, SEEDS[label], report)
            pairs.update(found)
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
        searched = bounded(schema, native, tmp, report)
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
            late, _ = liveness(schema, native, label, LIVE_SEEDS[label], report)
            require(not late, f"liveness, {label}: not answered within {LIVENESS_BOUND} on seeds {late}")
        for name, mutant in LIVE_MUTANTS.items():
            source = schema.source
            require(source.count(mutant["old"]) == 1, f"mutation site of {name}")
            stuck = build(schema, Path(tmp) / name, source.replace(mutant["old"], mutant["new"]),
                          executable=False)
            late, _ = liveness(schema, stuck, "three", 10)
            require(len(late) == 10, f"the liveness check accepts {name} on {10 - len(late)} seeds")
            report(f"  mutant {name} ({mutant['what']}): no answer within the bound on 10 of 10 seeds")
        if record:
            report("record: mutant evidence is recorded one mutant at a time, to keep every "
                   "command bounded: python3 -m targets.raft.check hunt <mutant> [three|five]")
        report("mutants (each rebuilt, its seed re-run, its short trace replayed and rejected):")
        natives = check_mutants(schema, native, tmp, report)
        refinement_teeth(schema, natives, pairs, report)
        figure8(schema, native, tmp, report)
    report("raft: ok")


def cli(argv):
    if not argv:
        return main()
    schema = Schema(HERE / "node.json")
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
        found = json.loads(RETAINED.read_text())[argv[1]]["found"]
        label = argv[2] if len(argv) > 2 else sorted(found)[-1]
        with TemporaryDirectory() as tmp:
            native = build(schema, Path(tmp) / "m", mutate(schema.source, argv[1]), executable=False)
            trace = sim.script(native, Clients(OPS), PROFILES[label], found[label]["script"])
        print(f"{argv[1]} on {label}: {found[label]['check']}: {found[label]['detail']}")
        print("\n".join(story(trace)))
    else:
        print(__doc__)
        return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
