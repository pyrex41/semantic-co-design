"""Build the unikernels with Nix and run them under solo5-spt, where this machine can.

Solo5 has no tender for macOS. On a Mac the only place it runs is a Linux virtual
machine, and the one this uses is whatever backs `docker`: a container of the pinned
`nixos/nix` image builds `tool/solo5/nix/pinned.nix -A solo5-run` (Solo5 at the pinned
commit, the images, the runner) and executes it. That is the real spt tender and the
real unikernel on a Linux kernel, not an emulation of either; the virtual machine under
the container is the Mac's and is the one thing here that is not the deployment target.

The container needs /dev/net/tun and CAP_NET_ADMIN (the node's network device is a tap
interface) and network access for the Nix binary cache. The Nix store is kept in the
Docker volume `sim-tool-solo5-nix` so that a second run downloads nothing; remove it
with `docker volume rm sim-tool-solo5-nix`.

On Linux with Nix the container is not needed:
    nix build .#solo5-run && sudo ./result/bin/sim-tool-solo5-run OUT
and `SOLO5_RECORDS=OUT python3 -m tool check solo5` then judges those records.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

from ..check import CheckFailure, require
from .scenarios import HVT, IMAGES

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "nixos/nix@sha256:746280551ddc1c72177d1a597c05eacac86dfaa18d5d649bf86411278bcd5470"
VOLUME = "sim-tool-solo5-nix"
COMMIT = "38e0c348be231a2a3c59510a5caabf562792679f"
SCRIPT = ("set -eu; nix-build /src/tool/solo5/nix/pinned.nix -A solo5-run -o /tmp/run >/dev/null; "
          "/tmp/run/bin/sim-tool-solo5-run /out")
TIMEOUT = 580


class Unavailable(CheckFailure):
    """Solo5 could not be built or run here. The message is the command and its error."""


def records(work):
    """Run the scenarios on Solo5 and return the directory holding the records."""
    given = os.environ.get("SOLO5_RECORDS")
    if given:
        return Path(given)
    name = os.environ.get("SOLO5_DOCKER", "docker")       # or podman, or a wrapper
    docker = shutil.which(name)
    if docker is None:
        raise Unavailable(f"solo5: blocked: no `{name}` on PATH to run a Linux container with; "
                          "run sim-tool-solo5-run on Linux and set SOLO5_RECORDS")
    out = work / "solo5"
    out.mkdir()
    command = [docker, "run", "--rm", "--label", "sim-tool-solo5=run",
               "--device", "/dev/net/tun", "--cap-add", "NET_ADMIN",
               "-v", f"{ROOT}:/src:ro", "-v", f"{VOLUME}:/nix", "-v", f"{out}:/out",
               IMAGE, "sh", "-c", SCRIPT]
    try:
        done = subprocess.run(command, text=True, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"solo5: blocked: no result within {TIMEOUT}s from\n  {' '.join(command)}") from None
    if done.returncode:
        raise Unavailable(f"solo5: blocked: exit status {done.returncode} from\n  {' '.join(command)}\n"
                          + (done.stdout + done.stderr)[-3000:])
    return out


def check(work, natives, digests, host_traces, report):
    from .check import judge
    out = records(work)
    require((out / "SOLO5_COMMIT").read_text().strip() == COMMIT, "solo5: not the pinned Solo5")
    built = json.loads((out / "node.c.sha256.json").read_text())
    require(built == digests, "solo5: the unikernels were compiled from a different node.c than "
                              "the simulator's library")
    abis = {}
    for line in (out / "images.txt").read_text().splitlines():
        name, abi, manifest = line.split(" ", 2)
        abis[name] = (json.loads(abi), json.loads(manifest))
    require(set(abis) == {f"{n}.spt" for n in IMAGES} | {f"{n}.hvt" for n in HVT},
            f"solo5: images built: {sorted(abis)}")
    for name, (abi, manifest) in abis.items():
        require(abi["target"] == name.rsplit(".", 1)[1], f"solo5: {name} is linked for {abi}")
    devices = [d["type"] for d in abis["accepted_job.spt"][1]["devices"] if d["name"] in ("disk", "net")]
    require(devices == ["BLOCK_BASIC", "NET_BASIC"], f"solo5: AcceptedJob declares {devices}")
    where = (out / "uname.txt").read_text().strip()
    tender = (out / "tender.txt").read_text().split("\n")[0]
    hashes = dict(reversed(line.split()) for line in (out / "sha256.txt").read_text().splitlines())
    require(set(hashes) == set(abis), "solo5: an image has no recorded hash")
    report(f"solo5: Solo5 {COMMIT[:12]} ({tender}) and {len(abis)} unikernels built by a Nix "
           f"derivation on {where}, as {(out / 'images-path.txt').read_text().strip()}; every "
           f"node.c is byte-identical to the simulator's; accepted_job.spt has SHA-256 "
           f"{hashes['accepted_job.spt']}")
    hvt = [line for line in (out / "hvt.txt").read_text().splitlines() if line.strip()]
    if hvt[-1] == "status 124" and any(line.startswith("@idle") for line in hvt):
        report("solo5: hvt: accepted_job.hvt booted under solo5-hvt and went idle; NO scenario "
               "was run on it")
    else:
        report(f"solo5: hvt: accepted_job.hvt is linked for hvt and was NOT run: solo5-hvt "
               f"said {hvt[0]!r} ({hvt[-1]})")
    spt = judge("solo5-spt", json.loads((out / "spt.json").read_text()), natives, report)
    linux = judge("host process on that Linux", json.loads((out / "host.json").read_text()),
                  natives, report)
    require(spt == host_traces == linux,
            "solo5: a clock-free scenario gives different simulator traces under different drivers: "
            + str(sorted(n for n in spt if not spt[n] == host_traces.get(n) == linux.get(n))))
    report(f"solo5: for the {len(spt)} scenarios of nodes without a clock, the unikernel under "
           f"solo5-spt, the host process here and the host process on that Linux reconstruct "
           f"to byte-identical simulator traces")
