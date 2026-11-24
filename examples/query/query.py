"""Small relational algebra runtime used by the query specialization example.

The schema is fixed at build time.  Rows are ordinary dictionaries at runtime,
and duplicate rows are deliberately preserved throughout the operators.
"""
from collections import defaultdict


SCHEMA = {
    "customers": ("id", "name"),
    "orders": ("id", "customer_id", "amount"),
}


def execute(ast, tables, cutoff):
    """Evaluate a restricted relational AST over *tables*.

    Supported nodes are scan, filter, join, and group_sum.  The predicate
    vocabulary is intentionally small and represented as data so a compiler
    can stage it while leaving rows and cutoff dynamic.
    """
    kind = ast["op"]
    if kind == "scan":
        rows = [dict(row) for row in tables[ast["table"]]]
        return [row for row in rows if row.get(ast.get("cutoff_column", "id"), 0) <= cutoff]
    if kind == "filter":
        rows = execute(ast["input"], tables, cutoff)
        return [row for row in rows if _predicate(ast["predicate"], row)]
    if kind == "join":
        left = execute(ast["left"], tables, cutoff)
        right = execute(ast["right"], tables, cutoff)
        result = []
        for lrow in left:
            for rrow in right:
                if lrow.get(ast["left_key"]) == rrow.get(ast["right_key"]):
                    merged = dict(lrow)
                    for key, value in rrow.items():
                        if key in merged:
                            merged["right_" + key] = value
                        else:
                            merged[key] = value
                    result.append(merged)
        return result
    if kind == "group_sum":
        groups = defaultdict(int)
        for row in execute(ast["input"], tables, cutoff):
            groups[row[ast["key"]]] += int(row[ast["value"]])
        return [{ast["key"]: key, ast["value"]: groups[key]} for key in sorted(groups)]
    raise ValueError(f"unknown query operation: {kind}")


def _predicate(predicate, row):
    field = predicate["field"]
    value = row.get(field)
    op = predicate["operator"]
    target = predicate["value"]
    if op == "=":
        return value == target
    if op == "<":
        return value < target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    if op == ">=":
        return value >= target
    raise ValueError(f"unknown predicate operator: {op}")


def fixture():
    return {
        "customers": [
            {"id": 1, "name": "Ada"},
            {"id": 2, "name": "Lin"},
            {"id": 2, "name": "Lin duplicate"},
        ],
        "orders": [
            {"id": 10, "customer_id": 1, "amount": 7},
            {"id": 11, "customer_id": 1, "amount": 9},
            {"id": 12, "customer_id": 2, "amount": 4},
            {"id": 13, "customer_id": 3, "amount": 100},
        ],
    }


QUERIES = {
    "filter": {"op": "filter", "predicate": {"field": "amount", "operator": ">=", "value": 8},
               "input": {"op": "scan", "table": "orders", "cutoff_column": "id"}},
    "join": {"op": "join", "left_key": "id", "right_key": "customer_id",
             "left": {"op": "scan", "table": "customers"},
             "right": {"op": "scan", "table": "orders", "cutoff_column": "id"}},
    "group_sum": {"op": "group_sum", "key": "customer_id", "value": "amount",
                   "input": {"op": "scan", "table": "orders", "cutoff_column": "id"}},
}
