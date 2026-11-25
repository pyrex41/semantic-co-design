"""Export the book programs as named, standalone Shen and Maude libraries."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from .check import HERE, catalog, expression, maude_tree, maude_vector, shen_tree


def export(cases, destination):
    """Generate both language modules with their interpreters and specializers."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"choose a fresh destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shen = [(HERE / "core.shen").read_text()]
    maude = [(HERE / "core.maude").read_text(), "fmod BOOK-PATTERNS is", "  protecting PROJECTION ."]
    for case in cases:
        name = case["id"]
        shen.append(f'(define {name}-config\n  -> {shen_tree(case["config"])})')
        maude.extend([f"  op {name}-config : -> Ints .",
                      f'  eq {name}-config = {maude_vector(case["config"])} .'])
        for field, suffix in (("program", "program"), ("changed", "edit")):
            entry = f"{name}-{suffix}"
            tree = expression(case[field])
            shen.append(f'(define {entry}\n  -> {shen_tree(tree)})')
            maude.extend([f"  op {entry} : -> Exp .",
                          f"  eq {entry} = {maude_tree(tree)} ."])
    maude.append("endfm")
    with TemporaryDirectory(dir=destination.parent) as temp:
        module = Path(temp) / "library"
        module.mkdir()
        (module / "patterns.shen").write_text("\n\n".join(shen) + "\n")
        (module / "patterns.maude").write_text("\n".join(maude) + "\n")
        module.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--case", action="append", help="export only this case (repeatable)")
    args = parser.parse_args()
    cases = catalog()
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error("unknown IDs: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.case]
    export(cases, args.out)
    print(f"Exported {len(cases)} programs and source edits to {args.out}")


if __name__ == "__main__":
    main()
