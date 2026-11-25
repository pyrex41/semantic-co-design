"""The effect shim against the simulator.

    nix develop -c python3 -m tool check solo5-host   the host-process driver only
    nix develop -c python3 -m tool check solo5        that, and the Solo5 unikernels

Both run the scenarios of tool/solo5/scenarios.py against real processes, turn each
record into a simulator trace with tool/solo5/compare.py, and require the mutant images to
be rejected. `solo5` needs a Linux machine to build and run the unikernels: on macOS it
uses a Linux container through Docker (tool/solo5/container.py) and fails, saying so,
when there is none. README.md in this directory says what each establishes.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from .. import check, sim
from ..check import CheckFailure, require
from ..compile import build
from ..schema import Schema
from . import compare, emit, run
from .mutants import DRIVERS
from .scenarios import DRIVER_MUTANTS, IMAGES, MUTANT_RUNS, NODES, SCENARIOS, manifest, source

HERE = Path(__file__).parent

# What each scenario must show, written from the scenario and not from any run: per boot,
# the frames the client receives and the rows in the block image after the kill.
OK, CONFLICT, FULL = "ok", "conflict", "full"
EXPECT = {
    "job-kill-between-write-and-reply": [
        ([["reply", 0, OK, 0], ["reply", 1, CONFLICT, 0]], [[0, 7, 11]]),
        ([], [[0, 7, 11], [1, 8, 11]]),
        ([["reply", 3, OK, 1], ["reply", 4, FULL, 0], ["reply", 5, CONFLICT, 1]],
         [[0, 7, 11], [1, 8, 11]])],
    "job-kill-before-write": [([], []), ([["reply", 1, OK, 0]], [[0, 7, 11]])],
    "job-kill-before-reply": [([], [[0, 7, 11]]),
                              ([["reply", 1, OK, 0], ["reply", 2, CONFLICT, 0]], [[0, 7, 11]])],
    "job-frames-that-are-not-requests": [([["reply", 0, OK, 0]], [[0, 7, 11]])],
    **{f"probe-kill-after-{cut}-of-4-effects": [
        ([["say", 1, 5], ["say", 2, 5]][:cut // 2], [[1, 5], [2, 5]][:(cut + 1) // 2]),
        ([["say", 1, 6], ["say", 2, 6], ["say", 3, 1], ["say", 3, 2]], [[1, 6], [2, 6]])]
       for cut in range(5)},
    "probe-step-without-result": [([["say", 1, 5], ["say", 2, 5]], [[1, 5], [2, 5]])],
}
# Why each mutant image must be rejected: a phrase of the comparison's own message.
REASON = {
    "accepted_job~ack-without-write": "the block image holds",
    "accepted_job~torn-row": "the block image holds",
    "accepted_job~stale-read": "the simulator cannot do ('local'",
    "accepted_job~late-cut": "the block image holds",
    "accepted_job~keeps-old-state": "post differs",
    "accepted_job~swallows-output": "the client received",
    "accepted_job~accepts-any-field": "are not the requests sent",
    "accepted_job~reply-before-ack": "effects differs",
    "probe~late-cut": "the block image holds",
    "timer~early-timer": "the simulator's clock",
    "timer~ignores-cancel": "the simulator cannot do ('local'",
    "timer~stale-clock": "the simulator cannot do ('local'",
}
CLANG = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
         "-Wno-unused-variable", "-Wno-tautological-compare", "-fbracket-depth=8000"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(work):
    """The simulator's library for every node, and the host image for every image.

    Returns ({image name: Native}, {image name: SHA-256 of node.c}, image directory).
    The node.c an image is compiled from must be, byte for byte, the node.c the
    simulator's library was compiled from.
    """
    digests = emit.emit_all(work / "src")
    images = work / "images"
    images.mkdir()
    natives, built = {}, {}
    for name, image in IMAGES.items():
        key = (image["node"], image.get("spec"))
        if key not in built:
            built[key] = build(Schema(manifest(image)), work / "lib" / name, source(image),
                               executable=False)
            require(sha(work / "lib" / name / "node.c") == digests[name],
                    f"{name}: the driver and the simulator would run different C")
        natives[name] = built[key]
        src = work / "src" / name
        subprocess.run([os.environ.get("CLANG", "clang"), *CLANG, "-I", str(HERE / "include"),
                        "-I", str(src), str(src / "driver.c"), str(HERE / "host.c"),
                        str(src / "node.c"), "-o", str(images / f"{name}.host")],
                       check=True, timeout=300)
    return natives, digests, images


def judge(label, records, natives, report):
    """Every record of one kind of driver against the simulator.

    Returns {scenario: trace digest} for the scenarios of nodes without a clock, whose
    traces do not depend on when the run happened.
    """
    require(set(records) == set(SCENARIOS), f"{label}: scenarios missing from the records: "
                                            f"{sorted(set(SCENARIOS) - set(records))}")
    digests, traces = {}, {}
    counts = {"boots": 0, "steps": 0, "kills": 0, "restarts": 0, "cuts": 0}
    for name, scenario in SCENARIOS.items():
        if scenario.get("mutant"):
            continue
        native = natives[scenario["image"]]
        try:
            trace, facts = compare.reconstruct(native, records[name], stops=scenario.get("stops", False))
        except compare.Mismatch as error:
            raise CheckFailure(f"{label}: {name}: {error}") from None
        traces[name] = trace
        counts["boots"] += len(facts["boots"])
        counts["steps"] += sum(len(boot["steps"]) for boot in facts["boots"])
        counts["kills"] += sum(boot["status"] == -9 for boot in records[name]["boots"])
        counts["restarts"] += len(facts["boots"]) - 1
        counts["cuts"] += sum("before_crash" in step for step in trace["steps"])
        for number, (received, rows) in enumerate(EXPECT.get(name, [])):
            boot = records[name]["boots"][number]
            require(boot["received"] == received and boot["rows"] == rows,
                    f"{label}: {name}: boot {number} should leave replies {received} and rows "
                    f"{rows}; it left {boot['received']} and {boot['rows']}")
        if not {"timer", "clock"} & set(native.schema.roles):
            digests[name] = sim.digest(trace)
    report(f"{label}: {len(traces)} scenarios, {counts['boots']} boots, {counts['steps']} events "
           f"handled; {counts['kills']} boots ended by SIGKILL, {counts['restarts']} of them "
           f"followed by a restart on the same image, {counts['cuts']} of those kills inside a "
           f"step; every record is a simulator trace that replays, with the same states, "
           f"effects, frames and disk rows")
    # The steps the real driver took, against the Shen source itself.
    pairs = 0
    for node in NODES:
        mine = [traces[name] for name in traces if SCENARIOS[name]["image"] == node]
        pairs += check.refinement(natives[node].schema, natives[node], check.step_pairs(mine))
    report(f"{label}: on the {pairs} distinct (state, event) pairs those runs reached, the "
           f"compiled step equals the Shen source in shen-jit")
    specific(label, records, natives, traces, report)
    tampering(label, records, natives, report)
    rejected = {}
    for name, scenario in SCENARIOS.items():
        if not scenario.get("mutant"):
            continue
        image = scenario["image"]
        native = natives[image.split("~")[0]]           # the simulator runs the unedited node
        try:
            compare.reconstruct(native, records[name])
        except compare.Mismatch as error:
            require(REASON[image] in str(error),
                    f"{label}: {image} was rejected, but not for its defect: {error}")
            rejected[image] = str(error).splitlines()[0]
            continue
        raise CheckFailure(f"{label}: the comparison accepted a run of {image}")
    require(set(rejected) == set(MUTANT_RUNS) and set(DRIVERS) == {
        m for mutants in DRIVER_MUTANTS.values() for m in mutants},
        f"{label}: a mutant was not run")
    report(f"{label}: {len(rejected)} mutant images rejected, each for its own defect: "
           + ", ".join(sorted(rejected)))
    return digests


def specific(label, records, natives, traces, report):
    """What the scenarios are for, beyond agreeing with the simulator."""
    # AcceptedJob: the retry after the kill is answered from the durable row.
    name = "job-kill-between-write-and-reply"
    schema = natives["accepted_job"].schema
    boots = [compare.parse(schema, raw) for raw in records[name]["boots"]]
    killed = boots[1]["steps"][-1]
    require(killed["event"] == ("submit", 2, 8, 11) and killed["effects"] == (("write", 0, 1, 8, 11),)
            and killed["applied"] == 1 and records[name]["boots"][1]["received"] == [],
            f"{label}: {name}: the node was not killed between the write and the reply")
    require(not any(e[0] == "write" for step in boots[2]["steps"] for e in step["effects"]),
            f"{label}: {name}: the retry wrote again")
    jobs = [traces[n] for n in traces if n.startswith("job-")]
    bad = check.invariants(schema, jobs)
    require(not any(bad), f"{label}: an AcceptedJob invariant fails on a real run: {bad}")
    # The same invariants reject a real run of the node that answers before the acknowledgement.
    mutant = f"{name}@accepted_job~reply-before-ack"
    trace, _ = compare.reconstruct(natives["accepted_job~reply-before-ack"], records[mutant])
    failed = [invariant for invariant, _ in check.invariants(schema, [trace])[0]]
    require("inv.ok-is-durable" in failed,
            f"{label}: the invariants accept a run of the node that replies before the "
            f"acknowledgement: {failed}")
    # Frames that are not requests never reached the step.
    # (A tap interface also carries the host's own frames, all of another ethertype.)
    skips = compare.parse(schema, records["job-frames-that-are-not-requests"]["boots"][0])["skips"]
    require([why for why in skips if why != "ethertype"] == ["tag", "tag", "field", "length"]
            and "ethertype" in skips, f"{label}: frames skipped for {skips}")
    # The probe: a kill after each prefix of four effects.
    probe = natives["probe"].schema
    cuts = [compare.parse(probe, records[f"probe-kill-after-{cut}-of-4-effects"]["boots"][0])
            ["steps"][-1]["applied"] for cut in range(5)]
    require(cuts == [0, 1, 2, 3, 4], f"{label}: the probe was killed after {cuts} effects")
    # A step without a result stops the node; the simulated run ends in the same error.
    stopped = traces["probe-step-without-result"]
    require(stopped["end"].startswith("error") and stopped["steps"][-1]["kind"] == "error",
            f"{label}: a step without a result did not end the run")
    # Timers fire in the order they are due, and never across a kill.
    order = [step["event"] for step in traces["timer-order-and-cancel"]["steps"]
             if step.get("event") and step["event"][0] == "fired"]
    require(order == [["fired", 2], ["fired", 1]], f"{label}: timers fired as {order}")
    crossed = [step["event"] for step in traces["timer-does-not-cross-a-kill"]["steps"]
               if step.get("event") and step["event"][0] == "fired"]
    require(crossed == [["fired", 2]], f"{label}: timers after the kill: {crossed}")
    declared = len(schema.invariants["state"]) + len(schema.invariants["trace"])
    report(f"{label}: AcceptedJob killed between its write and its reply answers the retry "
           f"from the durable row without writing again; its {declared} Shen invariants hold "
           f"on the {len(jobs)} real runs and inv.ok-is-durable fails on a real run of "
           f"reply-before-ack; 5 frames that are not requests skipped; probe killed after 0, "
           f"1, 2, 3 and 4 of 4 effects; a trap stops the node; timers fire in due order, not "
           f"after a cancel, not across a kill")


def line_of(boot, prefix, nth=0):
    return [n for n, line in enumerate(boot["lines"]) if line.startswith(prefix)][nth]


def drop(boot, prefix, nth=0):
    del boot["lines"][line_of(boot, prefix, nth)]


def bump(boot, prefix, nth=0, word=-1):
    """Add one to an integer (the last one by default) of a console line."""
    at = line_of(boot, prefix, nth)
    words = boot["lines"][at].split()
    words[word] = str(int(words[word]) + 1)
    boot["lines"][at] = " ".join(words)


# One correct record, edited in one place each time. The comparison draws on three
# sources (console, frames, block image); every one of them must be able to sink a record.
EDITS = {
    "a row missing from the image": lambda boots: boots[1]["rows"].pop(),
    "a row with another digest in the image": lambda boots: boots[0]["rows"][0].__setitem__(2, 12),
    "a row in the image that was never written": lambda boots: boots[0]["rows"].append([1, 9, 9]),
    "a reply the client did not receive": lambda boots: boots[2]["received"].pop(0),
    "a reply with another receipt at the client": lambda boots: boots[2]["received"][0].__setitem__(3, 0),
    "a request the client did not send": lambda boots: boots[0]["sent"].pop(),
    "a write the console does not report": lambda boots: drop(boots[1], "@did", 2),
    "another receipt in a logged step": lambda boots: bump(boots[2], "@step", 3),
    "another state in a logged step": lambda boots: bump(boots[0], "@step", 1, 1),
    "an effect the step did not return": lambda boots: bump(boots[0], "@step", 1),
    "another hold point in the console": lambda boots: bump(boots[1], "@hold"),
    "a read result the image does not explain": lambda boots: bump(boots[2], "@event", 2),
    "another initial state": lambda boots: bump(boots[0], "@init"),
    "the killed boot left out": lambda boots: boots.pop(1),
    "the boots in another order": lambda boots: boots.reverse(),
}


def tampering(label, records, natives, report):
    name = "job-kill-between-write-and-reply"
    for what, edit in EDITS.items():
        record = json.loads(json.dumps(records[name]))
        edit(record["boots"])
        require(record != records[name], f"{label}: the edit '{what}' changed nothing")
        try:
            compare.reconstruct(natives["accepted_job"], record)
        except compare.Mismatch:
            continue
        raise CheckFailure(f"{label}: the comparison accepted a record with {what}")
    report(f"{label}: {len(EDITS)} single edits of the record of {name} (console, frames, "
           f"image, boots) are each rejected")


def host(work, report):
    natives, digests, images = prepare(work)
    edited = sum("driver" in image or "spec" in image for image in IMAGES.values())
    report(f"images: {len(IMAGES)} host processes built from driver.c, host.c and node.c "
           f"({edited} of them with one edit of the driver or of the specification); each "
           f"node.c is byte-identical to the one the simulator's library is compiled from")
    records = run.run_all("host", images)
    return natives, digests, judge("host process", records, natives, report)


def main(report=print, solo5=True):
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        natives, digests, traces = host(work, report)
        if solo5:
            from . import container
            container.check(work, natives, digests, traces, report)
    report("solo5: ok" if solo5 else "solo5-host: ok (the Solo5 unikernels were not run)")
