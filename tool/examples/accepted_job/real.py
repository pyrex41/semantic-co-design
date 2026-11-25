"""AcceptedJob as real processes. Run: nix develop -c python3 -m tool real accepted_job

The compiled node runs under tool/host on loopback. A client submits and
retries; the node is killed with kill -9 at random moments and at chosen points
inside the host, and restarted. Afterwards tool/host/conform.py judges the run
from the files the processes left.
"""
from pathlib import Path
import random
import shutil
from tempfile import TemporaryDirectory

from ...check import CheckFailure, require
from ...host import Client, Cluster, bundle, wal
from ...host.conform import conform
from ...schema import Schema
from ...host import LINE
from .check import MUTANTS, mutate
from .workload import DIGESTS, KEY

HERE = Path(__file__).parent
RUNS, SUBMISSIONS, MUTANT_RUNS = 20, 10, 8
POINTS = ("log-torn", "logged", "effect", "disk-torn", "synced")
KEYS = (KEY, KEY + 1)
FLOODS = (("a line of 200000 brackets", b"[" * 200000 + b"\n"),
          (f"{64 * LINE} bytes without an end of line", b"x" * LINE))


def run(packed, directory, seed):
    """One seeded run of one node. Returns what the client saw and how the node died."""
    rng = random.Random(seed)
    cluster = Cluster(packed, directory)
    client = Client(cluster.clients[0])
    facts = {"sent": {}, "kills": 0, "points": {}, "torn": [], "data": cluster.data}
    armed = [None]

    def boot(point=None):
        # The first incarnation and half of the later ones carry a kill -9 at a point inside
        # the host: its n-th log record, or its first disk write or multi-effect step.
        point = point or (rng.choice(POINTS) if rng.random() < 0.5 else None)
        armed[0] = point
        cluster.start(0, point and f"{point}:{rng.randint(3, 7) if point in POINTS[:2] else 1}")

    def buried(external):
        """The process is dead: count how, and look at what a kill left before it is repaired."""
        how = "kill -9" if external else armed[0]
        facts["points"][how] = facts["points"].get(how, 0) + 1
        facts["kills"] += 1
        if any(wal.read(cluster.data[0] / name)[1] for name in ("events.log", "disk.log")):
            copy = Path(directory) / f"torn{len(facts['torn'])}"
            shutil.copytree(cluster.data[0], copy)
            facts["torn"].append(copy)

    try:
        boot(POINTS[seed % len(POINTS)])
        request = 0
        for _ in range(SUBMISSIONS):
            key, digest = rng.choice(KEYS), rng.choice(DIGESTS)
            for _ in range(40):
                if cluster.dead(0):
                    cluster.kill(0)
                    buried(False)
                    boot()
                doomed = rng.random() < 0.12
                facts["sent"][request] = (key, digest)
                client.send(("submit", request, key, digest))
                if doomed:
                    # Somewhere between the request leaving and its reply arriving.
                    client.wait(lambda o: False, rng.uniform(0, 0.004))
                    buried(cluster.kill(0))
                    boot()
                reply = client.wait(lambda o: o[:2] == ("reply", request), 0.3)
                request += 1
                if reply:
                    break
            else:
                raise CheckFailure(f"seed {seed}: a submission was never answered")
    finally:
        cluster.stop()
        client.close()
    facts["got"] = client.got
    return facts


def judge(schema, packed, facts, source_file=None):
    """conform.py on the files, then the client's own view against them."""
    found = conform(schema, facts["data"], source_file=source_file, module=packed["wasm"])
    logged = {tuple(e) for r in wal.read(facts["data"][0] / "events.log")[0]
              for e in r.get("effects", [])}
    rows = {tuple(row[2:]) for row in wal.read(facts["data"][0] / "disk.log")[0]}
    for reply in facts["got"]:
        require(reply in logged, f"the client received {reply}, which the log does not hold")
        _, request, status, receipt = reply
        if status == "ok":
            require((receipt, *facts["sent"][request]) in rows,
                    f"the client was told {reply} and the row is not in the durable file")
    return found


