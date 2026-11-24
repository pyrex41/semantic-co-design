"""Online partial evaluator for first-order Shen with dynamic conditionals.

Calls are unfolded until a conditional tests a runtime value. Each call in a
branch of that conditional becomes a specialization point: its arguments are
generalized, the resulting (function, static skeleton) pair names a residual
block, and blocks are produced until no new pair appears. No clause shape,
function name or loop form of the interpreted source is known here.
"""
from dataclasses import dataclass
from pathlib import Path
import re

from experiments.ordinary_vm.derive import Call, Cons, parse, sequence

STATE, RESULT, TEMP, PARAMS = 1023, 1022, 512, 500
EXACT = 1 << 53


class Unsupported(ValueError):
    """The source or the requested division is outside the implemented fragment."""


@dataclass(frozen=True)
class Expr:
    ir: object


class Test(Expr):
    """A residual comparison; a boolean, so usable only as a condition."""


@dataclass(frozen=True)
class Param:
    index: int


@dataclass(frozen=True)
class If:
    cond: object
    then: object
    other: object


@dataclass(frozen=True)
class Jump:
    block: int
    actuals: tuple


@dataclass(frozen=True)
class Return:
    value: object


def scalar(value):
    if type(value) is Expr:
        return value.ir
    if type(value) is int:
        return value
    raise Unsupported(f"expected a number where the program supplies {value!r}")


def has_dynamic(value):
    if isinstance(value, Cons):
        return has_dynamic(value.head) or has_dynamic(value.tail)
    return isinstance(value, Expr)


def generalize(values):
    """Keep fully static arguments. Lift every number of an argument that holds runtime data."""
    actuals = []

    def lift(value):
        if isinstance(value, Cons):
            return Cons(lift(value.head), lift(value.tail))
        if isinstance(value, Expr) or type(value) is int:
            actuals.append(scalar(value))
            return Param(len(actuals) - 1)
        return value

    skeleton = tuple(lift(v) if has_dynamic(v) else v for v in values)
    if len(actuals) > PARAMS:
        raise Unsupported("too many runtime values live at one program point")
    return skeleton, tuple(actuals)


def instantiate(value):
    if isinstance(value, Cons):
        return Cons(instantiate(value.head), instantiate(value.tail))
    return Expr(["var", value.index]) if isinstance(value, Param) else value


def match(pattern, value, env):
    if pattern == "_":
        return True
    if isinstance(pattern, str) and pattern[0].isupper():
        if pattern in env:
            raise Unsupported("repeated-variable patterns")
        env[pattern] = value
        return True
    if isinstance(value, Expr):
        raise Unsupported("a clause pattern inspects a runtime value")
    if isinstance(pattern, Cons):
        return (isinstance(value, Cons) and match(pattern.head, value.head, env)
                and match(pattern.tail, value.tail, env))
    return type(pattern) is type(value) and pattern == value


def exact(n):
    if not -EXACT < n < EXACT:
        raise Unsupported("static arithmetic leaves the exact range of Shen numbers")
    return n


def arithmetic(name, a, b):
    static = type(a) is int and type(b) is int
    if name in ("+", "-", "*"):
        if static:
            return exact({"+": a + b, "-": a - b, "*": a * b}[name])
        return Expr([{"+": "add", "-": "sub", "*": "mul"}[name], scalar(a), scalar(b)])
    if name == "shen.mod":
        if static:
            if a < 0 or b <= 0:
                raise Unsupported("static modulus outside non-negative operands")
            return a % b
        return Expr(["mod", scalar(a), scalar(b)])
    if name == "/":
        if static:
            if b == 0 or a % b:
                raise Unsupported("inexact static division")
            return a // b
        top, bottom = scalar(a), scalar(b)
        # Integer IR has no inexact quotient. Accept only (A - A mod B) / B.
        if (isinstance(top, list) and top[0] == "sub" and top[2] == ["mod", top[1], bottom]):
            return Expr(["div", top[1], bottom])
        raise Unsupported("a runtime quotient that may be inexact")
    left, right = (b, a) if name in (">", "<=") else (a, b)
    kind = "eq" if name == "=" else "lt" if name in ("<", ">") else "le"
    if name == "=" and not (isinstance(a, Expr) or isinstance(b, Expr)):
        return type(a) is type(b) and a == b
    if static:
        return left < right if kind == "lt" else left <= right
    return Test([kind, scalar(left), scalar(right)])


PRIMITIVES = {"+", "-", "*", "/", "shen.mod", "=", "<", ">", "<=", ">="}


