"""Validate, partially evaluate and lower a structured integer program to C11."""
import operator
from pathlib import Path

ARITHMETIC = {
    "add": ("+", operator.add), "sub": ("-", operator.sub), "mul": ("*", operator.mul),
    "eq": ("==", operator.eq), "lt": ("<", operator.lt), "le": ("<=", operator.le),
    "and": ("&&", lambda a,b: bool(a) and bool(b)),
    "or": ("||", lambda a,b: bool(a) or bool(b)),
}


LIMITS = {"slots": 1024, "depth": 128}


def integer(n):
    if type(n) is not int or not -(1 << 63) <= n < 1 << 63:
        raise ValueError(f"expected signed 64-bit integer: {n!r}")
    return n


def slot(n):
    if type(n) is not int or not 0 <= n < LIMITS["slots"]:
        raise ValueError("invalid scalar or bank number")
    return n


def node(tree, arities, depth):
    if depth > LIMITS["depth"] or not isinstance(tree,list) or not tree or not isinstance(tree[0],str):
        raise ValueError("invalid or deeply nested residual node")
    name = tree[0]
    if name not in arities or arities[name] not in (None,len(tree)-1):
        raise ValueError(f"invalid residual operation: {name}")
    return name


EXPRESSIONS = {**dict.fromkeys(ARITHMETIC,2), "div":2, "mod":2, "var":1, "len":0,
               "at":1, "get":2, "u32":1}
STATEMENTS = {"seq":None, "set":2, "put":3, "emit":1, "if":3, "while":2, "trap":0}


def expression(tree, depth):
    if type(tree) is int:
        return integer(tree)
    op = node(tree, EXPRESSIONS, depth)
    if op == "var":
        return [op, slot(tree[1])]
    if op == "get":
        return [op,slot(tree[1]),expression(tree[2],depth+1)]
    args = [expression(value,depth+1) for value in tree[1:]]
    if op not in ("len","at") and all(type(v) is int for v in args):
        if op == "u32":
            return args[0] % 2**32
        a,b = args
        if op in ("div","mod"):
            if b == 0:
                raise ValueError("division by zero")
            q = abs(a) // abs(b) * (-1 if (a < 0) != (b < 0) else 1)
            return integer(q if op == "div" else a-q*b)
        return integer(int(ARITHMETIC[op][1](a,b)))
    return [op,*args]


def normalize(tree, depth=0):
    op = node(tree,STATEMENTS,depth)
    def expr(child):
        return expression(child,depth+1)
    if op == "seq":
        steps = []
        for child in tree[1:]:
            step = normalize(child,depth+1)
            steps.extend(step[1:] if step[0] == "seq" else [step])
        return ["seq",*steps]
    if op == "trap":
        return [op]
    if op == "emit":
        return [op,expr(tree[1])]
    if op in ("set","put"):
        return [op,slot(tree[1]),*(expr(v) for v in tree[2:])]
    condition = expr(tree[1])
    branches = [normalize(child,depth+1) for child in tree[2:]]
    if type(condition) is int:
        if op == "if":
            return branches[0 if condition else 1]
        if not condition:
            return ["seq"]
    return [op,condition,*branches]


def lower_c_body(program, *, trap="exit(2);", slots=1024, depth=128):
    """Return C statements and the scratch bank IDs required by the program."""
    saved = dict(LIMITS)
    LIMITS.update(slots=slots, depth=depth)
    try:
        program = normalize(program)
    finally:
        LIMITS.update(saved)
    banks = set()
    def c_expr(tree):
        if type(tree) is int:
            return "INT64_MIN" if tree == -(2**63) else f"INT64_C({tree})"
        op,*args = tree
        if op == "len":
            return "count"
        if op == "var":
            return f"slots[{args[0]}]"
        if op == "at":
            return f"read_at(input, count, {c_expr(args[0])})"
        if op == "get":
            banks.add(args[0])
            return f"read_at(bank_{args[0]}, LIMIT, {c_expr(args[1])})"
        if op == "u32":
            return f"((int64_t)(uint32_t){c_expr(args[0])})"
        sign = ARITHMETIC[op][0] if op in ARITHMETIC else {"div":"/","mod":"%"}[op]
        return f"({c_expr(args[0])} {sign} {c_expr(args[1])})"

    lines = []
    def statement(tree,depth):
        op,*args = tree
        def line(code):
            lines.append("    "*depth + code)
        if op == "seq":
            for child in args:
                statement(child,depth)
        elif op == "trap":
            line(trap)
        elif op == "emit":
            line(f"emit({c_expr(args[0])});")
        elif op == "set":
            line(f"slots[{args[0]}] = {c_expr(args[1])};")
        elif op == "put":
            banks.add(args[0])
            line(f"write_at(bank_{args[0]}, {c_expr(args[1])}, {c_expr(args[2])});")
        else:
            cond = c_expr(args[0])
            line(f"{op} ({cond} != 0) {{")
            statement(args[1],depth+1)
            if op == "if" and args[2] != ["seq"]:
                line("} else {")
                statement(args[2],depth+1)
            line("}")
    statement(program,1)
    return lines, sorted(banks)


def generate(program, *, template=None, slots=1024, depth=128):
    """Lower to C. A caller may supply its own driver template, slot count and nesting bound."""
    lines, banks = lower_c_body(program, trap="exit(2);" if template is None else "TRAP();",
                                slots=slots, depth=depth)
    declarations = [f"static int64_t bank_{b}[LIMIT];" for b in banks]
    resets = [f"    memset(bank_{b}, 0, sizeof bank_{b});" for b in banks]
    if template is None:
        template = Path(__file__).with_name("driver.c.in").read_text()
    return template.replace("/* SLOTS */", str(slots)).replace("/* BANKS */","\n".join(declarations)).replace(
        "/* PROGRAM */","\n".join(resets+lines))
