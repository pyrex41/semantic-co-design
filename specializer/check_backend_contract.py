"""Exercise the fifth-example boundary without registering another language construct."""
import json
from pathlib import Path
import subprocess
import tempfile

from specializer import build, concrete
from specializer.backend import normalize


def check():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        program = ["seq", ["set", 0, ["add", 1, 1]],
                   ["while", ["lt", ["var", 1], ["len"]],
                    ["seq", ["put", 0, ["var", 1], ["mul", ["at", ["var", 1]], ["var", 0]]],
                     ["emit", ["get", 0, ["var", 1]]], ["set", 1, ["add", ["var", 1], 1]]]]]
        (path / "manifest.json").write_text(json.dumps(dict(entry="specialize", static_type="unit",
                dynamic_type="integers", result_type="integers")))
        def term(x, language):
            if not isinstance(x, list):
                return str(x)
            body = " ".join(term(a, language) for a in x)
            return f"[{body}]" if language == "shen" else f"box({body})"
        (path / "spec.shen").write_text(f"(define specialize X -> {term(program, 'shen')})\n")
        (path / "spec.maude").write_text("fmod SPEC is protecting STAGE . op specialize : T -> T . "
                f"var X : T . eq specialize(X) = {term(program, 'maude')} . endfm\n")
        for source in ("shen", "maude"):
            rows = [[], [3, -7, 9], [4]]
            assert concrete("scale", source, [], rows, directory=path) == [[], [6,-14,18], [8]]
            binary = path / source
            assert build("scale", source, [], binary, directory=path) == normalize(program)
            result = subprocess.run([binary], input="\n3 -7 9\n4\n", capture_output=True,
                                    text=True, check=True, timeout=5)
            assert result.stdout == "\n6 -14 18\n8\n"
    print("Unregistered fifth example passed through both sources and native builds.")


if __name__ == "__main__":
    check()
