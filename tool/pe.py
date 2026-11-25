"""Online partial evaluator for first-order Shen.

The evaluator runs an ordinary Shen program on values whose leaves are either
known (numbers, symbols, booleans, list structure) or runtime integers. It knows
no function name, clause shape or loop form of the program it specializes.

* A clause pattern or `=` that inspects a runtime leaf becomes a residual test,
  and clause selection becomes residual branches.
* A runtime conditional in value position evaluates both arms and joins them:
  leaves that differ become one temporary, shapes that differ stay a `Split`
  that is only resolved where something inspects it.
* A call in tail position under runtime control that re-enters a function
  already being unfolded is a specialization point. Its arguments are
  generalized and (function, static skeleton) names a residual block.
* Residual expressions are hash-consed per scope. A node used more than once is
  bound to a slot at the place it was created, so nothing is duplicated.

`experiments/branching_vm/pe.py` is a thin front for `derive` below, and
`tool/compile.py` builds node step functions on the same `Specializer`.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import sys

sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))

# shen-jit computes exactly on integers in [-2^61, 2^61 - 1] and raises "integer overflow"
# when + - * leave that range. Its reader turns a literal outside (-2^61, 2^61) into a float.
NUM_MIN, NUM_MAX = -(1 << 61), (1 << 61) - 1


class Unsupported(ValueError):
    """The source or the requested division is outside the implemented fragment."""


class Trapped(Exception):
    """The path being specialized always fails at run time; a trap was emitted."""


class NeedSplit(Exception):
    def __init__(self, split):
        self.split = split


@dataclass(frozen=True)
class Call:
    terms: tuple


@dataclass(frozen=True)
class Cons:
    head: object
    tail: object


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Param:
    index: int
    kind: str = "num"


@dataclass(frozen=True, eq=False)
class Split:
    """A value that is `a` when the runtime boolean `cond` holds and `b` otherwise."""
    cond: object
    a: object
    b: object


class Node:
    """A runtime integer: an input, a slot, or an operation on nodes and constants."""
    __slots__ = ("op", "args", "kind", "syms", "depth", "force", "uses", "slot", "sets", "must")

    def __init__(self, op, args, kind, syms=None, depth=0):
        self.op, self.args, self.kind, self.syms, self.depth = op, args, kind, syms, depth
        self.force, self.uses, self.slot, self.sets = False, 0, None, []
        self.must = False       # may trap: evaluated where it was created, used or not

    def __repr__(self):
        return f"<{self.kind} {self.op} {self.args}>"


class Buffer:
    """Residual statements of one control-flow arm, with its CSE and known-condition scope."""
    __slots__ = ("items", "cse", "known", "parent")

    def __init__(self, parent=None):
        self.items, self.cse, self.known, self.parent = [], {}, {}, parent


def shen_mod(a, b):
    """(shen.mod A B) as the Shen kernel defines it, or None where it has no answer.

    A when B > A. Otherwise B must be positive (a zero divisor loops, a negative one
    overflows), and the kernel doubles B until it exceeds A, which must stay exact.
    """
    if b > a:
        return a
    if b <= 0:
        return None
    top = b
    while top <= a:
        top *= 2
    return a % b if top <= NUM_MAX else None


BOTTOM = object()
ATOMS = ("at", "var")
INLINE_DEPTH = 16


def sequence(items, tail=None):
    for item in reversed(items):
        tail = Cons(item, tail)
    return tail


def items_of(value):
    """A proper static list as a Python list."""
    out = []
    while isinstance(value, Cons):
        out.append(value.head)
        value = value.tail
    if value is not None:
        raise Unsupported("expected a list of statically known length")
    return out


def is_variable(term):
    return isinstance(term, str) and term[:1].isupper()


def kind_of(value):
    if isinstance(value, Node):
        return value.kind
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "num"
    if type(value) is str:
        return "sym"
    return None


def has_dynamic(value):
    if isinstance(value, Cons):
        return has_dynamic(value.head) or has_dynamic(value.tail)
    return isinstance(value, Node)


def find_split(value):
    if isinstance(value, Split):
        return value
    if isinstance(value, Cons):
        return find_split(value.head) or find_split(value.tail)
    if isinstance(value, (list, tuple)):
        for item in value:
            found = find_split(item)
            if found:
                return found
    return None


def subst(value, split, replacement):
    if value is split:
        return replacement
    if isinstance(value, Cons):
        return Cons(subst(value.head, split, replacement), subst(value.tail, split, replacement))
    if isinstance(value, list):
        return [subst(item, split, replacement) for item in value]
    return value


# ---------------------------------------------------------------------------- reader

TOKEN = re.compile(r'"[^"]*"|[()\[\]|]|[^\s()\[\]|"]+')


def strip_comments(text):
    text = re.sub(r"\\\*.*?\*\\", " ", text, flags=re.DOTALL)
    return re.sub(r"\\\\.*", "", text)


def atom(token):
    if token.startswith('"'):
        return Text(token[1:-1])
    if re.fullmatch(r"[-+]?\d+", token):
        if not -NUM_MAX <= int(token) <= NUM_MAX:
            raise Unsupported(f"integer literal {token} is outside the range Shen reads exactly")
        return int(token)
    if re.match(r"[-+]?\.?\d", token):
        # 1.5, 1e2, .5, 1/2 ...: Shen reads a number (or something stranger) here.
        raise Unsupported(f"number {token} is not a plain integer")
    return {"true": True, "false": False}.get(token, token)


def desugar(terms):
    head = terms[0] if terms else None
    if head == "cases":
        body = Call(("simple-error", Text("cases exhausted")))
        pairs = terms[1:]
        if len(pairs) % 2:
            raise Unsupported("cases needs condition/result pairs")
        for n in range(len(pairs) - 2, -1, -2):
            body = Call(("if", pairs[n], pairs[n + 1], body))
        return body
    if head == "let" and len(terms) > 4:
        if len(terms) % 2:
            raise Unsupported("malformed let")
        body = terms[-1]
        for n in range(len(terms) - 3, 0, -2):
            body = Call(("let", terms[n], terms[n + 1], body))
        return body
    return Call(tuple(terms))


def read_forms(source):
    tokens = TOKEN.findall(strip_comments(source))
    pos = 0

    def read():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token not in ("(", "["):
            if token in (")", "]", "|"):
                raise Unsupported(f"unexpected {token}")
            return atom(token)
        end = ")" if token == "(" else "]"
        items, tail = [], None
        while tokens[pos] != end:
            if tokens[pos] == "|":
                pos += 1
                tail = read()
                if tokens[pos] != "]" or token != "[":
                    raise Unsupported("invalid list tail")
                break
            items.append(read())
        pos += 1
        return desugar(items) if token == "(" else sequence(items, tail)

    forms = []
    while pos < len(tokens):
        forms.append(read())
    return forms


def parse(source):
    """Top-level `define` forms to {name: [(patterns, guard, body), ...]}."""
    definitions = {}
    for form in read_forms(source):
        if not isinstance(form, Call) or len(form.terms) < 2 or form.terms[0] != "define":
            raise Unsupported("expected only first-order (define ...) forms")
        name, rest = form.terms[1], list(form.terms[2:])
        if rest and rest[0] == "{":
            rest = rest[rest.index("}") + 1:]
        clauses, start = [], 0
        while start < len(rest):
            if "<-" in rest[start:]:
                raise Unsupported("backtracking clauses (<-)")
            arrow = rest.index("->", start)
            patterns, body, start = tuple(rest[start:arrow]), rest[arrow + 1], arrow + 2
            guard = None
            if start < len(rest) and rest[start] == "where":
                guard, start = rest[start + 1], start + 2
            clauses.append((patterns, guard, body))
        if len({len(p) for p, _, _ in clauses}) != 1:
            raise Unsupported(f"{name}: clauses of different arity")
        definitions[name] = clauses
    return definitions


# Shen system functions a specification may call, written as the Shen they mean.
PRELUDE_SOURCE = """
(define append [] Ys -> Ys  [X | Xs] Ys -> [X | (append Xs Ys)])
(define length [] -> 0  [_ | Xs] -> (+ 1 (length Xs)))
(define element? _ [] -> false  X [Y | Ys] -> (if (= X Y) true (element? X Ys)))
(define reverse Xs -> (tool.reverse Xs []))
(define tool.reverse [] Acc -> Acc  [X | Xs] Acc -> (tool.reverse Xs [X | Acc]))
(define nth 1 [X | _] -> X  N [_ | Xs] -> (nth (- N 1) Xs))
(define hd [X | _] -> X)
(define tl [_ | Xs] -> Xs)
(define cons X Y -> [X | Y])
(define cons? [_ | _] -> true  _ -> false)
(define empty? [] -> true  _ -> false)
"""
PRELUDE = parse(PRELUDE_SOURCE)
PRIMITIVES = {"+": 2, "-": 2, "*": 2, "/": 2, "shen.mod": 2, "=": 2, "<": 2, ">": 2, "<=": 2,
              ">=": 2, "not": 1}


# ------------------------------------------------------------------------ specializer

class Specializer:
    def __init__(self, definitions, *, symbols=None, encoder=None, max_blocks=4096,
                 fuel=5_000_000, widen=None, division=None, strict=False):
        for name in definitions:
            if name in PRELUDE or name in PRIMITIVES:
                raise Unsupported(f"{name} redefines a Shen system function")
        self.definitions = {**PRELUDE, **definitions}
        self.symbols = symbols            # {symbol: integer code} or None
        self.encoder = encoder or (lambda value: [self.number(value)])
        self.max_blocks, self.fuel = max_blocks, fuel
        self.widen, self.division, self.strict = widen, division or {}, strict
        self.keys, self.pending = {}, []
        self.seen, self.wide = {}, {}
        self.buf = None
        self.temps = 0

    # ---- residual nodes

    def node(self, op, args, kind="num", syms=None, must=False):
        key = (op, kind, tuple(a if type(a) is int else id(a) for a in args))
        scope = self.buf
        while scope is not None:
            hit = scope.cse.get(key)
            if hit is not None:
                return hit
            scope = scope.parent
        depth = 0 if op in ATOMS else 1 + max(
            (a.depth for a in args if isinstance(a, Node) and not a.force), default=0)
        made = Node(op, args, kind, syms, depth)
        if depth > INLINE_DEPTH or must:
            made.force, made.depth = True, 0
        made.must = must
        self.buf.cse[key] = made
        self.buf.items.append(("def", made))
        return made

    def input(self, index, kind="num"):
        return self.node("at", (index,), kind)

    def temp(self, kind, syms=None):
        self.temps += 1
        return Node("var", (("t", self.temps - 1),), kind, syms)

    def code(self, symbol):
        if self.symbols is None or symbol not in self.symbols:
            raise Unsupported(f"symbol {symbol} meets runtime data but is not in the symbol table")
        return self.symbols[symbol]

    def operand(self, value):
        """A leaf as a residual operand: a node or an integer constant."""
        if isinstance(value, Node):
            return value
        if type(value) is bool:
            return int(value)
        if type(value) is int:
            return value
        if type(value) is str:
            return self.code(value)
        raise Unsupported(f"expected a number, symbol or boolean where the program supplies {value!r}")

    def number(self, value):
        if type(value) is int or (isinstance(value, Node) and value.kind == "num"):
            return value
        raise Unsupported(f"expected a number where the program supplies {value!r}")

    def negate(self, cond):
        return self.node("eq", (cond, 0), "bool")

    def trap(self):
        self.buf.items.append(("trap",))
        raise Trapped()

    def fail(self, why):
        """A static computation that Shen answers with an error.

        A node step traps on that path, as Shen does. Elsewhere it is refused.
        """
        if self.strict:
            self.trap()
        raise Unsupported(why)

    def comparison(self, name, a, b):
        """(operation, left, right) for one of < > <= >= applied to a and b."""
        left, right = (b, a) if name in (">", ">=") else (a, b)
        return ("lt" if name in ("<", ">") else "le"), left, right

    def block_key(self, name, skeleton):
        """The name of a residual block: the function and its static arguments, by type.

        Python has True == 1 and False == 0, so a static leaf carries its kind.
        """
        def typed(value):
            if isinstance(value, Cons):
                return "cons", typed(value.head), typed(value.tail)
            return value if isinstance(value, Param) or value is None else (kind_of(value), value)
        return name, tuple(typed(value) for value in skeleton)

    def known(self, cond):
        scope = self.buf
        while scope is not None:
            hit = scope.known.get(id(cond))
            if hit is not None:
                return hit[1]
            scope = scope.parent
        return None

    # ---- equality, matching, primitives

    def equal(self, a, b):
        if isinstance(a, Split):
            raise NeedSplit(a)
        if isinstance(b, Split):
            raise NeedSplit(b)
        if isinstance(a, Cons) or isinstance(b, Cons):
            if not (isinstance(a, Cons) and isinstance(b, Cons)):
                return False
            head = self.equal(a.head, b.head)
            if head is False:
                return False
            tail = self.equal(a.tail, b.tail)
            if tail is False or head is True:
                return tail
            return head if tail is True else self.node("and", (head, tail), "bool")
        if not isinstance(a, Node) and not isinstance(b, Node):
            return type(a) is type(b) and a == b
        if isinstance(a, Node) and isinstance(b, Node):
            if a.kind != b.kind:
                return False
            return True if a is b else self.node("eq", (a, b), "bool")
        if isinstance(b, Node):
            a, b = b, a
        if a.kind != kind_of(b):
            return False
        if a.kind == "bool":
            return a if b else self.negate(a)
        if a.kind == "sym" and a.syms is not None and b not in a.syms:
            return False
        return self.node("eq", (a, self.operand(b)), "bool")

    def match(self, pattern, value, env, conds):
        """False on a static mismatch. Runtime tests the match depends on are added to conds."""
        if pattern == "_" and type(pattern) is str:
            return True
        if is_variable(pattern):
            if pattern not in env:
                env[pattern] = value
                return True
            test = self.equal(env[pattern], value)
        elif isinstance(value, Split):
            raise NeedSplit(value)
        elif isinstance(pattern, Cons):
            return (isinstance(value, Cons) and self.match(pattern.head, value.head, env, conds)
                    and self.match(pattern.tail, value.tail, env, conds))
        elif isinstance(pattern, (Call, Text)):
            raise Unsupported(f"pattern {pattern!r}")
        else:
            test = self.equal(pattern, value)
        if test is False:
            return False
        if test is not True:
            conds.append(test)
        return True

    def primitive(self, name, a, b=None):
        for value in (a, b):
            if isinstance(value, Split):
                raise NeedSplit(value)
        if name == "=":
            return self.equal(a, b)
        if name == "not":
            if type(a) is bool:
                return not a
            if isinstance(a, Node) and a.kind == "bool":
                return self.negate(a)
            raise Unsupported("not applied to a value that is not a boolean")
        a, b = self.number(a), self.number(b)
        static = type(a) is int and type(b) is int
        if name in ("+", "-", "*"):
            if static:
                result = {"+": a + b, "-": a - b, "*": a * b}[name]
                if not NUM_MIN <= result <= NUM_MAX:
                    return self.fail("static arithmetic leaves the exact range of Shen integers")
                return result
            return self.node({"+": "add", "-": "sub", "*": "mul"}[name], (a, b), must=self.strict)
        if name == "shen.mod":
            if static:
                result = shen_mod(a, b)
                if result is None:
                    return self.fail("shen.mod has no answer for these operands")
                return result
            return self.node("mod", (a, b), must=self.strict)
        if name == "/":
            if static:
                if b == 0:
                    return self.fail("division by zero")
                if a % b:
                    raise Unsupported("inexact static division")
                if not NUM_MIN <= a // b <= NUM_MAX:
                    return self.fail("static arithmetic leaves the exact range of Shen integers")
                return a // b
            # Integer IR has no inexact quotient. Accept only (A - A mod B) / B.
            def same(x, y):
                return x is y or (type(x) is int and type(y) is int and x == y)
            if isinstance(a, Node) and a.op == "sub":
                top, rest = a.args
                if (isinstance(rest, Node) and rest.op == "mod" and same(rest.args[0], top)
                        and same(rest.args[1], b)):
                    return self.node("div", (top, b), must=self.strict)
            raise Unsupported("a runtime quotient that may be inexact")
        kind, left, right = self.comparison(name, a, b)
        if static:
            return left < right if kind == "lt" else left <= right
        return self.node(kind, (left, right), "bool")

    # ---- value position

    def arm(self, cond, polarity, thunk):
        outer = self.buf
        self.buf = Buffer(outer)
        self.buf.known[id(cond)] = (cond, polarity)
        try:
            value = thunk()
        except Trapped:
            value = BOTTOM
        buf, self.buf = self.buf, outer
        return buf, value

    def branch(self, cond, then, other):
        """Join two values under a condition that may only be known at run time."""
        if cond is True:
            return then()
        if cond is False:
            return other()
        if isinstance(cond, Split):
            return self.branch(cond.cond, lambda: self.branch(cond.a, then, other),
                               lambda: self.branch(cond.b, then, other))
        if not (isinstance(cond, Node) and cond.kind == "bool"):
            raise Unsupported("a condition that is not a boolean")
        known = self.known(cond)
        if known is not None:
            return then() if known else other()
        a, left = self.arm(cond, True, then)
        b, right = self.arm(cond, False, other)
        self.buf.items.append(("if", cond, a, b))
        if left is BOTTOM and right is BOTTOM:
            raise Trapped()
        return self.merge(cond, left, right, a, b)

    def joinable(self, a, b):
        """Whether two values have one shape, so that they join leaf by leaf with no Split."""
        if isinstance(a, Cons) or isinstance(b, Cons):
            return (isinstance(a, Cons) and isinstance(b, Cons)
                    and self.joinable(a.head, b.head) and self.joinable(a.tail, b.tail))
        kind = kind_of(a)
        if kind != kind_of(b) or isinstance(a, Split) or isinstance(b, Split):
            return False
        return kind != "sym" or a is b or a == b or all(
            isinstance(v, Node) or (self.symbols is not None and v in self.symbols) for v in (a, b))

    def merge(self, cond, a, b, abuf, bbuf):
        if a is BOTTOM:
            return b
        if b is BOTTOM:
            return a
        if a is b:
            return a
        if isinstance(a, Cons) and isinstance(b, Cons):
            # Lists join element by element. Lists of different length stay a Split, and so
            # does a record whose tags differ unless the rest of it has one shape: a tag
            # must never be separated from the fields it describes.
            xs, ys, x, y = [], [], a, b
            while isinstance(x, Cons) and isinstance(y, Cons):
                xs.append(x.head)
                ys.append(y.head)
                x, y = x.tail, y.tail
            tagged = kind_of(xs[0]) == "sym" and kind_of(ys[0]) == "sym" and xs[0] != ys[0]
            if isinstance(x, Cons) or isinstance(y, Cons) or (tagged and not self.joinable(a, b)):
                return Split(cond, a, b)
            return sequence([self.merge(cond, p, q, abuf, bbuf) for p, q in zip(xs, ys)],
                            self.merge(cond, x, y, abuf, bbuf))
        if not self.joinable(a, b):
            return Split(cond, a, b)
        kind, syms = kind_of(a), None
        if kind is None:
            return a
        if not isinstance(a, Node) and not isinstance(b, Node):
            if a == b:
                return a
            if kind == "num" and (a, b) == (1, 0):     # the comparison itself, read as 0 or 1
                return (self.node("add", (cond, 0)) if cond.op in ATOMS
                        else self.node(cond.op, cond.args, "num"))
            if kind == "bool":
                return cond if a else self.negate(cond)
        if kind == "sym":
            parts = [frozenset([v]) if type(v) is str else v.syms for v in (a, b)]
            syms = None if None in parts else parts[0] | parts[1]
        joined = self.temp(kind, syms)
        for buf, leaf in ((abuf, a), (bbuf, b)):
            leaf = self.operand(leaf)
            joined.sets.append(leaf)
            buf.items.append(("set", joined, leaf))
        return joined

    def distribute(self, values, function):
        try:
            return function(values)
        except NeedSplit as need:
            split = need.split
            return self.branch(split.cond,
                               lambda: self.distribute(subst(values, split, split.a), function),
                               lambda: self.distribute(subst(values, split, split.b), function))

    def value(self, term, env):
        if isinstance(term, Cons):
            head = self.value(term.head, env)
            return Cons(head, self.value(term.tail, env))
        if isinstance(term, str):
            if not is_variable(term):
                return term
            if term not in env:
                raise Unsupported(f"unbound variable {term}")
            return env[term]
        if isinstance(term, Text):
            raise Unsupported("strings")
        if not isinstance(term, Call):
            return term
        name, *args = term.terms
        if name == "if":
            return self.branch(self.value(args[0], env), lambda: self.value(args[1], env),
                               lambda: self.value(args[2], env))
        if name == "and":
            return self.branch(self.value(args[0], env), lambda: self.value(args[1], env),
                               lambda: False)
        if name == "or":
            return self.branch(self.value(args[0], env), lambda: True,
                               lambda: self.value(args[1], env))
        if name == "let":
            return self.value(args[2], {**env, args[0]: self.value(args[1], env)})
        if name in ("simple-error", "error"):
            self.trap()
        if not isinstance(name, str) or is_variable(name):
            raise Unsupported("higher-order application")
        values = [self.value(arg, env) for arg in args]
        if name in PRIMITIVES:
            if len(values) != PRIMITIVES[name]:
                raise Unsupported(f"partial application of {name}")
            return self.distribute(values, lambda vs: self.primitive(name, *vs))
        if name not in self.definitions:
            raise Unsupported(f"unknown function {name}")
        return self.call(name, values)

    def probe(self, name, values):
        """Raise NeedSplit before any residual branch if a clause must inspect a Split."""
        for patterns, guard, _ in self.definitions[name]:
            if len(patterns) != len(values):
                raise Unsupported(f"{name} applied to {len(values)} arguments")
            env, conds, matched = {}, [], True
            for pattern, value in zip(patterns, values):
                if not self.match(pattern, value, env, conds):
                    matched = False
                    break
            if matched and not conds and guard is None:
                return

    def call(self, name, values):
        try:
            self.probe(name, values)
        except NeedSplit as need:
            split = need.split
            return self.branch(split.cond, lambda: self.call(name, subst(values, split, split.a)),
                               lambda: self.call(name, subst(values, split, split.b)))
        return self.select(name, values, 0, lambda body, env, dyn: self.value(body, env),
                           lambda cond, then, other, dyn: self.branch(
                               cond, lambda: then(True), lambda: other(True)), False)

    def select(self, name, values, start, on_clause, fork, dyn):
        """First clause that matches. Clauses whose match needs runtime tests become branches."""
        self.fuel -= 1
        if self.fuel < 0:
            raise Unsupported("unfolding budget exhausted: static computation may not terminate")
        clauses = self.definitions[name]
        for index in range(start, len(clauses)):
            patterns, guard, body = clauses[index]
            env, conds, matched = {}, [], True
            for pattern, value in zip(patterns, values):
                if not self.match(pattern, value, env, conds):
                    matched = False
                    break
            if not matched:
                continue
            test = True
            for cond in conds:
                test = cond if test is True else self.node("and", (test, cond), "bool")
            if guard is not None:
                holds = lambda: self.value(guard, env)
                test = holds() if test is True else self.branch(test, holds, lambda: False)
            if test is False:
                continue
            return fork(test, lambda d: on_clause(body, env, d),
                        lambda d: self.select(name, values, index + 1, on_clause, fork, d), dyn)
        self.trap()

    # ---- tail position

    def fork(self, cond, then, other, dyn):
        """Residual control: each arm continues on its own and never joins."""
        if cond is True:
            return then(dyn)
        if cond is False:
            return other(dyn)
        if isinstance(cond, Split):
            return self.fork(cond.cond, lambda d: self.fork(cond.a, then, other, d),
                             lambda d: self.fork(cond.b, then, other, d), dyn)
        if not (isinstance(cond, Node) and cond.kind == "bool"):
            raise Unsupported("a condition that is not a boolean")
        known = self.known(cond)
        if known is not None:
            return then(dyn) if known else other(dyn)
        a, _ = self.arm(cond, True, lambda: then(True))
        b, _ = self.arm(cond, False, lambda: other(True))
        self.buf.items.append(("if", cond, a, b))

    def tail(self, term, env, dyn, trail):
        while isinstance(term, Call):
            name, *args = term.terms
            if name == "if":
                cond = self.value(args[0], env)
                return self.fork(cond, lambda d: self.tail(args[1], env, d, trail),
                                 lambda d: self.tail(args[2], env, d, trail), dyn)
            if name == "let":
                term, env = args[2], {**env, args[0]: self.value(args[1], env)}
                continue
            if name not in self.definitions:
                break
            return self.tail_call(name, [self.value(arg, env) for arg in args], dyn, trail)
        self.finish(self.value(term, env))

    def tail_call(self, name, values, dyn, trail):
        try:
            self.probe(name, values)
        except NeedSplit as need:
            split = need.split
            return self.fork(
                split.cond,
                lambda d: self.tail_call(name, subst(values, split, split.a), d, trail),
                lambda d: self.tail_call(name, subst(values, split, split.b), d, trail), dyn)
        if dyn and name in trail:
            return self.jump(name, values)
        self.select(name, values, 0,
                    lambda body, env, d: self.tail(body, env, d, trail | {name}), self.fork, dyn)

    def finish(self, value):
        split = find_split(value)
        if split:
            return self.fork(split.cond, lambda d: self.finish(subst(value, split, split.a)),
                             lambda d: self.finish(subst(value, split, split.b)), True)
        self.buf.items.append(("ret", [self.operand(v) for v in self.encoder(value)]))

    # ---- specialization points

    def generalize(self, name, values):
        """Split each argument into a static skeleton and the runtime values that fill it."""
        actuals = []

        def lift(value, chosen, counter):
            if isinstance(value, Cons):
                head = lift(value.head, chosen, counter)
                return Cons(head, lift(value.tail, chosen, counter))
            if isinstance(value, Node):
                actuals.append(value)
                return Param(len(actuals) - 1, value.kind)
            if type(value) is int:
                counter[0] += 1
                if chosen is None or counter[0] - 1 in chosen:
                    actuals.append(value)
                    return Param(len(actuals) - 1)
            return value

        def erase(value):
            if isinstance(value, Cons):
                return Cons(erase(value.head), erase(value.tail))
            return "#" if type(value) is int else "?" if isinstance(value, Node) else value

        def numbers(value, out):
            if isinstance(value, Cons):
                numbers(value.head, out)
                numbers(value.tail, out)
            elif type(value) is int:
                out.append(value)
            return out

        modes = self.division.get(name, ())
        skeleton = []
        for index, value in enumerate(values):
            mode = modes[index] if index < len(modes) else None
            if mode == "D" or (mode is None and has_dynamic(value)):
                chosen = None                      # every number is a runtime value
            elif mode == "S" or self.widen is None:
                chosen = ()                        # static numbers stay in the block's name
            else:
                # Widening: a static number that takes more than `widen` distinct values at
                # one position of one argument shape is a runtime value from then on.
                key = name, index, erase(value)
                found = numbers(value, [])
                seen = self.seen.setdefault(key, [set() for _ in found])
                chosen = self.wide.setdefault(key, set())
                for position, number in enumerate(found):
                    seen[position].add(number)
                    if len(seen[position]) > self.widen:
                        chosen.add(position)
            skeleton.append(lift(value, chosen, [0]))
        return tuple(skeleton), actuals

    def jump(self, name, values):
        split = find_split(values)
        if split:
            return self.fork(split.cond, lambda d: self.jump(name, subst(values, split, split.a)),
                             lambda d: self.jump(name, subst(values, split, split.b)), True)
        skeleton, actuals = self.generalize(name, values)
        key = self.block_key(name, skeleton)
        if key not in self.keys:
            if len(self.keys) == self.max_blocks:
                raise Unsupported("block budget exhausted: a static value varies without bound "
                                  "under runtime control")
            self.keys[key] = len(self.keys)
            self.pending.append((name, skeleton))
        self.buf.items.append(("jump", self.keys[key], actuals))

    def block(self, body):
        """Run `body` in a fresh root scope and return (statements, temporaries used)."""
        self.buf, self.temps = Buffer(), 0
        try:
            body()
        except Trapped:
            pass
        except RecursionError:
            raise Unsupported(
                "unfolding too deep: recursion that no runtime test in tail position controls "
                "does not end within the evaluator's depth (static recursion deeper than a few "
                "thousand calls, a loop that never ends, or recursion on a runtime value outside "
                "tail position)") from None
        buf, self.buf = self.buf, None
        return buf, self.temps

    def blocks(self):
        """Generate every pending specialization point until no new one appears."""
        done = []
        while len(done) < len(self.pending):
            name, skeleton = self.pending[len(done)]

            def instantiate(value):
                if isinstance(value, Cons):
                    return Cons(instantiate(value.head), instantiate(value.tail))
                if isinstance(value, Param):
                    return Node("var", (("p", value.index),), value.kind)
                return value

            values = [instantiate(v) for v in skeleton]
            done.append(self.block(lambda: self.select(
                name, values, 0, lambda body, env, d: self.tail(body, env, d, frozenset([name])),
                self.fork, False)))
        return done


# --------------------------------------------------------------------------- lowering

class Lowering:
    """Buffers to the structured integer IR of specializer/FORMAT.md."""

    def __init__(self, *, params, legacy, loop, share=True):
        self.share, self.legacy, self.loop, self.params, self.used = share, legacy, loop, params, 0
        if legacy:      # the slot layout of the first branching evaluator, inside 1024 slots
            self.state, self.result, self.shadow = 1023, 1022, 512
            self.temp_slot = lambda n: 1021 - n
        else:           # frame, shadow frame, block number, then temporaries
            self.shadow, self.state, self.result = params, 2 * params, None
            self.temp_slot = lambda n: 2 * params + 1 + n

    @property
    def slots(self):
        if not self.legacy:
            return 2 * self.params + 1 + self.used
        if self.params > 500 or 1022 - self.used < 512 + self.params:
            raise Unsupported("too many runtime values live at one program point")
        return 1024

    def mark(self, value):
        if not isinstance(value, Node):
            return
        value.uses += 1
        if value.uses == 1:
            for child in (value.sets if value.op == "var" else
                          () if value.op == "at" else value.args):
                self.mark(child)

    def bound(self, node):
        return node.op not in ATOMS and (node.uses >= 2 and self.share or node.force) and node.uses

    def roots(self, buf):
        for item in buf.items:
            if item[0] in ("jump", "ret"):
                for value in item[-1]:
                    self.mark(value)
            elif item[0] == "def" and item[1].must:
                self.mark(item[1])
            elif item[0] == "if":
                self.roots(item[2])
                self.roots(item[3])

    def effective(self, buf):
        result = False
        for item in buf.items:
            tag = item[0]
            if tag in ("trap", "jump", "ret"):
                result = True
            elif tag == "set":
                result |= item[1].uses > 0
            elif tag == "def":
                result |= bool(self.bound(item[1]))
            elif self.effective(item[2]) | self.effective(item[3]):
                if id(item) not in self.live:
                    self.live.add(id(item))
                    self.mark(item[1])
                    self.changed = True
                result = True
        return result

    def expr(self, value, define=False):
        if not isinstance(value, Node):
            return value
        if value.op == "at":
            return ["at", value.args[0]]
        if value.op == "var":
            space, n = value.args[0]
            return ["var", n if space == "p" else self.temp_slot(n)]
        if value.slot is not None and not define:
            return ["var", value.slot]
        return [value.op, *(self.expr(arg) for arg in value.args)]

    def statements(self, buf):
        out = []
        for item in buf.items:
            tag = item[0]
            if tag == "def":
                node = item[1]
                if self.bound(node):
                    node.slot = self.temp_slot(self.next)
                    self.next += 1
                    out.append(["set", node.slot, self.expr(node, define=True)])
            elif tag == "set":
                if item[1].uses:
                    out.append(["set", self.expr(item[1])[1], self.expr(item[2])])
            elif tag == "if":
                if id(item) in self.live:
                    out.append(["if", self.expr(item[1]), ["seq", *self.statements(item[2])],
                                ["seq", *self.statements(item[3])]])
            elif tag == "trap":
                out.append(["trap"])
            elif tag == "jump":
                actuals = [self.expr(v) for v in item[2]]
                moves = [(n, v) for n, v in enumerate(actuals) if v != ["var", n]]
                if len(moves) == 1:
                    out.append(["set", moves[0][0], moves[0][1]])
                else:       # simultaneous assignment: every actual reads the old frame
                    out += [["set", self.shadow + n, v] for n, v in moves]
                    out += [["set", n, ["var", self.shadow + n]] for n, _ in moves]
                out.append(["set", self.state, item[1]])
            else:
                values = [self.expr(v) for v in item[1]]
                if self.legacy:
                    out.append(["set", self.result, values[0]])
                else:
                    out += [["emit", v] for v in values]
                if self.loop:
                    out.append(["set", self.state, -1])
        return out

    def block(self, buf, temps):
        self.live, self.next = set(), temps
        self.roots(buf)
        self.changed = True
        while self.changed:
            self.changed = False
            self.effective(buf)
        code = self.statements(buf)
        self.used = max(self.used, self.next)
        return ["seq", *code]


def assemble(entry, blocks, *, legacy, share=True):
    """Entry code and residual blocks to one IR program. Returns (tree, slot count)."""
    def widest(buf):
        return max((len(item[2]) if item[0] == "jump" else
                    max(widest(item[2]), widest(item[3])) if item[0] == "if" else 0
                    for item in buf.items), default=0)

    params = max(widest(buf) for buf, _ in [entry, *blocks])
    lowering = Lowering(params=params, legacy=legacy, loop=bool(blocks), share=share)
    start = lowering.block(*entry)
    bodies = [lowering.block(*block) for block in blocks]
    state = ["var", lowering.state]
    program = ["seq"]
    if blocks and not legacy:
        program.append(["set", lowering.state, -1])
    program.append(start)
    if blocks:
        program.append(["while", ["le", 0, state], ["seq", *(
            ["if", ["eq", state, n], body, ["seq"]] for n, body in enumerate(bodies))]])
    if legacy:
        program.append(["emit", ["var", lowering.result]])
    return program, lowering.slots


def static(value, make_input):
    """JSON to a specialization-time value; {"input": n} is the runtime integer at index n."""
    if isinstance(value, dict):
        index, = value.values()
        if list(value) != ["input"] or type(index) is not int or index < 0:
            raise ValueError("expected {\"input\": index}")
        return make_input(index)
    if isinstance(value, list):
        return sequence([static(item, make_input) for item in value])
    return value


def derive(source, entry, arguments, *, share=True, **limits):
    """Specialize `entry` of a Shen file to a program that emits its one numeric answer.

    Limits: max_blocks, fuel, widen=<distinct values before a static number is widened>,
    division={function: ["S" | "D" | None, ...]} to fix the binding time of an argument.
    """
    specializer = Specializer(parse(Path(source).read_text()), **limits)
    start = specializer.block(lambda: specializer.jump(
        entry, [static(a, specializer.input) for a in arguments]))
    blocks = specializer.blocks()
    return assemble(start, blocks, legacy=True, share=share)[0], len(blocks)
