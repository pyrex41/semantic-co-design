"""Compiled logic, definite-clause grammars and indexed worlds from PAIP.

A goal is a tuple (predicate, *arguments). Arguments can be strings,
integers, finite floats, booleans, None, or finite trees of tuples or lists. A string
beginning with ? denotes a variable. ? and ?_ are fresh at each use. Wrap
a variable-looking constant in Literal. Clause(head, body) defines a Horn
clause, with a sequence of goals as the body. An empty body defines a fact.
The built-in =/2 unifies its arguments. CUT, the literal goal ("!",),
commits to choices made so far in the enclosing predicate or top-level
query. Answers are ordered binding maps for named query variables, with
one map per proof.

A DCGRule has the same head syntax. Strings in its body consume literal
words, tuples call grammar categories, and Guard(goal) calls an ordinary
Horn goal without consuming any words. An empty body consumes nothing.
Features and semantic forms are ordinary logic terms. The compiler adds
input and remainder lists and then compiles the resulting Horn clauses.

A World has an ordered sequence of parent names and ground facts. It
inherits its parents' facts. Chapter 14 predicates sub, ind, rel and val
infer transitive subclass, membership and relation endpoint types. A
custom attachment maps a new fact to an iterable of derived facts.
Negative facts use an explicit predicate such as ~likes. Positive and
negative facts can be queried independently, so absent facts stay unknown.
"""

from collections import defaultdict
from dataclasses import dataclass
from itertools import islice
from math import isfinite

from .grammar import _count, _words
from .residual import compile_residual


CUT = ("!",)


@dataclass(frozen=True)
class Literal:
    value: str


@dataclass(frozen=True)
class Clause:
    head: tuple
    body: tuple = ()


@dataclass(frozen=True)
class DCGRule:
    head: tuple
    body: tuple = ()


@dataclass(frozen=True)
class Guard:
    goal: tuple


@dataclass(frozen=True)
class World:
    parents: tuple = ()
    facts: tuple = ()


class _LogicVar:
    __slots__ = ()


def _variable(term):
    return isinstance(term, str) and term.startswith("?")


def _anonymous(name):
    return name in ("?", "?_")


def _atom(term):
    if isinstance(term, Literal):
        if type(term.value) is not str:
            raise ValueError("literal text must be a string")
        return term.value
    if type(term) in (str, bool, int) or term is None:
        return term
    if type(term) is float and isfinite(term):
        return term
    raise ValueError(f"invalid logic atom {term!r}")


def _goal(goal):
    if not isinstance(goal, (tuple, list)) or not goal:
        raise ValueError("a goal must have a predicate followed by arguments")
    name, *arguments = goal
    if type(name) is not str or not name or _variable(name):
        raise ValueError(f"invalid predicate {name!r}")
    if name == "!" and arguments:
        raise ValueError("cut does not take arguments")
    return name, tuple(arguments)


def _walk(term, bindings):
    while isinstance(term, _LogicVar) and term in bindings:
        term = bindings[term]
    return term


def _occurs(variable, term, bindings):
    pending = [term]
    while pending:
        current = _walk(pending.pop(), bindings)
        if current is variable:
            return True
        if isinstance(current, tuple):
            pending.extend(current)
    return False


def _unify(left, right, bindings):
    pending = [(left, right)]
    result = bindings
    while pending:
        left, right = pending.pop()
        left, right = _walk(left, result), _walk(right, result)
        if left is right:
            continue
        if isinstance(right, _LogicVar) and not isinstance(left, _LogicVar):
            left, right = right, left
        if isinstance(left, _LogicVar):
            if _occurs(left, right, result):
                return None
            if result is bindings:
                result = bindings.copy()
            result[left] = right
        elif type(left) is not type(right):
            return None
        elif isinstance(left, tuple):
            if len(left) != len(right):
                return None
            pending.extend(zip(reversed(left), reversed(right)))
        elif left != right:
            return None
    return result


def _equality(arguments, bindings, depth):
    result = _unify(*arguments, bindings)
    if result is not None:
        yield result


def _no_match(arguments, bindings, depth):
    return ()


def _map_term(term, visit, leaf):
    """Rebuild structured terms, including long difference lists, on a stack."""
    pending = [(False, term)]
    values = []
    active_lists = set()
    while pending:
        finish, item = pending.pop()
        if finish:
            size, list_id = item
            if list_id is not None:
                active_lists.remove(list_id)
            end = len(values) - size
            children = tuple(values[end:])
            del values[end:]
            values.append(children)
            continue
        current = visit(item)
        if isinstance(current, (list, tuple)):
            list_id = id(current) if isinstance(current, list) else None
            if list_id is not None:
                if list_id in active_lists:
                    raise ValueError("logic terms must be finite trees")
                active_lists.add(list_id)
            pending.append((True, (len(current), list_id)))
            pending.extend((False, child) for child in reversed(current))
        else:
            values.append(leaf(current))
    return values[0]


