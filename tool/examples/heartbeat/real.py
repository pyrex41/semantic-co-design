"""Heartbeat as two real processes. Run: nix develop -c python3 -m tool real heartbeat

Two nodes ping each other over TCP on 20 ms timers. One is killed with kill -9
and restarted, twice; then node 0 is killed and started as after a reboot of its
machine, with a clock that begins again at zero; both are then asked to stop.
tool/host/conform.py replays both logs and puts them in one causal order for
the invariant.
"""
from pathlib import Path
import random
import shutil
from tempfile import TemporaryDirectory
import time

from ...check import CheckFailure, require
from ...host import Client, Cluster, bundle, wal
from ...host.conform import conform
from ...schema import Schema

HERE = Path(__file__).parent
RUNS = 3


def run(packed, directory, seed):
    rng = random.Random(seed)
    cluster = Cluster(packed, directory)
    clients = [Client(address) for address in cluster.clients]
    kills = 0

    def ask(node, request):
        clients[node].send(request)
        answer = clients[node].wait(lambda o: o[:2] == ("report", request[1]), 5)
        require(answer, f"seed {seed}: node {node} did not answer {request}")
        return answer

    try:
        for node in (0, 1):
            cluster.start(node)
        for _ in range(2):
            time.sleep(rng.uniform(0.1, 0.25))
            victim = rng.randrange(2)
            kills += cluster.kill(victim)
            time.sleep(rng.uniform(0, 0.08))
            cluster.start(victim)
        time.sleep(rng.uniform(0.1, 0.25))
        kills += cluster.kill(0)
        cluster.start(0, env={"HOST_CLOCK_ZERO": "1"})
        time.sleep(0.2)
        before = [ask(node, ("status", node)) for node in (0, 1)]
        stopped = [ask(node, ("stop", 10 + node)) for node in (0, 1)]
        time.sleep(0.15)                    # several timer periods: a cancelled timer stays silent
        after = [ask(node, ("status", 20 + node)) for node in (0, 1)]
    finally:
        cluster.stop()
        for client in clients:
            client.close()
    require(all(b[2] and b[3] and b[4] for b in before),
            f"seed {seed}: a node heard nothing, sent nothing or read no clock: {before}")
    require([a[3] for a in after] == [s[3] for s in stopped],
            f"seed {seed}: a node kept beating after its timer was cancelled")
    return cluster.data, kills


def edited(data, work, node, change):
    copies = []
    for n, directory in enumerate(data):
        copies.append(work / f"edited{n}")
        shutil.rmtree(copies[n], ignore_errors=True)
        shutil.copytree(directory, copies[n])
    records = wal.read(copies[node] / "events.log")[0]
    change(records)
    (copies[node] / "events.log").write_bytes(b"".join(map(wal.frame, records)))
    return copies


def main(report=print):
    schema = Schema(HERE / "node.json")
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        packed = bundle(schema, work / "spec", nodes=2)
        total = {"events": 0, "segments": 0, "pairs": 0}
        kills, roles = 0, {}
        for seed in range(RUNS):
            data, killed = run(packed, work / f"run{seed}", seed)
            found = conform(schema, data, module=packed["wasm"])
            require(not found["violations"], f"seed {seed}: {found['violations']}")
            require(found["segments"] == killed + 2, f"seed {seed}: segments and kills differ")
            kills += killed
            for name in total:
                total[name] += found[name]
            for step in found["trace"]["steps"]:
                if step.get("event") and step["kind"] != "client":
                    role = schema.events[step["event"][0]].role
                    roles[role] = roles.get(role, 0) + 1
        require(all(roles.get(r) for r in ("recv", "timer", "clock", "request", "restart")),
                f"heartbeat: an event role never occurred: {roles}")
        report(f"real runs: {RUNS} runs of two nodes on loopback, {kills} kill -9, "
               f"{total['segments']} log segments, {total['events']} logged events replayed through "
               f"shen-jit ({total['pairs']} distinct pairs), events by role {dict(sorted(roles.items()))}; "
               f"hb.no-ping-from-nowhere holds on the interleaved logs")

        # Teeth, on the last run. A ping credited to a send that said something else, a tick
        # from a timer that was replaced, a clock that went back, and a sender whose log stops
        # before its sends are each rejected by causality; an edited count by refinement.
        records = wal.read(data[0] / "events.log")[0]
        seq = lambda tag, nth=0: [r["seq"] for r in records if r.get("event", [0])[0] == tag][nth]
        ping, tick, late, clock = seq("pinged", -1), seq("tick", 1), seq("tick", 2), seq("clocked", -1)
        # The reboot is in these files: across the last boot record the clock goes back, which
        # conforms; the last reading set back inside its segment does not.
        boot = [r["seq"] for r in records if "boot" in r][-1]
        readings = [(r["seq"] > boot, r["event"][2]) for r in records if r.get("event", [0])[0] == "clocked"]
        after = [reading for rebooted, reading in readings if rebooted]
        require(len(after) > 1 and max(after) < min(reading for rebooted, reading in readings if not rebooted),
                f"heartbeat: no clock that started again: {readings}")
        def cause(rs): rs[ping]["cause"][2] += 1
        def stale(rs): rs[late]["cause"] = rs[tick]["cause"]
        def back(rs): rs[clock]["event"][2] = 0
        def count(rs): rs[ping]["post"][1] += 1
        def cut(rs): del rs[2:]
        cases = [("a ping's cause", 0, cause, "causality"), ("a stale timer", 0, stale, "causality"),
                 ("a clock set back", 0, back, "causality"), ("a count", 0, count, "refinement"),
                 ("the sender's log cut short", 1, cut, "causality")]
        for label, node, change, reason in cases:
            try:
                conform(schema, edited(data, work, node, change), module=packed["wasm"])
            except CheckFailure as failure:
                require(str(failure).startswith(reason), f"teeth: {label}: {failure}")
            else:
                raise CheckFailure(f"teeth: logs with {label} edited were accepted")
        report(f"clock: after node 0's reboot its {len(after)} readings begin at {after[0]} ms, below "
               f"every reading before it, and the logs conform")
        report(f"log teeth: rejected with {', '.join(f'{c[0]} ({c[3]})' for c in cases)}")
    report("heartbeat on the real host: ok")
