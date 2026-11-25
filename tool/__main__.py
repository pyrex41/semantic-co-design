"""python3 -m tool check <kernel | sim | example>   run the retained checks
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
            else:
                try:
                    module = importlib.import_module(f".examples.{name}.check", __package__)
                except ModuleNotFoundError as missing:
                    if missing.name != f"{__package__}.examples.{name}":
                        raise
                    module = importlib.import_module(f"targets.{name}.check")   # targets/<name>/
                module.main(record=argv[0] == "record")
        return 0
    if len(argv) == 3 and argv[0] == "compile":
        from .compile import build
        from .schema import Schema
        manifest = Path(__file__).parent / "examples" / argv[1] / "node.json"
        if not manifest.exists():
            manifest = Path(__file__).parent.parent / "targets" / argv[1] / "node.json"
        native = build(Schema(manifest), argv[2])
        print(native.library, native.executable, native.stats)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
