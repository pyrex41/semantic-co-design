"""Load the exported named modules in both real engines with no Python runtime inside them."""
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from .check import discover, maude_vector, require, run, shen_tree
from .library import export


def main():
    cases = list(discover())
    with TemporaryDirectory(prefix="book-libraries-") as directory:
        library = Path(directory) / "lib"
        export(cases, library)
        shen = library / "patterns.shen"
        maude = library / "patterns.maude"
        queries = []
        expected = []
        with shen.open("a") as stream:
            for case in cases:
                name = case["id"]
                args, base, changed = next(row for row in case["samples"] if row[1] != row[2])
                for suffix, answer in (("program", base), ("edit", changed)):
                    # Execute the exported residual with an empty static environment.
                    stream.write(f'(output "BOOK-RESULT ~S~%" (proj-run '
                                 f'(proj-pe ({name}-{suffix}) ({name}-config)) [] {shen_tree(args)}))\n')
                    queries.append(f"red in BOOK-PATTERNS : eval(specialize({name}-{suffix}, "
                                   f"{name}-config), nil, {maude_vector(args)}) .")
                    expected.append(answer)
        result = run([os.environ.get("SHEN_JIT", "shen-jit"), "script", str(shen)])
        actual = list(map(int, re.findall(r"^BOOK-RESULT (-?\d+)$", result, re.MULTILINE)))
        require(actual == expected, "exported Shen module returned unexpected results")
        result = run([os.environ.get("MAUDE", "maude"), "-no-banner", "-no-advise", "-batch", str(maude)],
                     request="\n".join([*queries, "quit", ""]))
        actual = list(map(int, re.findall(r"^result [^:\n]+: (-?\d+)$", result, re.MULTILINE)))
        require(actual == expected, "exported Maude module returned unexpected results")
    print(f"Both standalone language modules executed {len(expected)} named program variants each.")


if __name__ == "__main__":
    main()
