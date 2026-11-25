"""python3 -m tool.host.pack <example> <directory> [nodes] [mutant]

Compile an example to what a machine needs to run it and what conform.py needs
to judge it, and nothing else:

    node.wasm  node.json  inits.json  <the specification>  <the invariants>

inits.json is the state integers of (<init> Node Nodes) for each node. With a
mutant named, the specification written is the example's with that one edit
(tool/examples/<example>/check.py, MUTANTS), and node.wasm is compiled from it.
"""
import importlib
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

from ..schema import Schema
from . import bundle


def pack(example, directory, nodes=1, mutant=None):
    directory = Path(directory)
    schema = Schema(Path(__file__).parents[1] / "examples" / example / "node.json")
    source = schema.source
    if mutant:
        check = importlib.import_module(f"..examples.{example}.check", __package__)
        source = check.mutate(source, check.MUTANTS[mutant])
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as work:
        packed = bundle(schema, work, nodes, source)
        shutil.copy(packed["wasm"], directory / "node.wasm")
    (directory / "node.json").write_text(json.dumps(schema.manifest, indent=2) + "\n")
    (directory / "inits.json").write_text(json.dumps(packed["inits"]) + "\n")
    (directory / schema.manifest["spec"]).write_text(source)
    if schema.invariants:
        shutil.copy(schema.directory / schema.invariants["file"], directory)
    return directory


if __name__ == "__main__":
    example, directory, *rest = sys.argv[1:]
    pack(example, directory, int(rest[0]) if rest else 1, rest[1] if rest[1:] else None)
