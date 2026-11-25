"""The images tool/solo5 builds and the scripted runs it plays against them.

An image is a node (a manifest), optionally with one driver mutant or one specification
mutant (tool/solo5/mutants.py). A scenario is a list of boots of one image on one block
image; see tool/solo5/run.py for the moves.

Event numbers in a `hold` count from 1 within a boot. For AcceptedJob a boot starts
restart (1), loaded (2), loaded (3), so the first request is event 4 and the
acknowledgement of its write is event 5.
"""
from pathlib import Path

from .mutants import DRIVERS, specs

TOOL = Path(__file__).parent.parent
NODES = {"accepted_job": "examples/accepted_job/node.json", "probe": "sim/probe/node.json",
         "timer": "solo5/timer/node.json", "store": "solo5/store/node.json"}
# Which driver mutants are built for which node: the ones whose effect that node can show.
DRIVER_MUTANTS = {
    "accepted_job": ["ack-without-write", "torn-row", "stale-read", "late-cut", "keeps-old-state",
                     "swallows-output", "accepts-any-field", "acks-failed-write",
                     "dedupes-requests", "repeats-output"],
    "probe": ["late-cut"],
    "timer": ["early-timer", "ignores-cancel", "stale-clock", "half-tick", "slow-tick",
              "wrapping-deadline"],
    "store": ["reads-own-write", "ignores-failed-read"],
}
NUM_MAX = 2 ** 61 - 1                  # the largest integer a specification computes with

IMAGES = {name: {"node": name} for name in NODES}
for _node, _mutants in DRIVER_MUTANTS.items():
    for _mutant in _mutants:
        assert _mutant in DRIVERS
        IMAGES[f"{_node}~{_mutant}"] = {"node": _node, "driver": _mutant}
for _name in specs():
    IMAGES[_name] = {"node": _name.split("~")[0], "spec": _name}
# The images that are also linked for the hvt tender (built, never run here).
HVT = ("accepted_job",)


def manifest(image):
    return TOOL / NODES[image["node"]]


def source(image):
    """The Shen source an image is compiled from: the node's, or its one edit."""
    text = (manifest(image).parent / "spec.shen").read_text()
    if "spec" in image:
        old, new = specs()[image["spec"]]
        assert text.count(old) == 1, f"mutation site of {image['spec']}"
        text = text.replace(old, new)
    return text


def submit(request, key, digest):
    return ["send", ["submit", request, key, digest]]


ETHER = "ff" * 6 + "020000000002"      # to everyone, from the client


def word(value):
    return value.to_bytes(8, "little").hex()


# ---- AcceptedJob: three keys against two receipts, two digests

JOB = {
    # The run the task names: a kill between the write and the reply, a restart on the same
    # block image, and a retry. Around it, every reply the node can give.
    "job-kill-between-write-and-reply": {"image": "accepted_job", "boots": [
        {"moves": [submit(0, 7, 11),          # written, acknowledged, answered ok 0
                   submit(1, 7, 22)]},        # the other digest: conflict 0
        {"hold": [4, 1],                      # event 4 is the request; die after its write
         "moves": [submit(2, 8, 11)]},
        {"moves": [submit(3, 8, 11),          # the retry: ok 1, and no second write
                   submit(4, 9, 11),          # a third key: full
                   submit(5, 8, 22)]},        # conflict 1
    ]},
    # The node dies with the step computed and nothing written.
    "job-kill-before-write": {"image": "accepted_job", "boots": [
        {"hold": [4, 0], "moves": [submit(0, 7, 11)]},
        {"moves": [submit(1, 7, 11)]},        # nothing was durable: written now, ok 0
    ]},
    # The node dies holding the acknowledgement, before the reply leaves.
    "job-kill-before-reply": {"image": "accepted_job", "boots": [
        {"hold": [5, 0], "moves": [submit(0, 7, 11)]},
        {"moves": [submit(1, 7, 11),          # durable already: ok 0 without a write
                   submit(2, 7, 22)]},
    ]},
    # Frames that are not requests must not reach the step: another ethertype, an
    # undeclared tag (99), the tag of an event that is not a request (11, `written`), a
    # `submit` (10) whose first number is 2^62, outside Shen's exact range, and a
    # `submit` that announces three integers where four are due.
    "job-frames-that-are-not-requests": {"image": "accepted_job", "boots": [
        {"moves": [["raw", ETHER + "0800" + "00" * 46],
                   ["raw", ETHER + "88b5" + word(4) + word(99) + "00" * 30],
                   ["raw", ETHER + "88b5" + word(2) + word(11) + "00" * 30],
                   ["raw", ETHER + "88b5" + word(4) + word(10) + word(2 ** 62) + "00" * 22],
                   ["raw", ETHER + "88b5" + word(3) + word(10) + "00" * 30],
                   submit(0, 7, 11)]},
    ]},
    # The same frame three times in one boot. Whether a repeated request is answered
    # again is the specification's decision (here: yes, from the stored row), so a loop
    # that drops a repeat, or one that sends an answer twice, is told apart from this one.
    "job-same-request-three-times": {"image": "accepted_job", "boots": [
        {"moves": [submit(0, 7, 11), submit(0, 7, 11), submit(0, 7, 11)]},
    ]},
    # The device refuses a write (see `fsize` in tool/solo5/run.py). The node must stop
    # without acknowledging, the client must not hear `ok`, and the next boot, on a
    # device that works, must accept the retry.
    "job-device-refuses-a-write": {"image": "accepted_job", "boots": [
        {"moves": [submit(0, 7, 11)]},
        {"fsize": 0, "moves": [submit(1, 8, 11)]},
        {"moves": [submit(2, 8, 11)]},
    ]},
}


# ---- the store: reads on request, a write and a read in one step, a failing device

