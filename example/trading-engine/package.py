"""Build or interpret the trading application using the shared application compiler."""

import argparse
from pathlib import Path
import subprocess
import sys
import sysconfig


HERE = Path(__file__).resolve().parent
COMPILER = HERE.parent / "webapp"
sys.path.insert(0, str(COMPILER))

import appc
from event_host import release_id
from values import canonical


MANIFEST = HERE / "manifest.json"


def revision(path, manifest):
    compiler = Path(appc.__file__).resolve()
    return release_id({"digests": {
        "manifest": appc.digest(path), "compiler": appc.digest(compiler),
        "drivers": {"values.py": appc.digest(compiler.parent / "values.py")},
        "compiler_dependencies": appc.digest(compiler.parent / "requirements-build.txt"),
        "source": {name: appc.digest(path.parent / name) for name in manifest["sources"]},
        "assets": {name: appc.digest(path.parent / name) for name in manifest["assets"]},
    }, "abi": {"python": sysconfig.get_config_var("SOABI")}})


def build(path, out, sign_key=None):
    import json

    appc.build(path, out)
    record = out / "ownership.json"
    ownership = json.loads(record.read_text())
    ownership["release"] = release_id(ownership)
    ownership["artifacts"] = {
        str(file.relative_to(out)): appc.digest(file)
        for file in out.rglob("*") if file.is_file() and file != record
    }
    record.write_text(canonical(ownership) + "\n")
    if sign_key:
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(sign_key), "-out",
                        str(out / "ownership.sig"), str(record)], check=True, timeout=20)


def verify(out, key):
    import json

    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key), "-signature",
                    str(out / "ownership.sig"), str(out / "ownership.json")], check=True, timeout=20)
    ownership = json.loads((out / "ownership.json").read_text())
    for name, wanted in ownership["artifacts"].items():
        file = (out / name).resolve()
        if out.resolve() not in file.parents or appc.digest(file) != wanted:
            raise ValueError(f"artifact digest does not match {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    compile_cmd = commands.add_parser("build")
    compile_cmd.add_argument("--manifest", type=Path, default=MANIFEST)
    compile_cmd.add_argument("--out", type=Path, required=True)
    compile_cmd.add_argument("--sign-key", type=Path)
    inspect_cmd = commands.add_parser("inspect")
    inspect_cmd.add_argument("release", type=Path)
    verify_cmd = commands.add_parser("verify")
    verify_cmd.add_argument("release", type=Path)
    verify_cmd.add_argument("--public-key", type=Path, required=True)
    commands.add_parser("interpret")
    args, command = parser.parse_known_args()
    if args.mode == "build":
        if command:
            parser.error("unexpected arguments")
        build(args.manifest, args.out, args.sign_key)
    elif args.mode == "inspect":
        if command:
            parser.error("unexpected arguments")
        print((args.release / "ownership.json").read_text(), end="")
    elif args.mode == "verify":
        if command:
            parser.error("unexpected arguments")
        verify(args.release, args.public_key)
    else:
        from event_host import main as invoke
        release, evaluator = appc.load(MANIFEST)
        invoke(release, appc.reference_entries(release, evaluator), revision(MANIFEST, release), command)


if __name__ == "__main__":
    main()
