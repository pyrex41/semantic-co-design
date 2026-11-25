"""Three Raft servers as three processes, with the harness as the network between them.

    nix develop -c python3 -m tool check solo5-cluster-host   host processes (macOS or Linux)
    nix develop -c python3 -m tool check solo5-cluster        that, and unikernels under solo5-spt

Each scenario of tool/solo5/cluster_scenarios.py is run RUNS times. Every record is
checked node by node against the compiled step, turned into one simulator trace
(tool/solo5/cluster_compare.py), and that trace is judged by the Raft target's own
checks: the fourteen Shen invariants of targets/raft/invariants.shen and the
linearizability checker targets/raft/lin.py, through targets.raft.check.judge. The
mutant images must be rejected; one that is not is reported as NOT FOUND, and the check
fails only if a mutant listed in REQUIRED is not rejected for its own defect.

A run is not a function of its seed: timers run on the machine's clock. What is checked
is the record of the run that happened.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

from .. import check
from ..check import CheckFailure, require
from ..compile import build
from ..schema import Schema
from . import cluster, cluster_compare, container, emit
from .check import CLANG, HERE, sha
from .cluster_scenarios import (DRIVERS, EXPECT, IMAGES, MUTANT_RUNS, NODE, NODES, SCENARIOS, SPECS,
                                source)
from .compare import Mismatch

RUNS = 5
# The mutants this check requires to be rejected, each with a phrase of the message that
# names its defect. For a driver mutant the phrase is the comparison's; for an edit of
# Raft it is the name of the check of targets/raft that fails on the trace of a real run.
REQUIRED = {
    "raft~delivers-any-address": "a frame addressed to node",
    "raft~sends-to-next-node": "on the node's link is",
    "raft~claims-another-sender": "on the node's link is",
    "raft~swallows-send": "frames on its link",
    "raft~recovery-forgets-vote": "inv.recovery-restores-the-disk",
}
SOURCES = ("cluster.py", "cluster_compare.py", "cluster_scenarios.py", "run.py", "compare.py",
           "emit.py", "driver.c", "host.c")
CLUSTER_SCRIPT = ("set -eu; nix-build /src/tool/solo5/nix/pinned.nix -A solo5-cluster-run "
                  "-o /tmp/run >/dev/null; /tmp/run/bin/sim-tool-solo5-cluster-run /out %d")
CLUSTER_TIMEOUT = 1700


def prepare(work):
    """The simulator's library for the node and for each edit of it, and every image as a
    host process. The node.c of an image is the node.c its library was compiled from."""
    require(set(EXPECT) == set(SCENARIOS) and set(REQUIRED) <= set(MUTANT_RUNS)
            and set(MUTANT_RUNS) == set(IMAGES) - {"raft"}, "a scenario or a mutant has no expectation")
    digests = emit.emit_cluster(work / "src")
    (work / "images").mkdir()
    schema = Schema(NODE)

    def library(name):
        native = build(schema, work / "lib" / name, source(IMAGES[name]), executable=False)
        require(sha(work / "lib" / name / "node.c") == digests[name],
                f"{name}: the driver and the simulator would run different C")
        return native

    def image(name):
        src = work / "src" / name
        subprocess.run([os.environ.get("CLANG", "clang"), *CLANG, "-I", str(HERE / "include"),
                        "-I", str(src), str(src / "driver.c"), str(HERE / "host.c"),
                        str(src / "node.c"), "-o", str(work / "images" / f"{name}.host")],
                       check=True, timeout=600)

    edited = ["raft", *(f"raft~{name}" for name in SPECS)]
    libraries = [library(name) for name in edited]      # the specializer recurses deeply: main thread
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(image, IMAGES))
    natives = dict(zip(edited, libraries))
    for name in IMAGES:
        natives.setdefault(name, natives["raft"])       # a driver mutant runs the unedited node
        require("spec" in IMAGES[name] or digests[name] == digests["raft"],
                f"{name}: a driver mutant was compiled from another node.c")
    return natives, digests, work / "images"


def provenance(label, kind, records, hashes, tender):
    """Each record says what was executed, and it is what this check expects. Not
    authentication: see tool/solo5/README.md."""
    for image, scenarios in records.items():
        file = f"{image}.{kind}"
        for name, runs in scenarios.items():
            for record in runs:
                where = f"{label}: {image}: {name} seed {record['seed']}"
                require(record.get("driver") == kind
                        and record.get("image") == {"file": file, "sha256": hashes.get(file)},
                        f"{where}: the record is of {record.get('driver')} {record.get('image')}")
                require((record.get("tender") or {}).get("sha256") == tender,
                        f"{where}: the record names the tender {record.get('tender')}")
                for node, boots in enumerate(record["boots"]):
                    for raw in boots:
                        banner = any(l.startswith("Solo5: Bindings version") for l in raw["other"])
                        argv = [Path(word).name for word in raw["argv"]]
                        require(f"node={node}" in argv and f"nodes={NODES}" in argv,
                                f"{where}: a boot of node {node} was started as {raw['argv']}")
                        if kind == "spt":
                            require(argv[0] == "solo5-spt" and file in argv[1:] and banner,
                                    f"{where}: a boot does not show a unikernel started by "
                                    f"solo5-spt: {raw['argv']} printed {raw['other'][:6]}")
                            taps = [word for word in argv if word.startswith("--net:")]
                            require(taps == [f"--net:net=tap{node}"],
                                    f"{where}: node {node} is not on its own tap: {taps}")
                        else:
                            require(argv[0] == file and not banner,
                                    f"{where}: a boot is not the host process: {raw['argv']}")


def shown(name, record, trace):
    """What the scenario requires of a run, read from the harness's log and the trace."""
    from targets.raft import lin
    want, log = EXPECT[name], record["log"]
    end = next(index for index, entry in enumerate(log) if entry["what"] == "end")
    kills = sum(entry["what"] == "kill" for entry in log[:end])
    restarts = sum(entry["what"] == "boot" and entry["boot"] > 0 for entry in log)
    cuts = sum(entry["what"] == "cut" for entry in log)
    require((kills, restarts, cuts) == (want["kills"], want["restarts"], want["cuts"]),
            f"{name}: {kills} kills, {restarts} restarts, {cuts} cuts")
    ops, problems = lin.history(trace)
    require(not problems, f"{name}: {problems}")
    answered = {op["req"]: op["result"] for op in ops if op["returned"] is not None}
    if want["answered"] is not None:
        require(len(answered) == want["answered"],
                f"{name} seed {record['seed']}: {len(answered)} of {want['answered']} operations "
                f"were answered: {answered}")
        gets = {request: answered[request] for request in want["gets"]}
        require(gets == want["gets"], f"{name} seed {record['seed']}: the gets returned {gets}, "
                                      f"the scenario requires {want['gets']}")
    return len(ops), len(answered)