class Specializer:
    def __init__(self, definitions, *, max_blocks=4096, fuel=5_000_000):
        self.definitions = definitions
        self.max_blocks = max_blocks
        self.fuel = fuel
        self.keys = {}
        self.pending = []

    def select(self, name, values):
        self.fuel -= 1
        if self.fuel < 0:
            raise Unsupported("unfolding budget exhausted: static computation may not terminate")
        for patterns, body in self.definitions[name]:
            env = {}
            if len(patterns) == len(values) and all(match(p, v, env) for p, v in zip(patterns, values)):
                return body, env
        raise Unsupported(f"no clause of {name} matches during specialization")

    def value(self, term, env):
        if isinstance(term, Cons):
            return Cons(self.value(term.head, env), self.value(term.tail, env))
        if isinstance(term, str):
            if term[0].isupper():
                return env[term]
            return {"true": True, "false": False}.get(term, term)
        if not isinstance(term, Call):
            return term
        name, *args = term.terms
        if name == "if":
            cond = self.value(args[0], env)
            if isinstance(cond, bool):
                return self.value(args[1 if cond else 2], env)
            if not isinstance(cond, Test):
                raise Unsupported("a condition that is not a boolean")
            # Both arms run during specialization, so they must be total scalars.
            then, other = scalar(self.value(args[1], env)), scalar(self.value(args[2], env))
            if (then, other) == (1, 0):
                return Expr(cond.ir)
            return Expr(["add", ["mul", cond.ir, then], ["mul", ["sub", 1, cond.ir], other]])
        values = [self.value(arg, env) for arg in args]
        if name in PRIMITIVES:
            return arithmetic(name, *values)
        if name not in self.definitions:
            raise Unsupported(f"unknown function {name}")
        body, local = self.select(name, values)
        return self.value(body, local)

    def jump(self, name, values):
        skeleton, actuals = generalize(values)
        key = name, skeleton
        if key not in self.keys:
            if len(self.keys) == self.max_blocks:
                raise Unsupported("block budget exhausted: a static value varies without bound "
                                  "under runtime control")
            self.keys[key] = len(self.keys)
            self.pending.append(key)
        return Jump(self.keys[key], actuals)

    def tail(self, term, env, point):
        """Residual control for a term in tail position."""
        while isinstance(term, Call):
            name, *args = term.terms
            if name == "if":
                cond = self.value(args[0], env)
                if isinstance(cond, bool):
                    term = args[1 if cond else 2]
                    continue
                if not isinstance(cond, Test):
                    raise Unsupported("a condition that is not a boolean")
                return If(cond.ir, self.tail(args[1], env, True), self.tail(args[2], env, True))
            if name not in self.definitions:
                break
            values = [self.value(arg, env) for arg in args]
            if point:
                return self.jump(name, values)
            term, env = self.select(name, values)
        return Return(scalar(self.value(term, env)))

    def run(self, entry, values):
        start = self.jump(entry, values)
        blocks = []
        while len(blocks) < len(self.pending):
            name, skeleton = self.pending[len(blocks)]
            body, env = self.select(name, [instantiate(v) for v in skeleton])
            blocks.append(self.tail(body, env, False))
        return start, blocks


def lower(code):
    if isinstance(code, If):
        return ["if", code.cond, lower(code.then), lower(code.other)]
    if isinstance(code, Return):
        return ["seq", ["set", RESULT, code.value], ["set", STATE, -1]]
    moves = [(n, value) for n, value in enumerate(code.actuals) if value != ["var", n]]
    if len(moves) == 1:
        (n, value), = moves
        return ["seq", ["set", n, value], ["set", STATE, code.block]]
    # Simultaneous assignment: every actual reads the old frame.
    return ["seq", *(["set", TEMP + n, value] for n, value in moves),
            *(["set", n, ["var", TEMP + n]] for n, _ in moves), ["set", STATE, code.block]]


def program(start, blocks):
    dispatch = [["if", ["eq", ["var", STATE], n], lower(block), ["seq"]]
                for n, block in enumerate(blocks)]
    return ["seq", lower(start), ["while", ["le", 0, ["var", STATE]], ["seq", *dispatch]],
            ["emit", ["var", RESULT]]]


def static(value):
    """JSON to a specialization-time value; {"input": n} is the runtime integer at index n."""
    if isinstance(value, dict):
        index, = value.values()
        if list(value) != ["input"] or type(index) is not int or index < 0:
            raise ValueError("expected {\"input\": index}")
        return Expr(["at", index])
    if isinstance(value, list):
        return sequence([static(item) for item in value])
    return value


def derive(source, entry, arguments, **limits):
    text = re.sub(r"\\\\.*", "", Path(source).read_text())
    specializer = Specializer(parse(text), **limits)
    start, blocks = specializer.run(entry, [static(a) for a in arguments])
    return program(start, blocks), len(blocks)


if __name__ == "__main__":
    import argparse
    import json
    from specializer.backend import generate, normalize

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("entry")
    parser.add_argument("arguments", help='JSON array; {"input": n} marks a runtime integer')
    parser.add_argument("--emit", choices=("ir", "c"), default="ir")
    args = parser.parse_args()
    tree = normalize(derive(args.source, args.entry, json.loads(args.arguments))[0])
    print(generate(tree) if args.emit == "c" else json.dumps(tree, indent=2))