def specification(work, schema, report):
    packed = bundle(schema, work / "spec")
    total = {"kills": 0, "events": 0, "segments": 0, "replies": 0, "requests": 0, "pairs": 0,
             "torn": 0, "lost_writes": 0}
    points, kept = {}, None
    for seed in range(RUNS):
        facts = run(packed, work / f"run{seed}", seed)
        found = judge(schema, packed, facts)
        require(not found["violations"], f"seed {seed}: invariants violated: {found['violations']}")
        require(found["segments"] == facts["kills"] + 1, f"seed {seed}: segments and kills differ")
        # A kill can leave a torn record. conform.py must accept the files as they were then.
        for copy in facts["torn"]:
            torn = conform(schema, [copy], module=packed["wasm"])
            require(torn["torn"] + torn["torn_rows"] and not torn["violations"],
                    f"seed {seed}: files with a torn tail were not accepted")
            total["torn"] += 1
        for name in ("kills", "events", "segments", "pairs", "lost_writes"):
            total[name] += {**facts, **found}[name]
        total["replies"] += len(facts["got"])
        total["requests"] += len(facts["sent"])
        for how, count in facts["points"].items():
            points[how] = points.get(how, 0) + count
        if kept is None and found["segments"] > 2 and found["rows"]:
            kept = facts
    require(set(points) == {"kill -9", *POINTS}, f"real runs: a way to die never happened: {points}")
    require(total["torn"] and kept, "real runs: no torn tail, or no run to edit")
    report(f"real runs: {RUNS} runs on loopback, {total['requests']} requests, "
           f"{total['replies']} replies received, {total['kills']} kill -9 "
           f"({dict(sorted(points.items()))}), {total['segments']} log segments")
    report(f"conformance: {total['events']} logged events replayed through shen-jit "
           f"({total['pairs']} distinct pairs over the runs), identical state and effects at "
           f"every one; every event traced to its cause; invariants hold on every run; "
           f"{total['torn']} torn tails read as they were left; "
           f"{total['lost_writes']} writes lost with their process")
    return packed, kept


def edited(directory, work, name, change):
    """A copy of a node's files with `change` applied to the records of one of them."""
    copy = work / "edited"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(directory, copy)
    records = wal.read(copy / name)[0]
    data = change(records)
    (copy / name).write_bytes(data if isinstance(data, bytes) else b"".join(map(wal.frame, records)))
    return copy