TALLY = ("steps", "delivered", "forward", "drop", "dup", "cut", "floods", "skipped", "crashes",
         "restarts", "cuts", "inferred", "client")


def unedited(label, records, natives, report):
    """Every run of the unedited image: node by node, as one trace, and under Raft's checks."""
    from targets.raft.check import judge as raft_judge
    schema, native = natives["raft"].schema, natives["raft"]
    require(set(records) == set(SCENARIOS) and all(len(runs) == RUNS for runs in records.values()),
            f"{label}: not {RUNS} runs of every scenario")
    traces, lines, total = [], [], dict.fromkeys(TALLY, 0)
    terms = 0
    for name in SCENARIOS:
        per, sent, got, lone = dict.fromkeys(TALLY, 0), 0, 0, 0
        for record in records[name]:
            where = f"{label}: {name} seed {record['seed']}"
            try:
                lone += cluster_compare.alone(native, record)
                trace, facts = cluster_compare.reconstruct(native, record)
            except Mismatch as error:
                raise CheckFailure(f"{where}: {error}") from None
            try:
                ops, answered = shown(name, record, trace)
            except CheckFailure as error:
                raise CheckFailure(f"{label}: {error}") from None
            sent, got = sent + ops, got + answered
            for key in TALLY:
                per[key] += facts["counts"][key]
                total[key] += facts["counts"][key]
            traces.append((where, trace))
        require(lone == per["steps"], f"{label}: {name}: steps compared alone and together differ")
        lines.append(f"{name}: {per['steps']} steps, {per['delivered']} messages handled, "
                     f"{per['drop']} dropped, {per['dup']} doubled, {per['cut']} cut, "
                     f"{per['floods']} misdelivered, {per['crashes'] + per['cuts']} kills "
                     f"({per['cuts']} inside a step), {per['restarts']} restarts, "
                     f"{got} of {sent} operations answered")
    verdicts = raft_judge(schema, [trace for _, trace in traces])
    for (where, _), (failures, _) in zip(traces, verdicts):
        require(not failures, f"{where}: Raft's checks fail on the trace of the run: {failures[:2]}")
    pairs = check.refinement(schema, native, check.step_pairs([t for _, t in traces]), nodes=NODES)
    report(f"{label}: {len(traces)} runs ({RUNS} of each of {len(SCENARIOS)} scenarios), "
           f"{total['steps']} steps of {NODES} nodes; every step on a console is the compiled "
           f"step's, every frame on a link is an effect on a console, every message handled was "
           f"written to that node's link for that node, and every run is one simulator trace "
           f"that replays, with the same states, effects, ticks and disk rows")
    for line in lines:
        report(f"{label}:   {line}")
    report(f"{label}: the harness forwarded {total['forward']} frames, dropped {total['drop']}, "
           f"doubled {total['dup']}, cut {total['cut']}, and put {total['floods']} on the link of "
           f"a node they were not addressed to, of which the nodes skipped {total['skipped']} "
           f"and handled none; {total['crashes'] + total['cuts']} SIGKILLs, {total['cuts']} of them "
           f"inside a step ({total['inferred']} with an effect performed and not logged), "
           f"{total['restarts']} restarts from the block image")
    report(f"{label}: on all {len(traces)} traces the {len(schema.invariants['trace'])} invariants "
           f"of targets/raft/invariants.shen hold and the client history is linearizable "
           f"(targets/raft/lin.py); on the {pairs} distinct (state, event) pairs the runs reached, "
           f"the compiled step equals spec.shen in shen-jit")
    return dict(traces)


