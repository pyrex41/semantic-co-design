import argparse
import json
from pathlib import Path

from . import build

parser = argparse.ArgumentParser(description="Specialize an executable specification")
commands = parser.add_subparsers(dest="command", required=True)
compile_cmd = commands.add_parser("build")
compile_cmd.add_argument("example")
compile_cmd.add_argument("--source", choices=("shen", "maude"), required=True)
compile_cmd.add_argument("--static", type=Path, required=True)
compile_cmd.add_argument("-o", type=Path, required=True)
compile_cmd.add_argument("--emit-ir", type=Path)
compile_cmd.add_argument("--emit-c", type=Path)
args = parser.parse_args()
build(args.example, args.source, json.loads(args.static.read_text()), args.o,
      emit_ir=args.emit_ir, emit_c=args.emit_c)
