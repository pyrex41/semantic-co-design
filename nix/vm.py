# Test script of checks.<linux>.real-vm (nix/vm.nix). Machines: client, node0, node1.
# Every fault here is the real one: a signal from systemd, the virtual machine's exit,
# the virtual link, iptables and tc. Nothing is judged until the end, and then only from
# the files the nodes left and what the client received.
import itertools
import json
import subprocess
import time

RUN = "@run@"
JOB, BEAT = "shen-node-job.service", "shen-node-heartbeat.service"
DATA = "/var/lib/shen-node"
nodes = [node0, node1]
numbers = itertools.count(1)


def load(name, answers, seed, pause=0):
    """Submit to the AcceptedJob node on machine `name` until `answers` submissions are answered."""
    return json.loads(client.succeed(
        f"{RUN} examples.accepted_job.load /tmp/{name}.json {name}:7000 {answers} {pause} 120 {seed}"))


def background(name, answers, seed, pause):
    client.succeed(
        f"rm -f /tmp/{name}.exit; systemd-run --unit=load-{name}-{seed} /bin/sh -c "
        f"'{RUN} examples.accepted_job.load /tmp/{name}.json {name}:7000 {answers} {pause} 300 {seed}"
        f" > /tmp/{name}.out; echo $? > /tmp/{name}.exit'")


def finished(name):
    client.wait_until_succeeds(f"test -e /tmp/{name}.exit", timeout=400)
    assert client.succeed(f"cat /tmp/{name}.exit").strip() == "0", "the workload ran out of time"
    return json.loads(client.succeed(f"cat /tmp/{name}.out"))


def answered(name):
    return json.loads(client.succeed(f"cat /tmp/{name}.json"))["answered"]


def ask(node, tag):
    """One request to a heartbeat node: ["report", Request, Seen, Beats, Last]."""
    request = json.dumps([tag, next(numbers)])
    return json.loads(client.succeed(
        f"{RUN} host.ask node{node}:7001 60 '{request}' /tmp/beat{node}.json"))


def seen():
    return [ask(node, "status")[2] for node in (0, 1)]


def both_hear_more(than, what):
    deadline = time.monotonic() + 120
    while not all(now > before for now, before in zip(seen(), than)):
        assert time.monotonic() < deadline, what
        time.sleep(0.5)


def up(node, unit, port):
    node.wait_for_unit(unit)
    node.wait_for_open_port(port)


start_all()
client.wait_for_unit("multi-user.target")
for node in nodes:
    up(node, JOB, 7000)

with subtest("power loss right after the first acknowledged submission"):
    first = load("node0", 1, seed=0)
    node0.crash()
    assert first["statuses"] == {"ok": 1}, first
    node0.start()
    up(node0, JOB, 7000)
    rows = node0.succeed(f"cat {DATA}/job/disk.log").splitlines()
    assert rows, "the client was told ok and the durable file is empty after the power loss"
    load("node0", 4, seed=1)

with subtest("kill -9 in the middle of a workload"):
    background("node0", 80, seed=2, pause=0.05)
    for _ in range(6):
        time.sleep(0.7)
        node0.systemctl(f"kill -s KILL {JOB}")
    killed = finished("node0")
    restarts = int(node0.succeed(f"systemctl show -p NRestarts --value {JOB}"))
    assert restarts >= 3, f"systemd restarted the node {restarts} times"
    assert killed["requests"] > killed["answers"], "no submission had to be retried"

with subtest("the link between client and node is cut and restored"):
    up(node0, JOB, 7000)
    background("node0", 100, seed=3, pause=0.05)
    time.sleep(0.5)
    node0.block()
    time.sleep(1)
    before = answered("node0")
    time.sleep(3)
    cut = answered("node0")
    assert cut == before, f"{cut - before} submissions were answered over a cut link"
    node0.unblock()
    finished("node0")
    assert answered("node0") > cut

