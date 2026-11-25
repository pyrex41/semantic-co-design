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

# What each scenario must show, written from the scenario and not from any run. Per boot:
# how many events the node handles, the frames the client receives, the rows in the block
# image afterwards, how the boot ends (None: killed by the harness; "trap": a step without
# a result; otherwise the reason the driver gives for stopping), and whether the node
# dies inside a step, with some of its effects performed and some not.
OK, CONFLICT, FULL = "ok", "conflict", "full"


def boot(events, received=None, rows=None, stops=None, inside=False):
    return {"events": events, "received": received, "rows": rows, "stops": stops,
            "inside": inside or stops not in (None, "trap")}


EXPECT = {
    "job-kill-between-write-and-reply": [
        boot(6, [["reply", 0, OK, 0], ["reply", 1, CONFLICT, 0]], [[0, 7, 11]]),
        boot(4, [], [[0, 7, 11], [1, 8, 11]]),
        boot(6, [["reply", 3, OK, 1], ["reply", 4, FULL, 0], ["reply", 5, CONFLICT, 1]],
             [[0, 7, 11], [1, 8, 11]])],
    "job-kill-before-write": [boot(4, [], [], inside=True),
                              boot(5, [["reply", 1, OK, 0]], [[0, 7, 11]])],
    "job-kill-before-reply": [boot(5, [], [[0, 7, 11]], inside=True),
                              boot(5, [["reply", 1, OK, 0], ["reply", 2, CONFLICT, 0]], [[0, 7, 11]])],
    "job-frames-that-are-not-requests": [boot(5, [["reply", 0, OK, 0]], [[0, 7, 11]])],
    "job-same-request-three-times": [boot(7, [["reply", 0, OK, 0]] * 3, [[0, 7, 11]])],
    "job-device-refuses-a-write": [
        boot(5, [["reply", 0, OK, 0]], [[0, 7, 11]]),
        boot(4, [], [[0, 7, 11]], stops="block-write"),
        boot(5, [["reply", 2, OK, 1]], [[0, 7, 11], [1, 8, 11]])],
    **{f"probe-kill-after-{cut}-of-4-effects": [
        boot(3, [["say", 1, 5], ["say", 2, 5]][:cut // 2], [[1, 5], [2, 5]][:(cut + 1) // 2],
             inside=cut < 4),
        boot(5, [["say", 1, 6], ["say", 2, 6], ["say", 3, 1], ["say", 3, 2]], [[1, 6], [2, 6]])]
       for cut in range(5)},
    "probe-step-without-result": [boot(4, [["say", 1, 5], ["say", 2, 5]], [[1, 5], [2, 5]],
                                       stops="trap")],
    "store-read-of-a-row-the-step-wrote": [
        boot(4, [["done", 1]], [[1, 5]], stops="read-after-write"),
        boot(3, [["say", 1, 5]], [[1, 5]])],
    "store-write-then-read-of-another-row": [
        boot(8, [["done", 2], ["done", 1], ["say", 1, 9], ["say", 1, 5]], [[1, 5], [2, 9]])],
    "store-failed-read-and-row-outside-the-device": [
        boot(4, [["done", 0]], [[0, 7]], stops="block-read"),
        boot(2, [], [[0, 7]], stops="disk-key"),
        boot(5, [["say", 1, 7], ["say", 0, 0]], [[0, 7]])],
    # The frames of the timer node carry clock readings; `specific` looks at them.
    "timer-order-and-cancel": [boot(15)],
    "timer-does-not-cross-a-kill": [boot(4), boot(6)],
    "timer-table-full": [boot(35, stops="timer-table-full"), boot(6)],
    "timer-far-due-time": [boot(6)],
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
    "accepted_job~acks-failed-write": "the block image holds",
    "accepted_job~dedupes-requests": "are not the requests sent",
    "accepted_job~repeats-output": "the client received",
    "accepted_job~reply-before-ack": "effects differs",
    "accepted_job~reply-then-write": "effects differs",
    "probe~late-cut": "the block image holds",
    "store~reads-own-write": "the simulator cannot do ('local'",
    "store~ignores-failed-read": "the simulator cannot do ('local'",
    "timer~early-timer": "the simulator's clock",
    "timer~ignores-cancel": "the simulator cannot do ('local'",
    "timer~stale-clock": "the simulator cannot do ('local'",
    "timer~half-tick": "before it fired",
    "timer~slow-tick": "and at least",
    "timer~wrapping-deadline": "of processor time",
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
    require(set(EXPECT) == {n for n, s in SCENARIOS.items() if not s.get("mutant")}
            and set(REASON) == set(MUTANT_RUNS), "a scenario or a mutant has no expectation")
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


def provenance(label, records, proof):
    """Each record says what was executed, and it is what this check expects.

    `proof` is {"kind": "host" or "spt", "images": {file: SHA-256}, "tender": SHA-256 or
    None}. The record carries the hash of the image file and of the tender as the runner
    read them, the argument vector of every boot and what the process printed besides
    its record. A unikernel under a Solo5 tender prints the bindings' banner; a host
    process does not. None of this is signed: it catches a mix-up, not a forgery.
    """
    for name, record in records.items():
        file = f"{SCENARIOS[name]['image']}.{proof['kind']}"
        require(record.get("driver") == proof["kind"]
                and record.get("image") == {"file": file, "sha256": proof["images"].get(file)},
                f"{label}: {name}: the record is of {record.get('driver')} {record.get('image')}, "
                f"not of {file} with SHA-256 {proof['images'].get(file)}")
        require((record.get("tender") or {}).get("sha256") == proof["tender"],
                f"{label}: {name}: the record names the tender {record.get('tender')}")
        for number, raw in enumerate(record["boots"]):
            banner = any(line.startswith("Solo5: Bindings version") for line in raw["other"])
            argv = [Path(word).name for word in raw["argv"]]
            if proof["kind"] == "spt":
                require(argv[0] == "solo5-spt" and file in argv[1:] and banner
                        and raw["argv"][0] == record["tender"]["path"],
                        f"{label}: {name}: boot {number} does not show a unikernel started by "
                        f"solo5-spt: {raw['argv']} printed {raw['other'][:6]}")
            else:
                require(argv[0] == file and not banner,
                        f"{label}: {name}: boot {number} is not the host process: {raw['argv']}")


def refuses(label, records, proof, what):
    try:
        provenance(label, records, proof)
    except CheckFailure:
        return
    raise CheckFailure(f"{label}: records with {what} were taken for records of {proof['kind']}")


def provenance_teeth(label, records, proof, report):
    """Records that say something else was executed are not accepted as these."""
    name = "job-kill-between-write-and-reply"
    edits = {
        "another image hash": lambda r: r["image"].__setitem__("sha256", "0" * 64),
        "another kind of driver": lambda r: r.__setitem__("driver", "hvt"),
        "another program in a boot's argument vector":
            lambda r: r["boots"][1]["argv"].__setitem__(0, "/bin/true"),
    }
    if proof["kind"] == "spt":
        edits["another tender hash"] = lambda r: r["tender"].__setitem__("sha256", "0" * 64)
        edits["a boot without the Solo5 banner"] = lambda r: r["boots"][1].__setitem__(
            "other", [line for line in r["boots"][1]["other"] if not line.startswith("Solo5:")])
    else:
        edits["a Solo5 banner"] = lambda r: r["boots"][1]["other"].append("Solo5: Bindings version v0.9.3")
    for what, edit in edits.items():
        edited = json.loads(json.dumps(records))
        edit(edited[name])
        refuses(label, edited, proof, what)
    report(f"{label}: {len(edits)} edits of what a record says was executed ({', '.join(edits)}) "
           f"are each rejected")


def judge(label, records, natives, report, proof):
    """Every record of one kind of driver against the simulator.

    Returns {scenario: trace digest} for the scenarios of nodes without a clock, whose
    traces do not depend on when the run happened.
    """
    require(set(records) == set(SCENARIOS), f"{label}: the records are not of the scenarios: "
                                            f"{sorted(set(SCENARIOS) ^ set(records))}")
    provenance(label, records, proof)
    provenance_teeth(label, records, proof, report)
    digests, traces = {}, {}
    counts = {"boots": 0, "steps": 0, "kills": 0, "restarts": 0, "cuts": 0}
    stops = []
    for name, scenario in SCENARIOS.items():
        if scenario.get("mutant"):
            continue
        native = natives[scenario["image"]]
        try:
            trace, facts = compare.reconstruct(native, records[name], stops=scenario.get("stops", False))
            compare.quiet(scenario["boots"], records[name])
        except compare.Mismatch as error:
            raise CheckFailure(f"{label}: {name}: {error}") from None
        traces[name] = trace
        expected = EXPECT[name]
        require(len(facts["boots"]) == len(scenario["boots"]) == len(expected),
                f"{label}: {name}: {len(facts['boots'])} boots recorded")
        for number, (want, parsed, raw) in enumerate(zip(expected, facts["boots"], records[name]["boots"])):
            steps = parsed["steps"]
            stopped = parsed["fail"] or ("trap" if steps and steps[-1]["trap"] is not None else None)
            require(len(steps) == want["events"] and stopped == want["stops"]
                    and raw["status"] == (-9 if stopped is None else compare.ABORT),
                    f"{label}: {name}: boot {number} should handle {want['events']} events and end "
                    f"by {want['stops'] or 'SIGKILL'}; it handled {len(steps)} and ended by "
                    f"{stopped or 'a signal'} with status {raw['status']}")
            for part in ("received", "rows"):
                require(want[part] is None or raw[part] == want[part],
                        f"{label}: {name}: boot {number} should leave {part} {want[part]}; it left "
                        f"{raw[part]}")
            counts["kills"] += stopped is None
            stops += [stopped] if stopped else []
        inside = sum("before_crash" in step for step in trace["steps"])
        require(inside == sum(want["inside"] for want in expected),
                f"{label}: {name}: {inside} deaths inside a step")
        counts["boots"] += len(expected)
        counts["steps"] += sum(want["events"] for want in expected)
        counts["restarts"] += len(expected) - 1
        counts["cuts"] += inside
        if not {"timer", "clock"} & set(native.schema.roles):
            digests[name] = sim.digest(trace)
    report(f"{label}: {len(traces)} scenarios, {counts['boots']} boots, {counts['steps']} events "
           f"handled, each count as the scenario requires; {counts['kills']} boots ended by "
           f"SIGKILL (status -9) and {len(stops)} by the node stopping itself (status 255: "
           f"{', '.join(sorted(stops))}); {counts['restarts']} boots are restarts on the same "
           f"image; {counts['cuts']} deaths are inside a step, {len(stops) - stops.count('trap')} "
           f"of them stops; every record is a simulator trace that replays, with the same "
           f"states, effects, frames and disk rows")
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
            compare.reconstruct(native, records[name], stops=scenario.get("stops", False))
            compare.quiet(scenario["boots"], records[name])
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


def unbacked(record):
    """Every `ok` a client of AcceptedJob received whose row was not in the block image
    when that boot had ended: (boot, reply). Read from the frames and the image alone."""
    asked, out = {}, []
    for number, raw in enumerate(record["boots"]):
        asked.update({request: [key, digest] for _, request, key, digest in raw["sent"]})
        out += [(number, reply) for reply in raw["received"]
                if reply[2] == OK and [reply[3], *asked[reply[1]]] not in raw["rows"]]
    return out


def fired(trace):
    return [step["event"] for step in trace["steps"]
            if step.get("event") and step["event"][0] == "fired"]


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
    jobs = [n for n in traces if n.startswith("job-")]
    bad = check.invariants(schema, [traces[n] for n in jobs])
    require(not any(bad), f"{label}: an AcceptedJob invariant fails on a real run: {bad}")
    loose = {n: unbacked(records[n]) for n in jobs}
    require(not any(loose.values()), f"{label}: a client holds ok without a row: {loose}")

    # Two specifications that answer too early, on the same driver.
    #
    # reply-then-write answers and then writes. Killed between the two, the client holds
    # `ok` and the block image has no row: the defect is in the frames and the image.
    early = f"{name}@accepted_job~reply-then-write"
    require(unbacked(records[early]) == [(1, ["reply", 2, OK, 1])],
            f"{label}: reply-then-write: ok replies without a row: {unbacked(records[early])}")
    trace, _ = compare.reconstruct(natives["accepted_job~reply-then-write"], records[early])
    failed = [invariant for invariant, _ in check.invariants(schema, [trace])[0]]
    require("inv.ok-is-durable" in failed,
            f"{label}: the invariants accept the run of reply-then-write: {failed}")
    # reply-before-ack writes and answers in one step. On this driver the block write
    # has returned before the reply is sent, so no kill leaves `ok` without its row,
    # and the frames and the image show nothing wrong. The simulator trace of the same
    # run does fail the invariant: there the row is still pending when the reply is
    # made, because the simulator has no sync between two effects of a step. That
    # failure is a statement about the simulator's schedules, not about this run.
    same = f"{name}@accepted_job~reply-before-ack"
    require(unbacked(records[same]) == [],
            f"{label}: reply-before-ack left ok without a row: {unbacked(records[same])}")
    trace, _ = compare.reconstruct(natives["accepted_job~reply-before-ack"], records[same])
    failed = [invariant for invariant, _ in check.invariants(schema, [trace])[0]]
    require("inv.ok-is-durable" in failed,
            f"{label}: the invariants accept the simulator trace of reply-before-ack: {failed}")

    # A repeated request reaches the step every time; the step answers from the stored row.
    repeat = compare.parse(schema, records["job-same-request-three-times"]["boots"][0])
    answers = [step["effects"] for step in repeat["steps"] if step["event"] == ("submit", 0, 7, 11)]
    require(answers == [(("write", 0, 0, 7, 11),), (("reply", 0, OK, 0),), (("reply", 0, OK, 0),)]
            and not [why for why in repeat["skips"] if why != "ethertype"],
            f"{label}: the repeated request was handled as {answers}, skips {repeat['skips']}")
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
    # The store: the refused read came after the write of the same step was performed.
    store = natives["store"].schema
    refused = compare.parse(store, records["store-read-of-a-row-the-step-wrote"]["boots"][0])["steps"][-1]
    require(refused["effects"] == (("write", 1, 1, 5), ("read", 1, 1)) and refused["applied"] == 1,
            f"{label}: the read after a write was not refused where expected: {refused}")
    # Timers fire in the order they are due, and never across a kill or a stop.
    order = fired(traces["timer-order-and-cancel"])
    require(order == [["fired", 2], ["fired", 1]], f"{label}: timers fired as {order}")
    crossed = fired(traces["timer-does-not-cross-a-kill"])
    require(crossed == [["fired", 2]], f"{label}: timers after the kill: {crossed}")
    require(fired(traces["timer-table-full"]) == [["fired", 1]] and not fired(traces["timer-far-due-time"]),
            f"{label}: timers fired after a full table or before a far due time")
    # A timer armed by a request, on the harness's clock: from sending the request to
    # reading the console line of the firing.
    timer = natives["timer"].schema
    raw = records["timer-order-and-cancel"]["boots"][0]
    steps = compare.parse(timer, raw)["steps"]
    waited = raw["at"][next(s for s in steps if s["event"] == ("fired", 2))["line"]] - raw["sent_at"][1]
    require(waited >= 400 * (1 - compare.DRIFT) - compare.SLACK,
            f"{label}: timer 2, delay 400 ms, fired {waited:.0f} ms after its request was sent")
    idle = records["timer-far-due-time"]["boots"][0]["cpu"]
    declared = len(schema.invariants["state"]) + len(schema.invariants["trace"])
    report(f"{label}: AcceptedJob killed between its write and its reply answers the retry "
           f"from the durable row without writing again; its {declared} Shen invariants hold "
           f"on the simulator traces of the {len(jobs)} real runs, and in none of them does a "
           f"client hold ok without its row in the image. reply-then-write, killed between its "
           f"reply and its write: the client holds ok 1 and the image has no such row, and "
           f"inv.ok-is-durable fails on its trace. reply-before-ack: no ok without its row in "
           f"the frames and the image (the block write is synchronous); inv.ok-is-durable fails "
           f"on the simulator trace only")
    report(f"{label}: the same request three times reaches the step three times; a refused "
           f"write, a failed read, a row outside the device, a read of a row the same step "
           f"wrote and a seventeenth timer each stop the node before that effect; 5 frames "
           f"that are not requests skipped; probe killed after 0, 1, 2, 3 and 4 of 4 effects; "
           f"a trap stops the node with the simulator's error code; timers fire in due order, "
           f"not after a cancel, not across a kill; timer 2 (400 ms) fired {waited:.0f} ms of "
           f"the harness's clock after its request was sent; two timers due far away cost "
           f"{idle:.2f} s of processor time in 3 s of waiting")


def line_of(boot, prefix, nth=0):
    return [n for n, line in enumerate(boot["lines"]) if line.startswith(prefix)][nth]


def drop(boot, prefix, nth=0):
    at = line_of(boot, prefix, nth)
    del boot["lines"][at], boot["at"][at]


def bump(boot, prefix, nth=0, word=-1):
    """Add one to an integer (the last one by default) of a console line."""
    at = line_of(boot, prefix, nth)
    words = boot["lines"][at].split()
    words[word] = str(int(words[word]) + 1)
    boot["lines"][at] = " ".join(words)


def unsent(boot):
    del boot["sent"][-1], boot["sent_at"][-1]


# One correct record, edited in one place each time, and a phrase of the message the
# comparison must reject it with. The comparison draws on the console, the frames, the
# block image and the exit status; every one of them must be able to sink a record.
EDITS = {
    "a row missing from the image": (lambda boots: boots[1]["rows"].pop(), "the block image holds"),
    "a row with another digest in the image":
        (lambda boots: boots[0]["rows"][0].__setitem__(2, 12), "the block image holds"),
    "a row in the image that was never written":
        (lambda boots: boots[0]["rows"].append([1, 9, 9]), "the block image holds"),
    "a reply the client did not receive":
        (lambda boots: boots[2]["received"].pop(0), "the client received"),
    "a reply with another receipt at the client":
        (lambda boots: boots[2]["received"][0].__setitem__(3, 0), "the client received"),
    "a request the client did not send": (lambda boots: unsent(boots[0]), "are not the requests sent"),
    "a write the console does not report": (lambda boots: drop(boots[1], "@did", 2), "asked to hold"),
    "another receipt in the reply of a logged step":
        (lambda boots: bump(boots[2], "@step", 3), "the client received"),
    "another state in a logged step": (lambda boots: bump(boots[0], "@step", 1, 1), "post differs"),
    "another key in the write of a logged step":
        (lambda boots: bump(boots[1], "@step", 3, -2), "effects differs"),
    "another hold point in the console": (lambda boots: bump(boots[1], "@hold"), "asked to hold"),
    "a read result the image does not explain":
        (lambda boots: bump(boots[2], "@event", 2), "the simulator cannot do ('local'"),
    "another initial state": (lambda boots: bump(boots[0], "@init"), "the initial state differs"),
    "the killed boot left out": (lambda boots: boots.pop(1), "the simulator cannot do ('local'"),
    "the boots in another order": (lambda boots: boots.reverse(), "the simulator cannot do ('local'"),
    "a boot that no signal ended": (lambda boots: boots[1].__setitem__("status", 0), "exit status"),
    "a boot without its @boot line": (lambda boots: drop(boots[0], "@boot"), "one @boot line"),
    "a boot on a block file of another size":
        (lambda boots: bump(boots[2], "@boot", 0, -2), "on the block file given"),
}


def tampering(label, records, natives, report):
    name = "job-kill-between-write-and-reply"
    for what, (edit, reason) in EDITS.items():
        record = json.loads(json.dumps(records[name]))
        edit(record["boots"])
        require(record != records[name], f"{label}: the edit '{what}' changed nothing")
        try:
            compare.reconstruct(natives["accepted_job"], record)
        except compare.Mismatch as error:
            require(reason in str(error),
                    f"{label}: a record with {what} was rejected, but for another reason: {error}")
            continue
        raise CheckFailure(f"{label}: the comparison accepted a record with {what}")
    # A record of a node with a clock, with the harness's clock edited: the request that
    # armed timer 2 is said to have been sent 300 ms later than it was.
    late = json.loads(json.dumps(records["timer-order-and-cancel"]))
    late["boots"][0]["sent_at"][1:] = [at + 300 for at in late["boots"][0]["sent_at"][1:]]
    try:
        compare.reconstruct(natives["timer"], late)
    except compare.Mismatch as error:
        require("the harness's clock" in str(error), f"{label}: the edited clock: {error}")
    else:
        raise CheckFailure(f"{label}: the comparison accepted a timer that fired early by the "
                           f"harness's clock")
    report(f"{label}: {len(EDITS)} single edits of the record of {name} (console, frames, "
           f"image, exit status, boots) are each rejected for the edit made, and so is a record "
           f"whose harness clock says a timer fired 300 ms early")


def host(work, report):
    natives, digests, images = prepare(work)
    edited = sum("driver" in image or "spec" in image for image in IMAGES.values())
    report(f"images: {len(IMAGES)} host processes built from driver.c, host.c and node.c "
           f"({edited} of them with one edit of the driver or of the specification); each "
           f"node.c is byte-identical to the one the simulator's library is compiled from")
    records = run.run_all("host", images)
    proof = {"kind": "host", "tender": None,
             "images": {f"{name}.host": sha(images / f"{name}.host") for name in IMAGES}}
    return natives, digests, judge("host process", records, natives, report, proof)


def main(report=print, solo5=True):
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        natives, digests, traces = host(work, report)
        supplied = False
        if solo5:
            from . import container
            supplied = container.check(work, natives, digests, traces, report)
    report("solo5-host: ok (the Solo5 unikernels were not run)" if not solo5 else
           "solo5: ok (judged from the records in SOLO5_RECORDS; Solo5 was NOT run by this "
           "command)" if supplied else "solo5: ok")
