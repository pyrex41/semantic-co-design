"""Compare Capcov's atomic publication boundary with a split provider."""
import argparse
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

REVISION = "23f7841ca7369bf793c633e682fb00c77afc644c"


def atomic_histories(repo, fixture):
    from capcov.operations.system import explore

    path = f"tests/claim_semantics/fixtures/systems/{fixture}.ops"
    source = subprocess.check_output(["git", "-C", repo, "show", f"{REVISION}:{path}"],
                                     text=True)
    initial = [{"table": "job", "key": "j",
                "fields": {"status": "pending", "payload": "hello"}}]
    calls = [{"command": "send", "args": {"id": "j"}, "outcome": result}
             for result in ("unknown", "committed")]
    graph = explore(source, initial, calls, depth=2, schedule=[[call] for call in calls])
    paths = [(0, ())]
    for _ in calls:
        following = []
        for node, counts in paths:
            edges = [edge for edge in graph["edges"] if edge["from"] == node]
            if not edges or any(not edge["to"] for edge in edges):
                raise ValueError("truncated Capcov schedule")
            for edge in edges:
                for dest, effects in zip(edge["to"], edge["effects"], strict=True):
                    count = effects.count(["emit", "delivery-attempt", "hello"])
                    following.append((dest, (*counts, count)))
        paths = following
    return sorted(counts for _, counts in paths)


def split_publication(retry_at, retention, *, observe_attempts=False):
    # Two stores have independent commits. Expiration uses a logical clock.
    with TemporaryDirectory() as directory:
        worker = sqlite3.connect(Path(directory) / "worker.db")
        provider = sqlite3.connect(Path(directory) / "provider.db")
        try:
            worker.execute("CREATE TABLE job(id TEXT PRIMARY KEY, attempted INTEGER)")
            worker.execute("INSERT INTO job VALUES ('j', 0)")
            worker.commit()
            provider.execute("CREATE TABLE effects(job TEXT)")
            provider.execute("CREATE TABLE receipts(job TEXT PRIMARY KEY, expires INTEGER)")
            attempts = []
            for now in (0, retry_at):
                if worker.execute("SELECT attempted FROM job WHERE id='j'").fetchone()[0]:
                    raise AssertionError("retry must see pending job")
                attempts.append(("j", now))
                with provider:
                    seen = provider.execute(
                        "SELECT 1 FROM receipts WHERE job='j' AND expires > ?", (now,)
                    ).fetchone()
                    if not seen:
                        provider.execute("INSERT INTO effects VALUES ('j')")
                        if retention is not None:
                            provider.execute("INSERT OR REPLACE INTO receipts VALUES ('j', ?)",
                                             (now + retention,))
                if now == 0:  # Provider committed. Worker lost reply before local mark.
                    worker.close()
                    worker = sqlite3.connect(Path(directory) / "worker.db")
                else:
                    with worker:
                        worker.execute("UPDATE job SET attempted=1 WHERE id='j'")
            count = provider.execute("SELECT count(*) FROM effects WHERE job='j'").fetchone()[0]
            return (tuple(attempts), count) if observe_attempts else count
        finally:
            worker.close()
            provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capcov", type=Path)
    args = parser.parse_args()
    os.environ.setdefault("CAPCOV_SHEN_IMPL", "shen-go")
    os.environ.setdefault("BIFROST_SHEN_GO", shutil.which("shen-go") or "")
    for fixture, expected in (("outbox", [(0, 1), (1, 1)]),
                              ("outbox-intended", [(0, 1), (1, 0)])):
        observed = atomic_histories(args.capcov, fixture)
        if observed != expected:
            raise AssertionError((fixture, observed, expected))
        print(f"{fixture}: model emissions on first send and retry = {observed}")
    for retention, retry_at, expected in ((None, 1, 2), (10, 9, 1), (10, 10, 2)):
        count = split_publication(retry_at, retention)
        if count != expected:
            raise AssertionError((retention, retry_at, count, expected))
        print(f"split provider: retention={retention} retry={retry_at} committed effects={count}")


if __name__ == "__main__":
    main()
