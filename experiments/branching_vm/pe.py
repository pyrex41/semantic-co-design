"""Front for the general partial evaluator, which now lives in tool/pe.py.

This module keeps the command line and the `derive`/`Unsupported` names that the
branching VM check was written against. There is one evaluator, not two.
"""
from pathlib import Path

from tool.pe import Unsupported, derive  # noqa: F401

if __name__ == "__main__":
    import argparse
    import json
    from specializer.backend import generate, normalize

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("entry")
    parser.add_argument("arguments", help='JSON array; {"input": n} marks a runtime integer')
    parser.add_argument("--emit", choices=("ir", "c"), default="ir")
    args = parser.parse_args()
    tree = normalize(derive(args.source, args.entry, json.loads(args.arguments))[0])
    print(generate(tree) if args.emit == "c" else json.dumps(tree, indent=2))
