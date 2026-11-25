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

A directory of records is taken on trust. The check requires it to be consistent with
itself and with this checkout (the hashes of the harness sources, of every driver.c and
node.c, of each image a record says it executed, of the tender; the Solo5 banner and the
argument vector of every boot), which catches records of another build, of another
driver or of the host process passed off as the unikernel. Nothing in it is signed:
whoever can write the directory can make it say anything. With SOLO5_RECORDS the check
says in its last line that Solo5 was not run by it.
"""
import hashlib
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


SOURCES = ("run.py", "scenarios.py", "mutants.py", "emit.py", "driver.c", "host.c")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def records(work):
    """Run the scenarios on Solo5. Returns the directory holding the records, and whether
    it was supplied instead of made here."""
    given = os.environ.get("SOLO5_RECORDS")
    if given:
        return Path(given), True
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
    return out, False


def check(work, natives, digests, host_traces, report):
    """Judge the Solo5 records. Returns whether they were supplied instead of made here."""
    from .check import judge, refuses
    out, supplied = records(work)
    require((out / "SOLO5_COMMIT").read_text().strip() == COMMIT, "solo5: not the pinned Solo5")
    built = json.loads((out / "node.c.sha256.json").read_text())
    require(built == digests, "solo5: the unikernels were compiled from a different node.c than "
                              "the simulator's library")
    drivers = json.loads((out / "driver.c.sha256.json").read_text())
    require(drivers == {name: sha(work / "src" / name / "driver.c") for name in IMAGES},
            "solo5: the unikernels were compiled from a different driver.c than this checkout's")
    ran = dict(reversed(line.split()) for line in (out / "sources.sha256").read_text().splitlines())
    here = {f"tool/solo5/{name}": sha(ROOT / "tool" / "solo5" / name) for name in SOURCES}
    require(ran == here, "solo5: the records were made by other harness sources than this "
                         f"checkout's: {sorted(n for n in here if ran.get(n) != here[n])}")
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
    *version, binary = (out / "tender.txt").read_text().strip().split("\n")
    tender, path = binary.split()
    require(version and version[0].startswith("solo5-spt tender, version v0.9.3")
            and Path(path).name == "solo5-spt", f"solo5: the tender is {version} {path}")
    hashes = dict(reversed(line.split()) for line in (out / "sha256.txt").read_text().splitlines())
    require(set(hashes) == set(abis) | {f"{n}.host" for n in IMAGES},
            "solo5: an image has no recorded hash")
    report(f"solo5: Solo5 {COMMIT[:12]} ({version[0]}) and {len(abis)} unikernels built by a Nix "
           f"derivation on {where}, as {(out / 'images-path.txt').read_text().strip()}; every "
           f"node.c and driver.c is byte-identical to this checkout's; accepted_job.spt has "
           f"SHA-256 {hashes['accepted_job.spt']}; the tender that ran them, {path}, has "
           f"SHA-256 {tender} (its build is input-addressed, not bit-reproducible)")
    hvt = [line for line in (out / "hvt.txt").read_text().splitlines() if line.strip()]
    if hvt[-1] == "status 124" and any(line.startswith("@idle") for line in hvt):
        report("solo5: hvt: accepted_job.hvt booted under solo5-hvt and went idle; NO scenario "
               "was run on it")
    else:
        report(f"solo5: hvt: accepted_job.hvt is linked for hvt and was NOT run: solo5-hvt "
               f"said {hvt[0]!r} ({hvt[-1]})")
    proof = {"kind": "spt", "images": hashes, "tender": tender}
    recorded = json.loads((out / "spt.json").read_text())
    spt = judge("solo5-spt", recorded, natives, report, proof)
    linux = judge("host process on that Linux", json.loads((out / "host.json").read_text()),
                  natives, report, {"kind": "host", "images": hashes, "tender": None})
    # The records of the host process are not accepted in place of the unikernel's:
    # as they are, relabelled, and with the image and tender entries of the real records.
    fake = json.loads((out / "host.json").read_text())
    refuses("solo5-spt", fake, proof, "the host process's runs")
    for name, record in fake.items():
        record["driver"] = "spt"
    refuses("solo5-spt", fake, proof, "the host process's runs, relabelled")
    for name, record in fake.items():
        record.update(image=recorded[name]["image"], tender=recorded[name]["tender"])
    refuses("solo5-spt", fake, proof, "the host process's runs under the unikernel's hashes")
    report("solo5: the host process's records are not accepted as records of solo5-spt: not as "
           "they are, not relabelled, and not with the image and tender hashes of the real ones "
           "(no boot in them is a solo5-spt command that printed the Solo5 banner)")
    require(spt == host_traces == linux,
            "solo5: a clock-free scenario gives different simulator traces under different drivers: "
            + str(sorted(n for n in spt if not spt[n] == host_traces.get(n) == linux.get(n))))
    report(f"solo5: for the {len(spt)} scenarios of nodes without a clock, the unikernel under "
           f"solo5-spt, the host process here and the host process on that Linux reconstruct "
           f"to byte-identical simulator traces")
    return supplied
