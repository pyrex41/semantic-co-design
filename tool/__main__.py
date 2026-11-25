"""python3 -m tool check <kernel | sim | example | target>   run the retained checks
python3 -m tool record <example>             regenerate an example's retained evidence
python3 -m tool compile <example> <dir>      write IR, C, shared library and executable"""
import importlib
import sys
from pathlib import Path


def main(argv):
    if len(argv) >= 2 and argv[0] in ("check", "record"):
        for name in argv[1:]:
            if name == "kernel":
                from .kernel_check import main as run
                run()
            elif name == "sim":
                from .sim_check import main as run
                run()
            elif (Path(__file__).parent / "examples" / name).is_dir():
                module = importlib.import_module(f".examples.{name}.check", __package__)
                module.main(record=argv[0] == "record")
            else:       # a target outside the kernel: targets/<name>/check.py
                module = importlib.import_module(f"targets.{name}.check")
                module.main(record=argv[0] == "record")
        return 0
    if len(argv) == 3 and argv[0] == "compile":
        from .compile import build
        from .schema import Schema
        native = build(Schema(Path(__file__).parent / "examples" / argv[1] / "node.json"), argv[2])
        print(native.library, native.executable, native.stats)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