def unlogged(label, records, native, report):
    """A node that died after an effect and before the line that logs it.

    The runs have such deaths only by chance. They have deaths at a hold point, which are
    exact: the console says how many effects were performed. Taking the last `@did` and
    the `@hold` off such a console gives the record of a node that performed the effect
    and did not live to say so. The comparison has to find the effect in the evidence (the
    frame on the link, the row in the image) and arrive at the same trace."""
    from .. import sim
    from .compare import role
    schema, held, inside, tried = native.schema, 0, 0, {"send": 0, "output": 0, "disk-write": 0}
    for record in records["kill-inside-a-step"]:
        trace, facts = cluster_compare.reconstruct(native, record)
        for node, boots in enumerate(facts["boots"]):
            for number, boot in enumerate(boots):
                if boot["hold"] is None:
                    continue
                last = boot["steps"][-1]
                held += 1
                inside += last["applied"] < len(last["effects"])
                done = last["effects"][last["applied"] - 1] if last["applied"] else None
                if done is None or role(schema, done) not in tried:
                    continue
                edited = json.loads(json.dumps(record))
                raw = edited["boots"][node][number]
                require(raw["lines"][-1].startswith("@hold") and raw["lines"][-2].startswith("@did"),
                        f"{label}: a held console does not end with @did and @hold")
                del raw["lines"][-2:], raw["at"][-2:]
                try:
                    again, facts_again = cluster_compare.reconstruct(native, edited)
                except Mismatch as error:
                    raise CheckFailure(f"{label}: an effect performed and not logged was not "
                                       f"found in the evidence: {error}") from None
                require(sim.dumps(again) == sim.dumps(trace) and facts_again["counts"]["inferred"]
                        == facts["counts"]["inferred"] + 1,
                        f"{label}: an effect performed and not logged gave another trace")
                tried[role(schema, done)] += 1
    require(inside and tried["send"] and tried["disk-write"],
            f"{label}: of {held} kills at a hold point {inside} fell inside a step; the last "
            f"effect before the kill was {tried}")
    report(f"{label}: of the {held} kills at a hold point, {inside} fell inside a step, with some "
           f"of its effects performed and some not; for the {sum(tried.values())} whose last "
           f"effect before the kill was a frame ({tried['send'] + tried['output']}) or a block "
           f"write ({tried['disk-write']}), the record without that effect's log line (a death "
           f"between the effect and the line) is found to have performed it, from the frame on "
           f"the link or the row in the image, and gives the same trace")


