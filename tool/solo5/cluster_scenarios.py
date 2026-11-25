"""The cluster images of tool/solo5 and the runs played against them.

Three Raft servers (targets/raft), each one image booted with its own node number. A
scenario is a list of moves of tool/solo5/cluster.py; every move is made by the harness,
which is the network and the client. The moves name roles, never a server number or a
term: who leads is whatever the run made of it.

    ["faults", {...}]        what the switch does to frames between nodes from here on:
                             the share dropped, the share forwarded twice, the share also
                             put on the link of a node it is not addressed to, and the
                             range of the forwarding delay in milliseconds
    ["leader"]               wait until a running node says it leads
    ["op", ["put", K, V]]    one client operation under the next request number, sent to
    ["op", ["get", K]]       whoever leads and sent again while it has no `ok`
    ["kill", WHO]            SIGKILL; WHO is "leader", "follower", or a node number
    ["boot", "down"]         boot a killed node again on its block image
    ["isolate", WHO]         cut every frame between WHO and the two others, both ways
    ["heal"]                 end the cut
    ["succession"]           wait until a node leads a later term than any node led when
                             the last kill or cut was made
    ["run", MS]              let the cluster run

The log of a server holds eight entries and every operation, and every repeat of one, is
an entry; the workloads stay below that.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODES = 3
NODE = ROOT / "targets" / "raft" / "node.json"

# Driver mutants of the path between nodes: one textual edit of driver.c each.
DRIVERS = {
    # A message is handled whatever node it is addressed to.
    "delivers-any-address": {
        "old": "kind->role == R_RECV && !mine()) {",
        "new": "kind->role == R_RECV && !mine() && 0) {"},
    # A message is addressed to the node after the one the step named.
    "sends-to-next-node": {
        "old": "    address(frame, fields[0]);\n",
        "new": "    address(frame, (fields[0] + 1) % cluster);\n"},
    # A message says it comes from the node it is sent to.
    "claims-another-sender": {
        "old": "    pack(frame + BODY + 8, self);\n",
        "new": "    pack(frame + BODY + 8, fields[0]);\n"},
    # A send is reported as performed and no frame leaves.
    "swallows-send": {
        "old": "    case R_SEND: message(kind, fields); break;\n",
        "new": "    case R_SEND: if (fields[0] != (self + 1) % cluster) message(kind, fields); break;\n"},
}
# Edits of targets/raft/spec.shen, by their names in targets/raft/check.py (MUTANTS) and
# targets/raft/mutants.json.
SPECS = ("recovery-forgets-vote", "leader-overwrites-last", "local-get")

IMAGES = {"raft": {}}
IMAGES.update({f"raft~{name}": {"driver": name} for name in DRIVERS})
IMAGES.update({f"raft~{name}": {"spec": name} for name in SPECS})


def manifest(image):
    return NODE


def source(image):
    """The Shen source an image is compiled from: the node's, or its one edit."""
    text = (NODE.parent / "spec.shen").read_text()
    if "spec" in image:
        from targets.raft.check import mutate
        text = mutate(text, image["spec"])
    return text


CALM = ["faults", {"drop": 0.0, "dup": 0.0, "flood": 0.05, "delay": [0.0, 4.0]}]
LOSSY = ["faults", {"drop": 0.10, "dup": 0.10, "flood": 0.10, "delay": [0.0, 15.0]}]


def put(key, value):
    return ["op", ["put", key, value]]


def get(key):
    return ["op", ["get", key]]


SCENARIOS = {
    # A leader is elected and serves puts and gets.
    "elect-and-serve": {"image": "raft", "moves": [
        CALM, ["leader"], put(0, 11), get(0), put(1, 13), put(0, 12), get(0), ["run", 200]]},
    # The leader is killed; another is elected, and what was acknowledged reads back.
    "kill-the-leader": {"image": "raft", "moves": [
        CALM, ["leader"], put(0, 11), put(1, 13), ["kill", "leader"], ["succession"],
        get(0), put(0, 12), get(0), get(1), ["run", 200]]},
    # A follower is killed and booted again on its disk. Then the leader is killed, so
    # the two that remain, one of them the restarted node, are the whole majority.
    "restart-from-disk": {"image": "raft", "moves": [
        CALM, ["leader"], put(0, 11), ["kill", "follower"], put(1, 13), ["boot", "down"],
        ["run", 600], ["kill", "leader"], ["succession"], get(1), get(0), ["run", 200]]},
    # The leader is cut off; the others elect one and take a write; the cut ends.
    "partition-heals": {"image": "raft", "moves": [
        CALM, ["leader"], put(0, 11), ["isolate", "leader"], ["succession"], put(0, 12),
        ["heal"], ["run", 600], get(0), ["run", 200]]},
    # Frames between nodes are dropped, duplicated and delayed.
    "lossy-links": {"image": "raft", "moves": [
        LOSSY, ["leader"], put(0, 11), get(0), put(1, 13), put(0, 12), get(0), ["run", 200]]},
    # All of it in one run. Only safety is asked of this one: no operation has to be
    # answered.
    "lossy-kills-and-cuts": {"image": "raft", "moves": [
        LOSSY, ["leader"], put(0, 11), ["kill", "leader"], ["succession"], put(1, 13),
        ["boot", "down"], ["run", 300], ["isolate", "leader"], ["succession"], put(0, 12),
        ["heal"], ["run", 400], get(0), ["run", 200]]},
}
# What each scenario must show, written from the scenario: the value every get returns,
# by request number (requests count from 1 in the order of the moves), whether every
# operation is answered, and how many kills (the kill that ends the run excluded), boots
# after a kill and cuts there are.
EXPECT = {
    "elect-and-serve": {"gets": {2: 11, 5: 12}, "answered": 5, "kills": 0, "restarts": 0, "cuts": 0},
    "kill-the-leader": {"gets": {3: 11, 5: 12, 6: 13}, "answered": 6, "kills": 1, "restarts": 0, "cuts": 0},
    "restart-from-disk": {"gets": {3: 13, 4: 11}, "answered": 4, "kills": 2, "restarts": 1, "cuts": 0},
    "partition-heals": {"gets": {3: 12}, "answered": 3, "kills": 0, "restarts": 0, "cuts": 1},
    "lossy-links": {"gets": {2: 11, 5: 12}, "answered": 5, "kills": 0, "restarts": 0, "cuts": 0},
    "lossy-kills-and-cuts": {"gets": None, "answered": None, "kills": 1, "restarts": 1, "cuts": 1},
}
# The scenarios each mutant image is run on, one seed each. The driver mutants get the
# first and the lossy one; the specification mutants get every scenario, because which
# history shows an edit of Raft is not known beforehand.
MUTANT_RUNS = {**{f"raft~{name}": ["elect-and-serve", "lossy-links"] for name in DRIVERS},
               **{f"raft~{name}": list(SCENARIOS) for name in SPECS}}
