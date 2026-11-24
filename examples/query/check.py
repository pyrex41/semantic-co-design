"""Independent correspondence check, including 100 seeded SQLite datasets."""
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from query import QUERIES, execute, fixture


def sqlite_group(rows, cutoff):
    db = sqlite3.connect(":memory:")
    db.executescript("create table orders(id integer, customer_id integer, amount integer);")
    db.executemany("insert into orders values(?,?,?)", [tuple(r.values()) for r in rows])
    return [{"customer_id": k, "amount": v} for k, v in db.execute(
        "select customer_id, sum(amount) from orders where id <= ? group by customer_id order by customer_id", (cutoff,))]


def main():
    assert execute(QUERIES["filter"], fixture(), 12) == [{"id": 11, "customer_id": 1, "amount": 9}]
    assert execute(QUERIES["group_sum"], fixture(), 12) == [{"customer_id": 1, "amount": 16}, {"customer_id": 2, "amount": 4}]
    for seed in range(100):
        rng = random.Random(seed)
        rows = [{"id": i, "customer_id": rng.randrange(4), "amount": rng.randrange(-5, 20)} for i in range(rng.randrange(0, 20))]
        cutoff = rng.randrange(0, 20)
        tables = {"customers": [], "orders": rows}
        assert execute(QUERIES["group_sum"], tables, cutoff) == sqlite_group(rows, cutoff)
    print("query checks: 100 datasets passed")


if __name__ == "__main__":
    main()