def tampering(label, records, native, report):
    """Single edits of one good record are each rejected, for the reason that fits."""
    record = records["kill-the-leader"][0]
    schema = native.schema

    def first(predicate):
        return next(index for index, entry in enumerate(record["log"]) if predicate(entry))

    forwarded = first(lambda e: e["what"] == "in" and e.get("decision") == "forward")
    written = first(lambda e: e["what"] == "out" and not e["flood"] and e["id"] == record["log"][forwarded]["id"])
    reply = first(lambda e: e["what"] == "in" and e.get("decision") == "client")
    rows = next(node for node in range(NODES) if record["boots"][node][-1]["rows"])
    line = next(index for index, text in enumerate(record["boots"][0][0]["lines"])
                if text.startswith("@step ") and index > 8)

    def drop_log(index):
        return lambda r: r["log"].pop(index)

    def state(r):
        words = r["boots"][0][0]["lines"][line].split()
        words[4] = str(int(words[4]) + 1)                  # the term
        r["boots"][0][0]["lines"][line] = " ".join(words)

    def value(r):
        data = bytearray.fromhex(r["log"][reply]["frame"])
        data[14 + 8 * 4] ^= 1                              # the reply's last field
        r["log"][reply]["frame"] = data.hex()

    def swap(r):
        r["boots"][0], r["boots"][1] = r["boots"][1], r["boots"][0]

    edits = {
        "a frame the harness wrote, left out of its log": (drop_log(written), "had not written to its link"),
        "a frame the harness forwarded, logged as dropped":
            (lambda r: r["log"][forwarded].__setitem__("decision", "drop"), "the simulator cannot do ('deliver'"),
        "a frame a node sent, left out of the log": (drop_log(forwarded), "frames on its link"),
        "a reply with another value": (value, "on the node's link is"),
        "a row missing from a block image": (lambda r: r["boots"][rows][-1]["rows"].pop(), "the block image holds"),
        "a term changed on a console": (state, "is not the compiled step's"),
        "the consoles of two nodes exchanged": (swap, "is not node_init's"),
        "an exit status that is not a kill": (lambda r: r["boots"][2][0].__setitem__("status", 0), "exit status"),
    }
    for what, (edit, phrase) in edits.items():
        edited = json.loads(json.dumps(record))
        edit(edited)
        try:
            cluster_compare.alone(native, edited)
            cluster_compare.reconstruct(native, edited)
        except Mismatch as error:
            require(phrase in str(error), f"{label}: {what}: rejected, but not for it: {error}")
            continue
        raise CheckFailure(f"{label}: a record with {what} was accepted")
    report(f"{label}: {len(edits)} single edits of one record are each rejected with the message "
           f"that belongs to the edit ({'; '.join(edits)})")


