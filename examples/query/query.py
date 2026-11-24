"""Independent oracle for staged relational query vectors."""
from collections import defaultdict


def unpack(static, dynamic):
    lw, rw, ast = static
    cutoff, nl, nr = dynamic[:3]
    pos = 3
    left = [dynamic[pos+i*lw:pos+(i+1)*lw] for i in range(nl)]
    pos += nl * lw
    right = [dynamic[pos+i*rw:pos+(i+1)*rw] for i in range(nr)]
    return lw, rw, ast, cutoff, left, right


def oracle(static, dynamic):
    lw, rw, ast, cutoff, left, right = unpack(static, dynamic)
    if ast[0] == 0:
        return [v for row in left if row[ast[1]] >= cutoff for v in row]
    if ast[0] == 1:
        return [v for lrow in left for rrow in right if lrow[ast[1]] == rrow[ast[2]] for v in (*lrow, *rrow)]
    if ast[0] == 2:
        sums = defaultdict(int)
        for row in left: sums[row[ast[1]]] += row[ast[2]]
        return [v for k in sorted(sums) for v in (k, sums[k])]
    raise ValueError(ast)


def fixture(static=(3, 3, [0, 2])):
    return static, [5, 3, 3, 1, 2, 7, 9, 2, 4, 8, 1, 6, 4, 2, 3, 10, 2, 5]
