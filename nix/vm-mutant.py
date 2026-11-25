# Test script of checks.<linux>.real-vm-mutant (nix/vm.nix). Machines: client, node0.
# node0 runs AcceptedJob compiled from the mutant reply-before-ack. The test passes only
# if conformance of its log FAILS, once against spec.shen and once against its own source.
import json
import subprocess

RUN = "@run@"
JOB, DATA = "shen-node-job.service", "/var/lib/shen-node"


def load(answers, seed):
    return json.loads(client.succeed(
        f"{RUN} examples.accepted_job.load /tmp/node0.json node0:7000 {answers} 0 120 {seed}"))


def conform(manifest):
    result = subprocess.run(
        ["@conform@", "--wasm", "@job@/node.wasm", f"{manifest}/node.json",
         f"{node0.out_dir}/node0/job", f"--received=0={node0.out_dir}/client/node0.json"],
        capture_output=True, text=True)
    print(result.returncode, result.stdout, result.stderr)
    return result


start_all()
client.wait_for_unit("multi-user.target")
node0.wait_for_unit(JOB)
node0.wait_for_open_port(7000)

first = load(3, seed=0)
assert first["statuses"].get("ok"), first
node0.systemctl(f"kill -s KILL {JOB}")
load(3, seed=1)

node0.succeed(f"systemctl stop {JOB}")
node0.copy_from_machine(f"{DATA}/job", "node0")
client.copy_from_machine("/tmp/node0.json", "client")

with subtest("the mutant's log does not refine spec.shen"):
    against = conform("@spec@")
    assert against.returncode != 0 and against.stderr.lstrip().startswith("refinement"), (
        "a node compiled from reply-before-ack passed conformance against spec.shen")

with subtest("judged against its own source, it tells a client ok before the row is durable"):
    own = conform("@job@")
    assert own.returncode == 1 and own.stdout, "the mutant's log does not even refine the mutant"
    broken = [name for name, _ in json.loads(own.stdout)["violations"]]
    assert "inv.ok-is-durable" in broken, broken
