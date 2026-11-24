"""Correspondence checks shared by the example replay commands."""
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from . import build, concrete


def native(binary, rows):
    payload = "".join(" ".join(map(str, row)) + "\n" for row in rows)
    result = subprocess.run([binary.resolve()], input=payload, text=True, capture_output=True,
                            cwd=binary.parent, env={"PATH": ""},
                            check=True, timeout=45)
    return [list(map(int, line.split())) for line in result.stdout.splitlines()]


def verify(example, static, rows, expected, *, normalize=lambda row: row, mutations=None):
    """Check both original or edited specifications, then replay detached binaries."""
    from . import ROOT
    programs = []
    for source in ("shen", "maude"):
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            directory = ROOT.parent / "examples" / example
            if mutations:
                edited = work / "source"
                edited.mkdir()
                shutil.copy(directory / "manifest.json", edited)
                spec = f"spec.{source}"
                body = (directory / spec).read_text()
                old, new = mutations[source]
                assert body.count(old) == 1, (example, source, old)
                (edited / spec).write_text(body.replace(old, new))
                directory = edited
            actual = concrete(example, source, static, rows, directory=directory)
            binary = work / "native"
            programs.append(build(example, source, static, binary, directory=directory))
            for engine, answers in [(source, actual), (source + "-native", native(binary, rows))]:
                assert len(answers) == len(expected)
                for row, got, want in zip(rows, answers, expected):
                    assert normalize(got) == normalize(want), (example, engine, row, got, want)
    assert programs[0] == programs[1], (example, "residual programs differ")
    return programs[0]