def mutants(label, records, natives, report):
    """The verdict on every mutant image, per scenario. Returns {image: [scenarios that
    rejected it for its defect]}."""
    from targets.raft.check import judge as raft_judge
    found, lines = {}, []
    for image, names in MUTANT_RUNS.items():
        require(set(records.get(image, {})) == set(names), f"{label}: {image} was not run on {names}")
        found[image] = []
        spec = "spec" in IMAGES[image]
        for name in names:
            record = records[image][name][0]
            try:
                cluster_compare.alone(natives["raft"], record)
                cluster_compare.reconstruct(natives["raft"], record)
                against = None
            except Mismatch as error:
                against = str(error).splitlines()[0]
            if not spec:
                if against is not None and REQUIRED.get(image, "\0") in against:
                    found[image].append(name)
                lines.append(f"{image} on {name}: " + (f"rejected: {against[:230]}" if against
                                                       else "ACCEPTED"))
                continue
            # An edit of Raft: the run is a run of the edited node, and what has to reject it
            # is Raft's own checks on the trace of that run.
            try:
                cluster_compare.alone(natives[image], record)
                trace, _ = cluster_compare.reconstruct(natives[image], record)
            except Mismatch as error:
                lines.append(f"{image} on {name}: no trace of the run: {str(error).splitlines()[0][:230]}")
                continue
            failures = raft_judge(natives[image].schema, [trace])[0][0]
            names_failed = [failure[0] for failure in failures]
            if REQUIRED.get(image, "\0") in names_failed or (image not in REQUIRED and failures):
                found[image].append(name)
            lines.append(f"{image} on {name}: "
                         + ("differs from the unedited node step by step; " if against else
                            "step by step the same as the unedited node; ")
                         + (f"Raft's checks reject the trace: {', '.join(names_failed)}" if failures
                            else "Raft's checks pass on the trace"))
    for line in lines:
        report(f"{label}:   {line}")
    for image in MUTANT_RUNS:
        kind = "an edit of spec.shen" if "spec" in IMAGES[image] else "an edit of driver.c"
        report(f"{label}: {image} ({kind}): "
               + (f"rejected by {', '.join(found[image])}" if found[image] else "NOT FOUND"))
    missing = [image for image in REQUIRED if not found[image]]
    require(not missing, f"{label}: required mutants were not rejected for their defect: {missing}")
    return found


def judge(label, kind, records, natives, report, hashes, tender):
    require(set(records) == set(IMAGES), f"{label}: images run: {sorted(records)}")
    provenance(label, kind, records, hashes, tender)
    traces = unedited(label, records["raft"], natives, report)
    unlogged(label, records["raft"], natives["raft"], report)
    tampering(label, records["raft"], natives["raft"], report)
    found = mutants(label, records, natives, report)
    return traces, found