def _query_term(term, variables):
    def convert(atom):
        if _variable(atom):
            if _anonymous(atom):
                return _LogicVar()
            if atom not in variables:
                variables[atom] = _LogicVar()
            return variables[atom]
        return _atom(atom)

    return _map_term(term, lambda node: node, convert)


def _answer(variables, bindings):
    names = {}
    for name, variable in variables.items():
        value = _walk(variable, bindings)
        if isinstance(value, _LogicVar):
            names.setdefault(value, name)
    used = set(variables)

    def substitute(term):
        if isinstance(term, _LogicVar):
            if term not in names:
                index = len(names)
                while f"?__{index}" in used:
                    index += 1
                name = f"?__{index}"
                used.add(name)
                names[term] = name
            return names[term]
        return term

    return {name: _map_term(variable, lambda term: _walk(term, bindings), substitute)
            for name, variable in variables.items()}


def _answers(resolve, goals, depth, limit):
    _count(depth, "max_depth")
    if limit is not None:
        _count(limit, "limit")
    variables = {}
    calls = []
    for goal in goals:
        name, arguments = _goal(goal)
        calls.append((name, _query_term(arguments, variables)))

    def prove():
        stack = []
        position, bindings = 0, {}
        while True:
            if position == len(calls):
                yield bindings
            else:
                name, arguments = calls[position]
                if name == "!":
                    stack.clear()
                    position += 1
                    continue
                stack.append((position, iter(resolve(name, arguments, bindings, depth))))

            while stack:
                index, choices = stack[-1]
                result = next(choices, None)
                if result is not None:
                    position, bindings = index + 1, result
                    break
                stack.pop()
            else:
                return

    proofs = prove()
    if limit is not None:
        proofs = islice(proofs, limit)
    return tuple(_answer(variables, bindings) for bindings in proofs)


def _term_source(term, variables, declarations):
    if _variable(term):
        if _anonymous(term) or term not in variables:
            name = f"var_{len(declarations)}"
            declarations.append(name)
            if _anonymous(term):
                return name
            variables[term] = name
        return variables[term]
    if isinstance(term, (list, tuple)):
        parts = [_term_source(child, variables, declarations) for child in term]
        if len(parts) == 1:
            return f"({parts[0]},)"
        return f"({', '.join(parts)})"
    return repr(_atom(term))


_HORN_BINDINGS = {
    "_LogicVar": _LogicVar,
    "_unify": _unify,
    "_no_match": _no_match,
    "_equality": _equality,
    "_answers": _answers,
}


def _horn_source(clauses):
    groups = {}
    for clause in clauses:
        name, arguments = _goal(clause.head)
        key = name, len(arguments)
        if key in (("=", 2), ("!", 0)):
            raise ValueError(f"{name}/{len(arguments)} is a built-in goal")
        body = tuple(map(_goal, clause.body))
        groups.setdefault(key, []).append((arguments, body))
    names = {key: f"_predicate_{i}" for i, key in enumerate(groups)}
    names["=", 2] = "_equality"
    lines = []
    for key, group in groups.items():
        lines += [f"def {names[key]}(arguments, bindings, depth):",
                  "    if depth == 0:",
                  "        return"]
        initial_length = len(lines)
        for head, body in group:
            variables = {}
            declarations = []
            head = _term_source(head, variables, declarations)
            calls = []
            impossible = False
            saw_cut = False
            for name, args in body:
                if name == "!":
                    calls.append((None, None))
                    saw_cut = True
                    continue
                expression = _term_source(args, variables, declarations)
                if (name, len(args)) == ("=", 2) and not any(_variable_names(args)):
                    if _unify(*_ground(args), {}) is None:
                        if saw_cut:
                            calls.append(("_no_match", "()"))
                        else:
                            impossible = True
                else:
                    calls.append((names.get((name, len(args)), "_no_match"), expression))
            if impossible:
                continue
            lines.extend(f"    {var} = _LogicVar()" for var in declarations)
            lines += [f"    next_0 = _unify(arguments, {head}, bindings)",
                      "    if next_0 is not None:"]
            indent = " " * 8
            cut_indents = []
            index = 0
            for callee, args in calls:
                if callee is None:
                    if not cut_indents or indent != cut_indents[-1]:
                        cut_indents.append(indent)
                    continue
                lines.append(f"{indent}for next_{index + 1} in {callee}({args}, next_{index}, depth - 1):")
                indent += " " * 4
                index += 1
            lines.append(f"{indent}yield next_{index}")
            # Returning at each cut's indentation discards only earlier choices
            # from this predicate, after the loops to its right are exhausted.
            lines.extend(f"{indent}return" for indent in reversed(cut_indents))
        if len(lines) == initial_length:
            lines.append("    yield from ()")
    entries = ", ".join(f"{key!r}: {name}" for key, name in names.items())
    lines += [
        f"_predicates = {{{entries}}}",
        "def _resolve(name, arguments, bindings, depth):",
        "    predicate = _predicates.get((name, len(arguments)), _no_match)",
        "    return predicate(arguments, bindings, depth)",
    ]
    return "\n".join(lines) + "\n"


