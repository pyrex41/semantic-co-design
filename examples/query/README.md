# Relational queries

The fixed input is `[leftRowWidth, rightRowWidth, query]`. Column numbers are zero-based. A scan is `[0, table]` with table 0 for left and 1 for right. A filter `[1, column, child]` keeps rows whose field meets the dynamic cutoff. Scans and nested filters can feed an inner join `[2, leftKey, rightKey, leftChild, rightChild]` or a grouped sum `[3, key, value, child]`.

The runtime row contains `cutoff, leftCount, rightCount`, then the left and right rows in row-major order. Use non-null signed integers, positive row widths, valid columns, and sums in signed 64-bit range. The join emits both matching rows in full. Grouping emits key/sum pairs in first-seen order. The SQLite comparison sorts result tuples and preserves multiplicity. The grouped implementation scans the child once per candidate group, so its worst-case runtime is quadratic.

Run `python3 -m examples.query.check` in the Nix shell. It checks the three fixture queries and a grouped sum over a filter on 100 seeded datasets plus fixed boundary cases, including signed 64-bit extrema. A source-only mutation changes the inclusive predicate to a strict predicate and removes a row equal to the cutoff.
