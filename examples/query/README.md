# Relational queries

The schema fixes `customers(id,name)` and `orders(id,customer_id,amount)`.
Rows, duplicate multiplicity, and the cutoff remain dynamic. `query.py`
implements the executable relational algebra used as the independent oracle.
It supports filter, inner join, and grouped sum. `check.py` compares grouped
results with SQLite over 100 seeded datasets, including empty inputs,
duplicates, unmatched keys, and cutoff ties.

The shared staged interface uses `spec.shen` and `spec.maude` with entry point
`specialize`. The static value is a query AST identifier and runtime values are
the encoded table rows. Build and replay with:

```sh
python3 -m specializer build query --source shen --static '[3]' -o /tmp/query
python3 -m examples.query.check
```

The backend removes the fixed query shape while retaining the runtime row
iteration. SQL parsing and null semantics are outside this bounded example.