def container_records(work):
    """Run the cluster under solo5-spt in a Linux container (see container.py)."""
    given = os.environ.get("SOLO5_CLUSTER_RECORDS")
    if given:
        return Path(given), True
    name = os.environ.get("SOLO5_DOCKER", "docker")
    docker = shutil.which(name)
    if docker is None:
        raise container.Unavailable(f"solo5-cluster: blocked: no `{name}` on PATH to run a Linux "
                                    "container with")
    out = work / "solo5"
    out.mkdir()
    command = [docker, "run", "--rm", "--label", "sim-tool-solo5=cluster",
               "--device", "/dev/net/tun", "--cap-add", "NET_ADMIN",
               "-v", f"{container.ROOT}:/src:ro", "-v", f"{container.VOLUME}:/nix",
               "-v", f"{out}:/out", container.IMAGE, "sh", "-c", CLUSTER_SCRIPT % RUNS]
    try:
        done = subprocess.run(command, text=True, capture_output=True, timeout=CLUSTER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise container.Unavailable(f"solo5-cluster: blocked: no result within {CLUSTER_TIMEOUT}s "
                                    f"from\n  {' '.join(command)}") from None
    if done.returncode:
        raise container.Unavailable(f"solo5-cluster: blocked: exit status {done.returncode} from\n  "
                                    f"{' '.join(command)}\n" + (done.stdout + done.stderr)[-3000:])
    return out, False


def on_solo5(work, natives, digests, report):
    out, supplied = container_records(work)
    require((out / "SOLO5_COMMIT").read_text().strip() == container.COMMIT, "not the pinned Solo5")
    require(json.loads((out / "node.c.sha256.json").read_text()) == digests,
            "solo5-cluster: the unikernels were compiled from a different node.c than the "
            "simulator's library")
    drivers = json.loads((out / "driver.c.sha256.json").read_text())
    require(drivers == {name: sha(work / "src" / name / "driver.c") for name in IMAGES},
            "solo5-cluster: the unikernels were compiled from a different driver.c")
    listed = lambda name: dict(reversed(line.split()) for line in (out / name).read_text().splitlines())
    ran = listed("sources.sha256")
    here = {f"tool/solo5/{name}": sha(HERE / name) for name in SOURCES}
    require(ran == here, "solo5-cluster: the records were made by other harness sources than this "
                         f"checkout's: {sorted(n for n in here if ran.get(n) != here[n])}")
    *version, binary = (out / "tender.txt").read_text().strip().split("\n")
    tender, path = binary.split()
    require(version and version[0].startswith("solo5-spt tender, version v0.9.3"),
            f"solo5-cluster: the tender is {version}")
    hashes, hosts = listed("sha256.txt"), listed("host.sha256.txt")
    report(f"solo5-cluster: {len(IMAGES)} unikernels built by a Nix derivation on "
           f"{(out / 'uname.txt').read_text().strip()}; every node.c and driver.c is "
           f"byte-identical to this checkout's; raft.spt has SHA-256 {hashes['raft.spt']}; the "
           f"tender, {path}, has SHA-256 {tender}; one tap interface per node "
           f"({(out / 'taps.txt').read_text().strip()})")
    hvt = [line for line in (out / "hvt.txt").read_text().splitlines() if line.strip()]
    if hvt[-1] == "status 124" and any(line.startswith("@idle") for line in hvt):
        report("solo5-cluster: hvt: raft.hvt booted under solo5-hvt as node 0 of 3; NO cluster "
               "was run on it")
    else:
        report(f"solo5-cluster: hvt: BLOCKED: raft.hvt is linked for hvt and was NOT run: "
               f"solo5-hvt said {hvt[0]!r} ({hvt[-1]})")
    spt = judge("solo5-spt cluster", "spt", json.loads((out / "spt.json").read_text()), natives,
                report, hashes, tender)
    linux = judge("host-process cluster on that Linux", "host",
                  json.loads((out / "host.json").read_text()), natives, report, hosts, None)
    # The host processes' records are not records of the unikernels.
    fake = json.loads((out / "host.json").read_text())
    for scenarios in fake.values():
        for runs in scenarios.values():
            for record in runs:
                record["driver"] = "spt"
    try:
        provenance("solo5-spt cluster", "spt", fake, hashes, tender)
    except CheckFailure:
        report("solo5-cluster: the host processes' records, relabelled, are not accepted as "
               "records of solo5-spt")
    else:
        raise CheckFailure("solo5-cluster: host-process records were taken for solo5-spt's")
    return supplied, spt, linux


def main(solo5=False, report=print):
    started = time.time()
    with TemporaryDirectory() as work:
        work = Path(work)
        natives, digests, images = prepare(work)
        report(f"cluster: {len(IMAGES)} images of {NODES} Raft servers built as host processes: "
               f"the node, {len(DRIVERS)} edits of driver.c and {len(SPECS)} edits of spec.shen; "
               f"each node.c is the one its simulator library was compiled from")
        records = cluster.run_all("host", images, RUNS)
        hashes = {f"{name}.host": sha(images / f"{name}.host") for name in IMAGES}
        judge("host-process cluster", "host", records, natives, report, hashes, None)
        if not solo5:
            report(f"solo5-cluster-host: ok ({time.time() - started:.0f} s); this ran host "
                   f"processes, NOT Solo5")
            return
        supplied, _, _ = on_solo5(work, natives, digests, report)
    report("solo5-cluster: ok" + (" (judged from the records in SOLO5_CLUSTER_RECORDS; Solo5 was "
                                  "NOT run by this command)" if supplied else "")
           + f" ({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
