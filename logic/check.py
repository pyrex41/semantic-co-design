"""Check every native transition against the source runtimes and a source edit."""
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from build import build

HERE = Path(__file__).resolve().parent


def check(root):
    for policy in ("withdraw", "keep"):
        graphs = []
        for lang in ("shen", "maude"):
            binary = root / lang
            graph = build(HERE / f"accepted-job.{lang}", policy, binary)
            graphs.append(graph)
            database = root / "jobs"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE IF NOT EXISTS instances(id INTEGER PRIMARY KEY,state TEXT NOT NULL)")
                for (state, event), (target, output) in graph.items():
                    db.execute("INSERT OR REPLACE INTO instances VALUES(1,?)", (state,))
                    db.commit()
                    observed = subprocess.check_output([binary, database, "1", event], text=True).strip()
                    stored, = db.execute("SELECT state FROM instances WHERE id=1").fetchone()
                    if (observed, stored) != (f"{target} {output}", target):
                        raise AssertionError((lang, policy, state, event, observed, stored))
        if graphs[0] != graphs[1]:
            raise AssertionError("source transition functions disagree")
        expected = "withdrawn-a" if policy == "withdraw" else "pending-a"
        if graphs[0]["pending-a", "cancel"] != (expected, "cancelled"):
            raise AssertionError("incorrect cancellation")
    for lang, old, new in (("shen", "[[Status D] conflict]", "[[Status D] [receipt D]]"),
                            ("maude", "result(pending(D), conflict)", "result(pending(D), receipt(D))")):
        source = (HERE / f"accepted-job.{lang}").read_text()
        if source.count(old) != 1:
            raise ValueError("mutation site changed")
        modified = root / f"modified.{lang}"
        modified.write_text(source.replace(old, new))
        exe = root / f"edited-{lang}"
        build(modified, "withdraw", exe)
        modified.unlink()
        actual = subprocess.check_output([exe, root / f"{lang}.db", "2", "submit-a", "submit-b"],
                                         text=True, env={"PATH": ""})
        if actual.splitlines() != ["pending-a receipt-a"] * 2:
            raise AssertionError(actual)
    print("112 native transitions checked; both source edits changed native conflict behavior")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        check(Path(directory))
