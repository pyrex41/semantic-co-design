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
from .check import MUTANTS, mutate
from .workload import DIGESTS, KEY

HERE = Path(__file__).parent
RUNS, SUBMISSIONS, MUTANT_RUNS = 20, 10, 8
POINTS = ("log-torn", "logged", "effect", "disk-torn", "synced")
KEYS = (KEY, KEY + 1)


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

    cases = [("a state", "events.log", state, "refinement"), ("an effect", "events.log", effect, "refinement"),
             ("an event", "events.log", event, "refinement"), ("a cause", "events.log", cause, "causality"),
             ("a record removed", "events.log", removed, "log"), ("a byte", "events.log", byte, "log"),
             ("a short record", "events.log", middle, "log"), ("a row added", "disk.log", row, "causality")]
    caught = []
    for label, name, change, reason in cases:
        try:
            conform(schema, [edited(kept["data"][0], work, name, change)], module=packed["wasm"])
        except CheckFailure as failure:
            require(str(failure).startswith(reason), f"teeth: {label} rejected for another reason: {failure}")
            caught.append(f"{label} ({reason})")
        else:
            raise CheckFailure(f"teeth: files with {label} edited were accepted")
    # A torn final record is not an edit: the same files cut inside the last record conform,
    # with one event fewer.
    whole = conform(schema, kept["data"], module=packed["wasm"])
    cut = conform(schema, [edited(kept["data"][0], work, "events.log",
                                  lambda rs: b"".join(map(wal.frame, rs))[:-9])], module=packed["wasm"])
    require(cut["torn"] == 1 and cut["events"] == whole["events"] - 1 and not cut["violations"],
            "teeth: a torn final record was not dropped")
    report(f"log teeth: one run's files rejected with {', '.join(caught)}; cut inside the final "
           f"record they conform with {cut['events']} of {whole['events']} events")


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
        mutants(work, schema, report)
    report("accepted_job on the real host: ok")
