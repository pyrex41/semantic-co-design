"""python3 nix/packed.py <node-run> <node-conform> <accepted_job> <its mutant> <heartbeat>

The flake check packed-node. The node packages, launchers and commands that the
NixOS machine tests deploy (nix/vm.nix), on loopback, where the only fault is
kill -9 and this script plays systemd. It shows that the packages run and that
the conformance command accepts and rejects what the machine tests expect; it
shows nothing about a kernel, a network or a disk.
"""
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

RUN, CONFORM, JOB, MUTANT, BEAT = sys.argv[1:]


def ports(count):
    sockets = [socket.create_server(("127.0.0.1", 0)) for _ in range(count)]
    found = [f"127.0.0.1:{s.getsockname()[1]}" for s in sockets]
    for s in sockets:
        s.close()
    return found


def serve(package, node, data, client, peers, wait=True):
    """Start a node; return once its client port answers, when its boot record is durable."""
    process = subprocess.Popen([f"{package}/bin/node-host", str(node), str(data), client, *peers],
                               stdin=subprocess.DEVNULL)
    host, _, port = client.rpartition(":")
    deadline = time.monotonic() + 60
    while wait and process.poll() is None and time.monotonic() < deadline:
        try:
            socket.create_connection((host, int(port)), timeout=1).close()
            break
        except OSError:
            time.sleep(0.01)
    return process


def kill(process):
    process.send_signal(signal.SIGKILL)
    process.wait(timeout=30)


def run(*arguments, check=True):
    result = subprocess.run([RUN, *map(str, arguments)], capture_output=True, text=True, timeout=120)
    assert not check or result.returncode == 0, (arguments, result.stdout, result.stderr)
    return json.loads(result.stdout) if result.returncode == 0 else None


def conform(packed, *arguments, wasm=None):
    return subprocess.run([CONFORM, "--wasm", f"{wasm or packed}/share/node/node.wasm",
                           f"{packed}/share/node/node.json", *map(str, arguments)],
                          capture_output=True, text=True, timeout=300)


def accepted(result, what):
    assert result.returncode == 0, f"{what}: {result.stdout} {result.stderr}"
    return json.loads(result.stdout)


def job(package, work, kills):
    """One AcceptedJob node under a workload, killed and started again `kills` times."""
    data, record = work / "data", work / "client.json"
    client, peer = ports(2)
    node = serve(package, 0, data, client, [peer])
    try:
        first = run("examples.accepted_job.load", record, client, 2, 0, 60, 0)
        assert first["statuses"] == {"ok": 2} or first["statuses"].get("conflict"), first
        load = subprocess.Popen([RUN, "examples.accepted_job.load", str(record), client, "60", "0.02", "60", "1"],
                                stdout=subprocess.DEVNULL)
        for _ in range(kills):
            time.sleep(0.4)
            kill(node)
            node = serve(package, 0, data, client, [peer])
        assert load.wait(timeout=90) == 0, "the workload ran out of time"
    finally:
        kill(node)
    return data, record


def main(report=print):
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name in ("job", "mutant", "beat"):
            (work / name).mkdir()

        data, record = job(JOB, work / "job", 4)
        facts = accepted(conform(JOB, data, f"--received=0={record}"), "accepted_job")
        assert facts["segments"] == 5 and facts["received"] and not facts["violations"], facts
        # Teeth of --received: an output the client never got from this node.
        forged = work / "forged.json"
        forged.write_text(json.dumps(json.loads(record.read_text())["got"] + [["reply", 9999, "ok", 0]]))
        result = conform(JOB, data, f"--received=0={forged}")
        assert result.returncode and result.stderr.startswith("causality: a client received"), result
        # An output the client holds once more than the node emitted it, and no log at all.
        forged.write_text(json.dumps(2 * json.loads(record.read_text())["got"]))
        result = conform(JOB, data, f"--received=0={forged}")
        assert result.returncode and result.stderr.startswith("causality: a client received"), result
        result = conform(JOB, work / "nowhere")
        assert result.returncode and result.stderr.startswith("log: node 0 has no log"), result
        report(f"packed accepted_job: {facts['events']} events in {facts['segments']} segments "
               f"conform; {facts['received']} outputs the client received are all in the log; "
               f"one forged output, the outputs received twice over and a directory with no log "
               f"are rejected")

        data, record = job(MUTANT, work / "mutant", 1)
        against = conform(JOB, data, f"--received=0={record}", wasm=MUTANT)
        assert against.returncode and against.stderr.startswith("refinement"), against
        own = conform(MUTANT, data, f"--received=0={record}")
        broken = [name for name, _ in json.loads(own.stdout)["violations"]]
        assert own.returncode == 1 and "inv.ok-is-durable" in broken, own
        report("packed mutant reply-before-ack: its log is rejected against spec.shen "
               "(refinement) and, against its own source, by inv.ok-is-durable")

        clients, peers = ports(2), ports(2)
        data = [work / "beat" / f"node{n}" for n in (0, 1)]
        records = [work / "beat" / f"client{n}.json" for n in (0, 1)]
        wrong = serve(BEAT, 0, data[0], clients[0], peers[:1], wait=False)
        assert wrong.wait(timeout=30), "a two-node package started as a one-node system"
        nodes = []
        try:
            nodes += [serve(BEAT, n, data[n], clients[n], peers) for n in (0, 1)]
            ask = lambda n, tag, number: run("host.ask", clients[n], 30, json.dumps([tag, number]), records[n])
            deadline = time.monotonic() + 60
            while not all(ask(n, "status", 1)[2] for n in (0, 1)):
                assert time.monotonic() < deadline, "the heartbeat nodes never heard each other"
            kill(nodes[1])
            nodes[1] = serve(BEAT, 1, data[1], clients[1], peers)
            while not all(ask(n, "status", 2)[2] for n in (0, 1)):
                assert time.monotonic() < deadline, "pings did not resume after the kill"
            for n in (0, 1):
                ask(n, "stop", 3)
        finally:
            for node in nodes:
                kill(node)
        facts = accepted(conform(BEAT, *data, *(f"--received={n}={records[n]}" for n in (0, 1))),
                         "heartbeat")
        assert facts["nodes"] == 2 and facts["segments"] == 3 and facts["received"], facts
        report(f"packed heartbeat: two nodes, {facts['events']} events in {facts['segments']} "
               f"segments conform")
    report("packed-node: ok")


if __name__ == "__main__":
    main()
