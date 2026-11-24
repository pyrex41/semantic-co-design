import json
from pathlib import Path
import random
import sqlite3

from specializer.replay import verify

ROOT = Path(__file__).parent


def datasets():
    yield 3, [], []
    yield 3, [(1,3), (1,3), (7,-2)], [(1,2), (1,2), (9,7)]
    yield 2**62 + 1, [(8,2**62), (8,1), (2,-2**63), (3,2**63-1)], [(8,-2**63)]
    for seed in range(100):
        rng = random.Random(seed)
        def table():
            return [(rng.randrange(4), rng.randrange(-4,5)) for _ in range(rng.randrange(9))]
        yield rng.randrange(-4,5), table(), table()


def sql(statement, data):
    cutoff, left, right = data
    db = sqlite3.connect(":memory:")
    try:
        for name, rows in (("l",left), ("r",right)):
            db.execute(f"create table {name}(k integer,v integer)")
            db.executemany(f"insert into {name} values(?,?)", rows)
        return [x for row in db.execute(statement, (cutoff,) if "?" in statement else ()) for x in row]
    finally:
        db.close()


def ordered(row,width):
    assert len(row) % width == 0
    return sorted(tuple(row[i:i+width]) for i in range(0,len(row),width))


def main():
    data = list(datasets())
    rows = [[cutoff,len(l),len(r),*(v for pair in l+r for v in pair)] for cutoff,l,r in data]
    for name, width, statement in [
        ("filter",2,"select k,v from l where v >= ?"),
        ("join",4,"select l.*,r.* from l join r on l.k=r.k"),
        ("group",2,"select k,sum(v) from l group by k")]:
        fixed = json.loads((ROOT / f"{name}.json").read_text())
        verify("query", fixed, rows, [sql(statement,d) for d in data],
               normalize=lambda row: ordered(row,width))
    verify("query", [2,2,[3,0,1,[1,1,[0,0]]]], rows,
           [sql("select k,sum(v) from l where v >= ? group by k",d) for d in data],
           normalize=lambda row: ordered(row,2))
    verify("query",[2,2,[1,1,[0,0]]], [[3,1,0,7,3]], [[]], mutations={
        "shen": ("[le [at 0]", "[lt [at 0]"),
        "maude": ("box(le box(at 0)", "box(lt box(at 0)")})
    print(f"Queries: 3 operators × {len(data)} SQLite datasets and strict-cutoff mutant passed.")


if __name__ == "__main__":
    main()