def put(key, value):
    return ["send", ["put", key, value]]


def get(key):
    return ["send", ["get", key]]


def swap(key, other, value):
    return ["send", ["swap", key, other, value]]


STORE = {
    # One step writes row 1 and then reads row 1. The device would answer with the new
    # value, which no simulated schedule does, so the driver stops before the read. The
    # write was performed: the next boot reads 5.
    "store-read-of-a-row-the-step-wrote": {"image": "store", "boots": [
        {"moves": [put(1, 4), swap(1, 1, 5)]},
        {"moves": [get(1)]},
    ]},
    # One step writes row 1 and then reads row 2: nothing to refuse.
    "store-write-then-read-of-another-row": {"image": "store", "boots": [
        {"moves": [put(2, 9), swap(1, 2, 5), get(1)]},
    ]},
    # A read the device fails (the block file is cut down to one block under the running
    # process, and row 3 is read), then a write to a row outside the device. Both stop
    # the node. Row 0, written before, is still there.
    "store-failed-read-and-row-outside-the-device": {"image": "store", "boots": [
        {"moves": [put(0, 7), ["shrink", 1], get(3)]},
        {"moves": [put(99, 1)]},
        {"moves": [get(0), get(3)]},
    ]},
}

# ---- the simulator's probe: one request makes write, say, write, say

PROBE = {
    f"probe-kill-after-{cut}-of-4-effects": {"image": "probe", "boots": [
        {"hold": [3, cut], "moves": [["send", ["go", 5]]]},     # restart, loaded, go
        {"moves": [["send", ["go", 6]]]},
    ]} for cut in range(5)
}
# The probe without a clause for an acknowledgement: the step has no result and the node
# must stop, as the simulator's run ends in an error.
TRAP = {"probe-step-without-result": {"image": "probe~no-ack-clause", "stops": True, "boots": [
    {"moves": [["send", ["go", 5]]]}]}}

# ---- timers and the clock

TIMER = {
    # Delays are milliseconds and a client move takes about a tenth of a second. Timer 2
    # is armed after timer 1 and fires first; timer 1 is re-armed, so it fires once, 900
    # after the second arming; timer 3 is cancelled long before it is due, and the run
    # outlasts its due time. Fifteen events in all.
    "timer-order-and-cancel": {"image": "timer", "boots": [
        {"moves": [["send", ["arm", 1, 5000]], ["send", ["arm", 2, 400]],
                   ["send", ["arm", 3, 1500]], ["send", ["arm", 1, 900]],
                   ["send", ["disarm", 3]], ["events", 15], ["sleep", 1.0]]},
    ]},
    # A timer armed before a kill does not fire after the restart, though the second boot
    # outlasts its due time.
    "timer-does-not-cross-a-kill": {"image": "timer", "boots": [
        {"moves": [["send", ["arm", 1, 400]]]},
        {"moves": [["send", ["arm", 2, 100]], ["events", 6], ["sleep", 0.6]]},
    ]},
    # The driver's timer table has 16 entries. The seventeenth timer stops the node, and
    # none of the sixteen fires after the restart.
    "timer-table-full": {"image": "timer", "boots": [
        {"moves": [["send", ["arm", n, 600000]] for n in range(1, 18)]},
        {"moves": [["send", ["arm", 1, 100]], ["events", 6], ["sleep", 0.3]]},
    ]},
    # Due times far away: one chosen so that the due time in nanoseconds, taken modulo
    # 2^64, is the first millisecond of the clock (see `due` in tool/solo5/run.py), and
    # the largest delay there is. The node must wait for them without using the processor.
    "timer-far-due-time": {"image": "timer", "boots": [
        {"idle_cpu": 0.3,
         "moves": [["send", ["arm", 1, {"due": 18446744073710}]], ["send", ["arm", 2, NUM_MAX]],
                   ["sleep", 3.0]]},
    ]},
}

SCENARIOS = {**JOB, **PROBE, **TRAP, **STORE, **TIMER}

# Scenarios replayed on mutant images: the comparison must reject each of these runs.
MUTANT_RUNS = {
    "accepted_job~ack-without-write": "job-kill-between-write-and-reply",
    "accepted_job~torn-row": "job-kill-between-write-and-reply",
    "accepted_job~stale-read": "job-kill-between-write-and-reply",
    "accepted_job~late-cut": "job-kill-before-write",
    "accepted_job~keeps-old-state": "job-kill-between-write-and-reply",
    "accepted_job~swallows-output": "job-kill-between-write-and-reply",
    "accepted_job~accepts-any-field": "job-frames-that-are-not-requests",
    "accepted_job~reply-before-ack": "job-kill-between-write-and-reply",
    "accepted_job~reply-then-write": "job-kill-between-write-and-reply",
    "accepted_job~acks-failed-write": "job-device-refuses-a-write",
    "accepted_job~dedupes-requests": "job-same-request-three-times",
    "accepted_job~repeats-output": "job-same-request-three-times",
    "store~reads-own-write": "store-read-of-a-row-the-step-wrote",
    "store~ignores-failed-read": "store-failed-read-and-row-outside-the-device",
    "probe~late-cut": "probe-kill-after-2-of-4-effects",
    "timer~early-timer": "timer-order-and-cancel",
    "timer~ignores-cancel": "timer-order-and-cancel",
    "timer~stale-clock": "timer-order-and-cancel",
    "timer~half-tick": "timer-order-and-cancel",
    "timer~slow-tick": "timer-order-and-cancel",
    "timer~wrapping-deadline": "timer-far-due-time",
}
for _image, _scenario in MUTANT_RUNS.items():
    SCENARIOS[f"{_scenario}@{_image}"] = {**SCENARIOS[_scenario], "image": _image, "mutant": True}