with subtest("delay and loss on a node's link, and a kill under them"):
    quick = load("node1", 5, seed=4)
    node1.succeed("tc qdisc add dev eth1 root netem delay 60ms loss 15%")
    slow = load("node1", 10, seed=5)
    node1.systemctl(f"kill -s KILL {JOB}")
    load("node1", 5, seed=6)
    node1.succeed("tc qdisc del dev eth1 root")
    assert slow["fastest_ms"] >= 50, f"an answer took {slow['fastest_ms']} ms through a 60 ms delay"
    assert quick["fastest_ms"] < slow["fastest_ms"], (quick, slow)

with subtest("two nodes ping each other"):
    for node in nodes:
        node.succeed(f"systemctl start {BEAT}")
        up(node, BEAT, 7001)
    both_hear_more([0, 0], "the nodes never heard each other")

with subtest("partition between the nodes, then heal"):
    node0.succeed("iptables -I INPUT -s node1 -j DROP")
    node1.succeed("iptables -I INPUT -s node0 -j DROP")
    time.sleep(1)
    heard = seen()
    time.sleep(2)
    apart = seen()
    assert apart == heard, f"pings crossed the partition: {heard} then {apart}"
    node0.succeed("iptables -D INPUT -s node1 -j DROP")
    node1.succeed("iptables -D INPUT -s node0 -j DROP")
    both_hear_more(apart, "pings did not resume after the heal")

with subtest("delay and loss between the nodes, and a kill"):
    node0.succeed("tc qdisc add dev eth1 root netem delay 30ms loss 10%")
    both_hear_more(seen(), "no ping got through the delay and loss")
    node0.systemctl(f"kill -s KILL {BEAT}")
    up(node0, BEAT, 7001)
    node0.succeed("tc qdisc del dev eth1 root")

with subtest("power loss of one of the two nodes"):
    node1.crash()
    node1.start()
    node1.wait_for_unit("multi-user.target")
    node1.succeed(f"systemctl start {BEAT}")
    up(node1, BEAT, 7001)
    both_hear_more(seen(), "pings did not resume after the power loss")

with subtest("a cancelled timer stays silent"):
    stopped = [ask(node, "stop") for node in (0, 1)]
    time.sleep(0.5)
    after = [ask(node, "status") for node in (0, 1)]
    assert [a[3] for a in after] == [s[3] for s in stopped], (stopped, after)

with subtest("conformance of every log to the Shen source"):
    for node in nodes:
        node.succeed(f"systemctl stop {JOB} {BEAT}")
        node.copy_from_machine(f"{DATA}/job", node.name)
        node.copy_from_machine(f"{DATA}/heartbeat", node.name)
    for file in ("node0.json", "node1.json", "beat0.json", "beat1.json"):
        client.copy_from_machine(f"/tmp/{file}", "client")
    out = client.out_dir

    def conform(packed, *arguments):
        result = subprocess.run(
            ["@conform@", "--wasm", f"{packed}/node.wasm", f"{packed}/node.json", *arguments],
            capture_output=True, text=True)
        print(result.stdout, result.stderr)
        assert result.returncode == 0, f"conformance failed: {result.stdout} {result.stderr}"
        return json.loads(result.stdout)

    jobs = {name: conform("@job@", f"{out}/{name}/job", f"--received=0={out}/client/{name}.json")
            for name in ("node0", "node1")}
    for facts in jobs.values():
        assert facts["events"] and facts["received"] and facts["rows"], facts
    # node0 booted, lost power, and was killed at least once; node1 booted and was killed once.
    assert jobs["node0"]["segments"] >= 3 and jobs["node1"]["segments"] >= 2, jobs
    beat = conform("@heartbeat@", f"{out}/node0/heartbeat", f"{out}/node1/heartbeat",
                   f"--received=0={out}/client/beat0.json", f"--received=1={out}/client/beat1.json")
    # Two boots, a kill of node 0 and a power loss of node 1.
    assert beat["nodes"] == 2 and beat["segments"] >= 4 and beat["received"], beat
