"""Check retained residuals against both executable sources and their dynamic loops."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from specializer import build, concrete
from specializer.replay import native

EXAMPLES = Path(__file__).resolve().parent
CASES = (("vm","sum",1), ("decoder","grammar",3), ("railway","layout",1), ("query","join",2))


def loops(tree):
    if not isinstance(tree,list):
        return 0
    return (tree[0] == "while") + sum(loops(child) for child in tree[1:])


def check(write=False):
    with TemporaryDirectory() as tmp:
        binary = Path(tmp) / "native"
        for name,fixture,expected_loops in CASES:
            directory = EXAMPLES / name
            fixed = json.loads((directory / f"{fixture}.json").read_text())
            case = json.loads((directory / "runtime.json").read_text())
            for source in ("shen","maude"):
                assert concrete(name,source,fixed,[case["input"]]) == [case["output"]]
                ir, c = Path(tmp) / "program.json", Path(tmp) / "program.c"
                program = build(name,source,fixed,binary,emit_ir=ir,emit_c=c)
                assert native(binary,[case["input"]]) == [case["output"]]
                assert loops(program) == expected_loops
                for artifact in (ir,c):
                    retained = directory / "residual" / artifact.name
                    if write and source == "shen":
                        retained.parent.mkdir(exist_ok=True)
                        retained.write_bytes(artifact.read_bytes())
                    assert retained.read_bytes() == artifact.read_bytes(), retained
    print("Both sources reproduce the four retained IR/C pairs and expected dynamic loop counts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write",action="store_true")
    check(parser.parse_args().write)