def compile_horn(clauses):
    """Build run(goals, *, max_depth=32, limit=None).

    Resolution tries clauses and goals in supplied order. max_depth caps
    nested predicate calls on a proof path. limit caps returned proofs.
    """
    source = _horn_source(clauses) + (
        "def run(goals, *, max_depth=32, limit=None):\n"
        "    return _answers(_resolve, goals, max_depth, limit)\n"
    )
    return compile_residual("horn-clauses", source, _HORN_BINDINGS)


def _variable_names(term):
    if _variable(term):
        yield term
    elif isinstance(term, Guard):
        yield from _variable_names(term.goal)
    elif isinstance(term, (tuple, list)):
        for child in term:
            yield from _variable_names(child)


def _dcg_clause(rule):
    name, head = _goal(rule.head)
    parts = tuple(rule.body)
    used = set(_variable_names((head, parts)))

    def difference_variables():
        index = 0
        while True:
            candidate = f"?__difference_{index}"
            if candidate not in used:
                yield candidate
            index += 1

    fresh = difference_variables()
    incoming = position = next(fresh)
    body = []
    for part in parts:
        if isinstance(part, Guard):
            body.append(part.goal)
            continue
        remainder = next(fresh)
        if isinstance(part, str):
            body.append(("=", position, ("cons", Literal(part), remainder)))
        else:
            category, features = _goal(part)
            body.append((category, *features, position, remainder))
        position = remainder
    return Clause((name, *head, incoming, position), tuple(body))


def _word_list(words):
    result = ()
    for word in reversed(_words(words)):
        result = ("cons", Literal(word), result)
    return result


def compile_unification_grammar(rules, start, *, facts=()):
    """Build run(words, *features, max_depth=32, limit=None).

    The start category and arities are staged. Input words and requested
    feature values or variables arrive at runtime. Each answer consumes all
    input and projects named feature variables.
    """
    _goal((start,))
    clauses = tuple(map(_dcg_clause, rules))
    arities = tuple(sorted({len(clause.head) - 3 for clause in clauses
                            if clause.head[0] == start}))
    if not arities:
        raise ValueError(f"unknown unification grammar category {start!r}")
    source = _horn_source((*clauses, *facts)) + (
        "def run(words, *features, max_depth=32, limit=None):\n"
        f"    if len(features) not in {arities!r}:\n"
        "        raise ValueError('incorrect number of start features')\n"
        f"    goals = (({start!r}, *features, _word_list(words), ()),)\n"
        "    return _answers(_resolve, goals, max_depth, limit)\n"
    )
    return compile_residual("unification-grammar", source,
                            {**_HORN_BINDINGS, "_word_list": _word_list})


_WORLD_ARITIES = {"sub": 2, "ind": 2, "rel": 3, "val": 3}


def _ground(term):
    if _variable(term):
        raise ValueError("world facts must be ground")
    if isinstance(term, (list, tuple)):
        return tuple(map(_ground, term))
    return _atom(term)


def _fact(fact):
    name, arguments = _goal(fact)
    if name == "!":
        raise ValueError("cut cannot be asserted as a world fact")
    if name in _WORLD_ARITIES and len(arguments) != _WORLD_ARITIES[name]:
        raise ValueError(f"wrong arity for attached predicate {name!r}")
    return name, *_ground(arguments)


def _coordinates(term, path=()):
    pending = [(path, term)]
    while pending:
        path, current = pending.pop()
        if isinstance(current, tuple):
            yield path, ("tuple", len(current))
            pending.extend(((*path, index), current[index])
                           for index in reversed(range(len(current))))
        elif not isinstance(current, _LogicVar):
            yield path, (type(current).__name__, current)


def _same(left, right):
    return tuple(_coordinates(left)) == tuple(_coordinates(right))


