"""Query oracle and SQLite correspondence checks."""
import random
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from query import oracle, fixture


def sql_group(rows):
    db = sqlite3.connect(":memory:")
    db.execute("create table t (a integer, b integer, c integer)")
    db.executemany("insert into t values (?,?,?)", rows)
    out = []
    for key, total in db.execute("select a, sum(c) from t group by a order by a"):
        out.extend((key, total))
    return out


def main():
    static, dynamic = fixture()
    assert oracle(static, dynamic) == [1, 2, 7, 8, 1, 6]
    for seed in range(100):
        rng = random.Random(seed)
        rows = [(rng.randrange(4), rng.randrange(8), rng.randrange(-10, 20))
                for _ in range(rng.randrange(0, 25))]
        flat = [0, len(rows), 0] + [v for row in rows for v in row]
        assert oracle((3, 3, [2, 0, 2]), flat) == sql_group(rows)
    print("query checks: oracle plus 100 SQLite datasets passed")


if __name__ == "__main__":
    main()
