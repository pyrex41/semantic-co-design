"""Direct relational evaluator used for the benchmark baseline."""
def oracle(static, dynamic):
    left_width, right_width, ast = static
    cutoff, nleft, nright = dynamic[:3]
    boundary = 3 + left_width*nleft
    assert len(dynamic) == boundary + right_width*nright
    tables = [
        [dynamic[i:i+left_width] for i in range(3,boundary,left_width)],
        [dynamic[i:i+right_width] for i in range(boundary,len(dynamic),right_width)],
    ]

    def evaluate(node):
        operation,*args = node
        if operation == 0:
            return tables[args[0]]
        if operation == 1:
            column,child = args
            return [row for row in evaluate(child) if row[column] >= cutoff]
        if operation == 2:
            k,j,left,right = args
            right_rows = evaluate(right)
            return [a+b for a in evaluate(left) for b in right_rows if a[k] == b[j]]
        if operation == 3:
            key,value,child = args
            groups = {}
            for row in evaluate(child):
                groups[row[key]] = groups.get(row[key],0) + row[value]
            return list(groups.items())
        raise ValueError(operation)
    return [v for row in evaluate(ast) for v in row]