def _derived(fact, by_name):
    predicate, *args = fact
    if predicate == "sub":
        child, parent = args
        for _, lower, upper in by_name["sub"]:
            if _same(parent, lower):
                yield "sub", child, upper
            if _same(child, upper):
                yield "sub", lower, parent
        for _, individual, category in by_name["ind"]:
            if _same(child, category):
                yield "ind", individual, parent
    elif predicate == "ind":
        individual, category = args
        for _, lower, upper in by_name["sub"]:
            if _same(category, lower):
                yield "ind", individual, upper
    elif predicate == "rel":
        relation, source_type, target_type = args
        for _, label, source, target in by_name["val"]:
            if _same(relation, label):
                yield "ind", source, source_type
                yield "ind", target, target_type
    elif predicate == "val":
        relation, source, target = args
        for _, label, source_type, target_type in by_name["rel"]:
            if _same(relation, label):
                yield "ind", source, source_type
                yield "ind", target, target_type


def _closure(seeds, attachments, max_facts):
    facts = []
    keys = set()
    by_name = defaultdict(list)

    def insert(fact):
        key = tuple(_coordinates(fact))
        if key not in keys:
            if len(facts) == max_facts:
                raise ValueError("world exceeds max_facts during attachment closure")
            keys.add(key)
            facts.append(fact)
            by_name[fact[0]].append(fact)

    for seed in seeds:
        insert(seed)
    for fact in facts:
        for extra in tuple(_derived(fact, by_name)):
            insert(extra)
        attachment = attachments.get(fact[0])
        if attachment is not None:
            callback_fact = _quote_fact(fact)
            for extra in attachment(callback_fact):
                insert(_fact(extra))
    return tuple(facts)


def _quote_fact(fact):
    """Let attachments copy any variable-looking literal into a derived fact."""
    def quote(term):
        if isinstance(term, tuple):
            return tuple(map(quote, term))
        return Literal(term) if _variable(term) else term

    return quote(fact)


def _world_facts(worlds, attachments, max_facts):
    closed = {}
    active = set()

    def visit(name):
        if name in active:
            raise ValueError(f"world inheritance cycle at {name!r}")
        if name in closed:
            return closed[name]
        if name not in worlds:
            raise ValueError(f"unknown parent world {name!r}")
        active.add(name)
        world = worlds[name]
        inherited = [fact for parent in world.parents for fact in visit(parent)]
        closed[name] = _closure((*inherited, *map(_fact, world.facts)), attachments, max_facts)
        active.remove(name)
        return closed[name]

    for name in worlds:
        if type(name) is not str or not name:
            raise ValueError("world names must be nonempty strings")
        visit(name)
    return closed


def _world_indices(worlds):
    indices = {}
    for world, facts in worlds.items():
        by_predicate = defaultdict(list)
        for name, *arguments in facts:
            by_predicate[name, len(arguments)].append(tuple(arguments))
        predicates = {}
        for key, rows in by_predicate.items():
            posting = defaultdict(list)
            for index, row in enumerate(rows):
                for coordinate in _coordinates(row):
                    posting[coordinate].append(index)
            predicates[key] = tuple(rows), dict(posting)
        indices[world] = predicates
    return indices


def _read_world(index, goals, limit):
    def resolve(name, arguments, bindings, depth):
        entry = index.get((name, len(arguments)))
        if entry is None:
            return
        rows, posting = entry

        bound_arguments = _map_term(arguments, lambda term: _walk(term, bindings), lambda atom: atom)
        buckets = (posting.get(coordinate, ()) for coordinate in
                   _coordinates(bound_arguments))
        matches = min(buckets, key=len)
        for row in matches:
            result = _unify(arguments, rows[row], bindings)
            if result is not None:
                yield result

    return _answers(resolve, goals, 0, limit)


def compile_worlds(worlds, *, attachments=None, max_facts=4096):
    """Build run(world, goals, *, limit=None) over a closed static fact base.

    Attachments execute at build time in each world, including for inherited
    facts. They receive an immutable fact whose variable-looking strings are
    represented by Literal. Derivation is bounded by max_facts per world.
    Runtime retrieval uses the smallest posting list for any bound structural
    coordinate, then unifies candidates to preserve repeated-variable tests.
    """
    _count(max_facts, "max_facts")
    attachments = dict(attachments or {})
    if not all(callable(attachment) for attachment in attachments.values()):
        raise ValueError("attached functions must be callable")
    closed = _world_facts(worlds, attachments, max_facts)
    indices = _world_indices(closed)
    source = (
        f"_index = {indices!r}\n"
        "def run(world, goals, *, limit=None):\n"
        "    return _read_world(_index[world], goals, limit)\n"
    )
    return compile_residual("indexed-worlds", source, {"_read_world": _read_world})
