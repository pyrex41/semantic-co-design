"""Prepare a first-order query and rule base for repeated runs over new facts."""

from collections import deque
from dataclasses import dataclass
from itertools import count


@dataclass(frozen=True)
class _Var:
    scope: int
    position: int


class _TrackedNames:
    def __init__(self, layout):
        self.layout = layout
        self.positions = set()

    def __len__(self):
        return len(self.layout)

    def setdefault(self, name, position):
        offset = self.layout.setdefault(name, position)
        self.positions.add(offset)
        return offset


def _term(value):
    if isinstance(value, (list, tuple)):
        return tuple(map(_term, value))
    if value is None or isinstance(value, (int, str, float)):
        return value
    raise ValueError(f"unsupported query term {value!r}")


def _walk(value, bindings):
    while isinstance(value, _Var) and value in bindings:
        value = bindings[value]
    return value


def _occurs(variable, value, bindings):
    value = _walk(value, bindings)
    if isinstance(value, tuple):
        return any(_occurs(variable, part, bindings) for part in value)
    return value == variable


def _unify(left, right, bindings):
    """Extend this branch's frame or return None. Siblings keep their frame."""
    bindings = bindings.copy()
    pairs = list(zip(reversed(left), reversed(right)))
    while pairs:
        a, b = pairs.pop()
        a, b = _walk(a, bindings), _walk(b, bindings)
        if a == b:
            continue
        if isinstance(a, _Var) or isinstance(b, _Var):
            if not isinstance(a, _Var):
                a, b = b, a
            if _occurs(a, b, bindings):
                return None
            bindings[a] = b
        elif isinstance(a, tuple) and isinstance(b, tuple) and len(a) == len(b):
            pairs.extend(zip(reversed(a), reversed(b)))
        else:
            return None
    return bindings


def _ground(value, bindings):
    value = _walk(value, bindings)
    if isinstance(value, _Var):
        raise ValueError("query requires a ground value")
    if isinstance(value, tuple):
        return tuple(_ground(part, bindings) for part in value)
    return value


def compile_query(query, arities, *, rules=(), project=(), predicates=None):
    """Stage a finite-branching query with recursive rules and lazy answers.

    Goals are relation calls, ``and``, ``or``, ``same``, ``where`` and ground
    ``not``. A term beginning with '?' names a query or rule variable. Facts
    are a runtime mapping of relation names to iterable rows of ground terms.
    Projected answers must be ground. Duplicate proofs yield duplicate rows.
    """
    signatures = tuple(arities.items())
    slots = {name: index for index, (name, _) in enumerate(signatures)}
    forms = {"and", "or", "same", "where", "not"}
    if any(not isinstance(name, str) or name in forms or type(arity) is not int or arity < 0
           for name, arity in signatures):
        raise ValueError("invalid relation signature")
    predicates = dict(predicates or {})
    rule_slots = [[] for _ in signatures]

    def templates(values, names):
        def term(value):
            if isinstance(value, str) and value.startswith("?"):
                position = names.setdefault(value, len(names))
                return lambda scope: _Var(scope, position)
            if isinstance(value, (list, tuple)):
                if len(value) == 2 and value[0] == "quote":
                    literal = _term(value[1])
                    return lambda scope: literal
                return templates(value, names)
            literal = _term(value)
            return lambda scope: literal

        pieces = tuple(map(term, values))
        return lambda scope: tuple(piece(scope) for piece in pieces)

    def relation(name, args):
        index = slots[name]
        if len(args) != signatures[index][1]:
            raise ValueError(f"incorrect arity of relation {name!r}")
        return index

    def analyze(goal, names):
        tag, *parts = goal
        if tag == "and":
            steps = tuple(analyze(part, names) for part in parts)

            def conjunction(scope, context, bindings, rest):
                yield bindings, (*((scope, step) for step in steps), *rest)

            return conjunction
        if tag == "or":
            steps = tuple(analyze(part, names) for part in parts)

            def disjunction(scope, context, bindings, rest):
                for step in steps:
                    yield bindings, ((scope, step), *rest)

            return disjunction
        if tag == "same":
            left, right = parts
            terms = templates((left, right), names)

            def equality(scope, context, bindings, rest):
                a, b = terms(scope)
                extended = _unify((a,), (b,), bindings)
                if extended is not None:
                    yield extended, rest

            return equality
        if tag == "where":
            name, *args = parts
            test = predicates[name]
            if not callable(test):
                raise ValueError(f"predicate {name!r} is not callable")
            terms = templates(args, names)

            def where(scope, context, bindings, rest):
                if test(*_ground(terms(scope), bindings)):
                    yield bindings, rest

            return where
        if tag == "not":
            negated, = parts
            used = _TrackedNames(names)
            code = analyze(negated, used)
            offsets = tuple(sorted(used.positions))

            def negate(scope, context, bindings, rest):
                for offset in offsets:
                    _ground(_Var(scope, offset), bindings)
                absent = next(_search(context, bindings, ((scope, code),)), None) is None
                if absent:
                    yield bindings, rest

            return negate

        index = relation(tag, parts)
        args = templates(parts, names)
        definitions = rule_slots[index]

        def lookup(scope, context, bindings, rest):
            terms = args(scope)
            for fact in context.rows[index]:
                extended = _unify(terms, fact, bindings)
                if extended is not None:
                    yield extended, rest
            for head, body in definitions:
                fresh = next(context.scopes)
                extended = _unify(terms, head(fresh), bindings)
                if extended is not None:
                    yield extended, ((fresh, body), *rest)

        return lookup

    for head, body in rules:
        relation_name, *args = head
        index = relation(relation_name, args)
        names = {}
        rule_slots[index].append((templates(args, names), analyze(body, names)))
    names = {}
    code = analyze(query, names)
    output = templates(tuple(project), names)(0)

    def answers(facts, *, budget=None):
        if budget is not None and (type(budget) is not int or budget < 0):
            raise ValueError("query budget must be a nonnegative integer")
        rows = []
        for name, arity in signatures:
            relation_rows = tuple(tuple(map(_term, row)) for row in facts.get(name, ()))
            if any(len(row) != arity for row in relation_rows):
                raise ValueError(f"incorrect fact arity for relation {name!r}")
            rows.append(relation_rows)
        context = _Context(rows, count(1), budget)
        for frame in _search(context, {}, ((0, code),)):
            yield _ground(output, frame)

    return answers


@dataclass
class _Context:
    rows: list
    scopes: object
    budget: object


def _search(context, initial, goals):
    # Breadth-first scheduling keeps finite proofs reachable on a recursive branch.
    agenda = deque([(initial, goals)])
    while agenda:
        bindings, remaining = agenda.popleft()
        if not remaining:
            yield bindings
            continue
        if context.budget is not None:
            if context.budget == 0:
                raise RuntimeError("query budget exhausted")
            context.budget -= 1
        (scope, step), *rest = remaining
        agenda.extend(step(scope, context, bindings, tuple(rest)))
