# Relational queries

Static values encode row widths and one AST. `[0,column]` filters left rows,
`[1,leftKey,rightKey]` joins both tables, and `[2,key,value]` computes a
grouped sum. Dynamic vectors carry cutoff, row counts, and flattened rows.
`query.py` is the independent oracle. `check.py` compares grouped results with
SQLite over 100 seeded datasets, including empty inputs and duplicates.

The shared staged interface uses `spec.shen` and `spec.maude` with entry point
`specialize`. The static value is a query AST identifier and runtime values are
the encoded table rows. Build and replay with:

```sh
python3 -m specializer build query --source shen --static '[3]' -o /tmp/query
python3 -m examples.query.check
```

The backend removes the fixed query shape while retaining the runtime row
iteration. SQL parsing and null semantics are outside this bounded example.