def teeth(work, schema, packed, kept, report):
    """Edited files of a run that passed must be rejected, each for its own reason."""
    records = wal.read(kept["data"][0] / "events.log")[0]
    write = next(r for r in records if any(e[0] == "write" for e in r.get("effects", [])))
    reply = next(r for r in records if any(e[0] == "reply" for e in r.get("effects", [])))
    acked = next(r for r in records if r.get("event", [0])[0] == "written")
    final = wal.frame(records[-1])

    def state(rs): rs[write["seq"]]["post"][2] += 1
    def effect(rs): rs[reply["seq"]]["effects"][0][3] += 1
    def event(rs): rs[write["seq"]]["event"][3] += 1
    def cause(rs): rs[acked["seq"]]["cause"] = None
    def removed(rs): del rs[acked["seq"]]
    def row(rs): rs.append([rs[-1][0], rs[-1][1] + 1, 1, KEY, 33])
    def byte(rs): return b"".join(map(wal.frame, rs)).replace(b'"post"', b'"past"', 1)
    def middle(rs): return b"".join(map(wal.frame, rs)).replace(final, final[:-9] + b"\n")
    def number(rs): rs[0] = 5
    def nested(rs): rs[acked["seq"]]["cause"] = [[1], [2]]
    def text(rs): rs[acked["seq"]]["cause"] = [1, "x"]
    def start(rs): rs[0]["boot"]["init"] = 5
    def scalar(rs): rs[0] = 5

    cases = [("a state edited", "events.log", state, "refinement"),
             ("an effect edited", "events.log", effect, "refinement"),
             ("an event edited", "events.log", event, "refinement"),
             ("a cause edited", "events.log", cause, "causality"),
             ("a record removed", "events.log", removed, "log"), ("a byte changed", "events.log", byte, "log"),
             ("a short record", "events.log", middle, "log"), ("a row added", "disk.log", row, "causality"),
             # Records with a good checksum and no form: a failure of the check, not of Python.
             ("a record that is a number", "events.log", number, "log"),
             ("a cause of lists", "events.log", nested, "log"), ("a cause of text", "events.log", text, "log"),
             ("an initial state that is a number", "events.log", start, "log"),
             ("a row that is a number", "disk.log", scalar, "log")]
    caught = []

    def rejected(label, reason, directories, **given):
        try:
            conform(schema, directories, module=packed["wasm"], **given)
        except CheckFailure as failure:
            require(str(failure).startswith(reason), f"teeth: {label} rejected for another reason: {failure}")
            caught.append(f"{label} ({reason})")
        else:
            raise CheckFailure(f"teeth: files with {label} were accepted")

    for label, name, change, reason in cases:
        rejected(label, reason, [edited(kept["data"][0], work, name, change)])
    # No log is not a log that conforms: a directory that is not there, and an empty one.
    (work / "empty").mkdir()
    rejected("no directory", "log", [work / "nowhere"])
    rejected("no records", "log", [work / "empty"])
    # What the client received conforms as received, and not with one output once more.
    got = [list(output) for output in kept["got"]]
    require(got and conform(schema, kept["data"], module=packed["wasm"], received={0: got})["received"],
            "teeth: the run to edit has no outputs")
    rejected("an output received once more than it was emitted", "causality", kept["data"],
             received={0: got + got[:1]})
    rejected("an output never emitted", "causality", kept["data"],
             received={0: got + [["reply", 9999, "ok", 0]]})
    # A torn final record is not an edit: the same files cut inside the last record conform,
    # with one event fewer.
    whole = conform(schema, kept["data"], module=packed["wasm"])
    cut = conform(schema, [edited(kept["data"][0], work, "events.log",
                                  lambda rs: b"".join(map(wal.frame, rs))[:-9])], module=packed["wasm"])
    require(cut["torn"] == 1 and cut["events"] == whole["events"] - 1 and not cut["violations"],
            "teeth: a torn final record was not dropped")
    report(f"log teeth: one run's files rejected with {', '.join(caught)}; cut inside the final "
           f"record they conform with {cut['events']} of {whole['events']} events")


def hostile(work, schema, packed, report):
    """Lines that must cost the host nothing: each is followed, on the same connection, by a
    request that must be answered, and nothing but those requests may reach the log."""
    lines = [("a number outside 64 bits", b'["submit", 5, 8, 9223372036854775808]'),
             ("a number below 64 bits", b'["submit", 5, -9223372036854775809, 22]'),
             ("nesting deeper than the parser's stack", b"[" * (LINE - 1)),
             ("events only the host may make", b'["written", 0]\n["restart"]\n["loaded", 0, 0, 1, 7, 99]'),
             ("ill-typed fields", b'["submit", 5.0, true, 22]\n["submit", 5, 8]\n{"a": 1}\n5\nnull\n[["submit"], 1, 2, 3]')]
    cluster = Cluster(packed, work / "hostile")
    cluster.start(0)
    client, sent = Client(cluster.clients[0]), []

    def answered(label):
        sent.append(("submit", len(sent), KEY, DIGESTS[0]))
        client.send(sent[-1])
        require(client.wait(lambda o: o[:2] == ("reply", len(sent) - 1), 10) and not cluster.dead(0),
                f"hostile input: the host did not survive {label}")

    try:
        answered("nothing")
        for label, line in lines:
            client.conn.sendall(line + b"\n")
            answered(label)
        # More than a line can hold, with an end or without: the host stops reading that
        # sender, whose earlier request counts, and goes on serving the others.
        for label, flood in FLOODS:
            sender = Client(cluster.clients[0])
            sender.send(sent[0])
            try:
                for _ in range(64):
                    sender.conn.sendall(flood)
            except OSError:
                pass
            sender.wait(lambda o: False, 10)
            require(sender.conn is None, f"hostile input: the host kept reading {label}")
            answered(label)
    finally:
        cluster.stop()
        client.close()
    events = [tuple(r["event"]) for r in wal.read(cluster.data[0] / "events.log")[0]
              if schema.events[r.get("event", ["restart"])[0]].role == "request"]
    require(sorted(events) == sorted([sent[0]] * len(FLOODS) + sent), f"hostile input: the log holds requests nobody sent: {events}")
    found = conform(schema, cluster.data, module=packed["wasm"], received={0: client.got})
    require(not found["violations"] and found["segments"] == 1, f"hostile input: {found['violations']}")
    report(f"hostile input: the host survives and logs nothing for {', '.join(l for l, _ in lines)}; "
           f"it drops the sender of {' and of '.join(l for l, _ in FLOODS)}")


def short(work, schema, packed, report):
    """A write that comes back short is a torn tail: the process must stop there, before the
    record's effects leave it. The fault is a real one, a limit on the size of the file,
    set inside the record that carries the reply."""
    request = ("submit", 0, KEY, DIGESTS[0])

    def one(name, fsize=None):
        cluster = Cluster(packed, work / name)
        cluster.start(0, fsize=fsize)
        client = Client(cluster.clients[0])
        try:
            client.send(request)
            reply = client.wait(lambda o: o[:2] == ("reply", 0), 10 if fsize is None else 2)
            if fsize is not None:
                cluster.procs[0].wait(timeout=30)
        finally:
            cluster.stop()
            client.close()
        return cluster.data, client.got, reply

    data, _, reply = one("whole")
    require(reply, "short write: no reply without the limit")
    records = wal.read(data[0] / "events.log")[0]
    sizes = [len(wal.frame(r)) for r in records]
    at = next(r["seq"] for r in records if any(e[0] == "reply" for e in r.get("effects", [])))
    data, got, reply = one("short", sum(sizes[:at]) + sizes[at] // 2)
    left, torn = wal.read(data[0] / "events.log")
    require(left == records[:at] and torn == sizes[at] // 2,
            "short write: the limit did not fall inside the record that carries the reply")
    require(not reply and not got, f"short write: {got} left a process whose record is a torn tail")
    found = conform(schema, data, module=packed["wasm"], received={0: got})
    require(found["torn"] == 1 and not found["violations"], "short write: the files do not conform")
    report(f"short write: with events.log limited to {sum(sizes[:at]) + sizes[at] // 2} bytes the "
           f"host stops at record {at}, half written; its reply never reaches the client")


def mutants(work, schema, report):
    """Each mutant, running for real, is rejected twice: its log does not refine spec.shen,
    and judged against its own source the run violates the invariant named for it."""
    for name, mutant in MUTANTS.items():
        source = work / f"{name}.shen"
        source.write_text(mutate(schema.source, mutant))
        packed = bundle(schema, work / name, source=source.read_text())
        refined = None
        for seed in range(MUTANT_RUNS):
            facts = run(packed, work / f"{name}{seed}", seed)
            try:
                conform(schema, facts["data"], module=packed["wasm"])
            except CheckFailure as failure:
                require(str(failure).startswith("refinement"), f"{name}: {failure}")
                refined = seed if refined is None else refined
            try:
                names = [n for n, _ in judge(schema, packed, facts, source)["violations"]]
            except CheckFailure as failure:
                require("the client" in str(failure), f"{name}: {failure}")
                names = ["the client's view"]
            if refined is not None and mutant["invariant"] in names:
                break
        else:
            raise CheckFailure(f"mutant {name}: {MUTANT_RUNS} real runs did not show both rejections")
        report(f"mutant {name}: refinement against spec.shen rejects the log of run {refined}; "
               f"{mutant['invariant']} fails on run {seed} ({facts['kills']} kill -9)")


def main(report=print):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        packed, kept = specification(work, schema, report)
        teeth(work, schema, packed, kept, report)
        hostile(work, schema, packed, report)
        short(work, schema, packed, report)
        mutants(work, schema, report)
    report("accepted_job on the real host: ok")
