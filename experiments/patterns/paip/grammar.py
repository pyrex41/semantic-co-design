"""Compiled phrase-structure grammars from PAIP chapters 2 and 19.

For generation, supply a mapping from category names to ordered sequences of
right-hand sides. Each right-hand side is a tuple of symbols. A symbol is a
category if it is declared on the left, and otherwise is a literal word. An
empty right-hand side denotes an empty phrase.

For parsing, supply PhraseRule objects. The same symbol convention applies.
A rule can compute its meaning from the meanings of its children and can
score a parse from the child Parse records. Higher scores rank first,
including the scores of all descendants. Right-hand sides must consume a
word or descend through an acyclic chain of single-category rules. Binary
and longer rules can be left-recursive. An optional sequence of open
categories supplies lexical alternatives when a word is absent from the
grammar. All complete parses are retained.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from math import isfinite
from random import Random

from .residual import compile_residual


def _symbol(name):
    if type(name) is not str or not name:
        raise ValueError("grammar symbols must be nonempty strings")
    return name


def _rhs(parts):
    if isinstance(parts, str):
        raise ValueError("a rule body must be a sequence of symbols")
    return tuple(map(_symbol, parts))


def _count(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _words(words):
    words = tuple(words.split() if isinstance(words, str) else words)
    if any(type(word) is not str for word in words):
        raise ValueError("words must be strings")
    return words


def _minimum_depth(rules):
    minimum = {}
    while True:
        previous = minimum.copy()
        for category, alternatives in rules.items():
            for rhs in alternatives:
                if any(part in rules and part not in minimum for part in rhs):
                    continue
                depth = 1 + max((minimum.get(part, 0) for part in rhs), default=0)
                minimum[category] = min(minimum.get(category, depth), depth)
        if minimum == previous:
            return minimum


def _tree_words(tree):
    if isinstance(tree, str):
        yield tree
    else:
        for child in tree[1:]:
            yield from _tree_words(child)


def _finish_generation(generate, depth, trees, limit, seed):
    _count(depth, "max_depth")
    if limit is not None:
        _count(limit, "limit")
    choose = tuple
    if seed is not None:
        random = Random(seed)

        def choose(choices):
            return (random.choice(choices),) if choices else ()

    results = generate(depth, choose)
    if limit is not None:
        results = islice(results, limit)
    return tuple(tree if trees else tuple(_tree_words(tree)) for tree in results)


def compile_generator(rules, start):
    """Build run(max_depth=8, *, trees=False, limit=None, seed=None).

    Depth counts category expansions on a branch. Results follow rule order.
    With a seed, choose a single derivation by sampling feasible alternatives
    at each category. A tree is (category, *children) with word leaves.
    """
    rules = {_symbol(category): tuple(map(_rhs, alternatives))
             for category, alternatives in rules.items()}
    if _symbol(start) not in rules:
        raise ValueError(f"unknown generation category {start!r}")
    names = {category: f"_generate_{i}" for i, category in enumerate(rules)}
    minimum = _minimum_depth(rules)
    lines = []
    for category, alternatives in rules.items():
        lines.append(f"def {names[category]}(depth, choose):")
        feasible = [(rhs, 1 + max((minimum.get(part, 0) for part in rhs), default=0))
                    for rhs in alternatives
                    if all(part not in rules or part in minimum for part in rhs)]
        if not feasible:
            lines.append("    yield from ()")
            continue
        choices = ", ".join(f"({i}, {depth})" for i, (_, depth) in enumerate(feasible))
        lines.append(f"    choices = tuple(i for i, need in ({choices},) if depth >= need)")
        lines.append("    for choice in choose(choices):")
        for index, (rhs, _) in enumerate(feasible):
            lines.append(f"        if choice == {index}:")
            indent = " " * 12
            children = []
            for i, part in enumerate(rhs):
                if part not in names:
                    children.append(repr(part))
                    continue
                child = f"child_{i}"
                children.append(child)
                lines.append(f"{indent}for {child} in {names[part]}(depth - 1, choose):")
                indent += " " * 4
            tree = ", ".join([repr(category), *children])
            lines.append(f"{indent}yield ({tree},)")
    lines += [
        "def run(max_depth=8, *, trees=False, limit=None, seed=None):",
        f"    return _finish_generation({names[start]}, max_depth, trees, limit, seed)",
    ]
    return compile_residual("grammar-generation", "\n".join(lines) + "\n",
                            {"_finish_generation": _finish_generation})


@dataclass(frozen=True)
class PhraseRule:
    lhs: str
    rhs: tuple
    meaning: object = None
    preference: object = 0


@dataclass(frozen=True)
class Parse:
    tree: object
    meaning: object
    score: int | float
    span: tuple[int, int]


def _terminal(word, start):
    return Parse(word, word, 0, (start, start + 1))


def _parse_node(category, children, start, end, meaning, preference):
    meanings = tuple(child.meaning for child in children)
    if meaning is not None:
        value = meaning(*meanings)
    else:
        value = meanings[0] if len(meanings) == 1 else (category, *meanings)
    if value is None:
        return None
    score = preference(*children) if callable(preference) else preference
    if score is None:
        score = 0
    if type(score) not in (int, float) or (type(score) is float and not isfinite(score)):
        raise ValueError("parse preference must be a finite number")
    try:
        total = score + sum(child.score for child in children)
    except OverflowError as error:
        raise ValueError("aggregate parse score must be finite") from error
    if type(total) is float and not isfinite(total):
        raise ValueError("aggregate parse score must be finite")
    return Parse((category, *(child.tree for child in children)), value, total, (start, end))


def _category_order(rules):
    dependencies = {rule.lhs: [] for rule in rules}
    for rule in rules:
        if len(rule.rhs) == 1 and rule.rhs[0] in dependencies:
            dependencies[rule.lhs].append(rule.rhs[0])
    result = []
    active = set()

    def visit(category):
        if category in active:
            raise ValueError(f"single-category parsing cycle at {category!r}")
        if category not in result:
            active.add(category)
            for child in dependencies[category]:
                visit(child)
            active.remove(category)
            result.append(category)

    for category in dependencies:
        visit(category)
    return result


def _phrase_lines(rule, categories, meaning, preference):
    lines = []
    indent = " " * 4

    def emit(line):
        lines.append(indent + line)

    emit(f"if end - start >= {len(rule.rhs)}:")
    indent += " " * 4
    left = "start"
    for i, part in enumerate(rule.rhs):
        remaining = len(rule.rhs) - i - 1
        right = "end" if remaining == 0 else f"position_{i}"
        if remaining and part in categories:
            emit(f"for {right} in range({left} + 1, end - {remaining} + 1):")
            indent += " " * 4
        if remaining and part not in categories:
            emit(f"{right} = {left} + 1")
        if part in categories:
            emit(f"for child_{i} in chart[{categories[part]}].get(({left}, {right}), ()):")
        else:
            emit(f"if {right} == {left} + 1 and tokens[{left}] == {part!r}:")
        indent += " " * 4
        if part not in categories:
            emit(f"child_{i} = _terminal(tokens[{left}], {left})")
        left = right
    children = ", ".join(f"child_{i}" for i in range(len(rule.rhs)))
    emit(f"node = _parse_node({rule.lhs!r}, ({children},), start, end, {meaning}, {preference})")
    emit("if node is not None:")
    lines.append(indent + "    yield node")
    return lines


def compile_preference_parser(rules, start, *, open_categories=()):
    """Build run(words), returning complete Parse records in preference order.

    A meaning callback receives child meanings. A result of None rejects the
    derivation. A preference callback receives child Parse records and adds
    a numeric preference to their combined score.
    """
    rules = [PhraseRule(_symbol(rule.lhs), _rhs(rule.rhs), rule.meaning, rule.preference)
             for rule in rules]
    if any(not rule.rhs for rule in rules):
        raise ValueError("phrase parsing rules must have a nonempty body")
    categories = {category: i for i, category in enumerate(_category_order(rules))}
    if _symbol(start) not in categories:
        raise ValueError(f"unknown parsing category {start!r}")
    open_categories = tuple(map(_symbol, open_categories))
    if any(category not in categories for category in open_categories):
        raise ValueError("open categories must be declared in the grammar")
    by_category = {category: [] for category in categories}
    bindings = {"_words": _words, "_parse_node": _parse_node, "_terminal": _terminal}
    known = tuple(sorted({part for rule in rules for part in rule.rhs if part not in categories}))
    lines = [f"_known = frozenset({known!r})"] if open_categories else []
    for i, rule in enumerate(rules):
        meaning = "None"
        if rule.meaning is not None:
            if not callable(rule.meaning):
                raise ValueError("a meaning callback must be callable")
            meaning = f"_meaning_{i}"
            bindings[meaning] = rule.meaning
        preference = f"_preference_{i}"
        if callable(rule.preference):
            bindings[preference] = rule.preference
        elif type(rule.preference) is int or (
            type(rule.preference) is float and isfinite(rule.preference)
        ):
            preference = repr(rule.preference)
        else:
            raise ValueError("parse preference must be a finite number or callable")
        by_category[rule.lhs].extend(_phrase_lines(rule, categories, meaning, preference))
    for category, index in categories.items():
        lines.append(f"def _parse_{index}(tokens, start, end, chart):")
        lines.extend(by_category[category])
        if category in open_categories:
            lines += ["    if end == start + 1 and tokens[start] not in _known:",
                      "        child = _terminal(tokens[start], start)",
                      f"        yield _parse_node({category!r}, (child,), start, end, None, 0)"]
    lines += [
        "def run(words):",
        "    tokens = _words(words)",
        f"    chart = tuple({{}} for _ in range({len(categories)}))",
        "    for width in range(1, len(tokens) + 1):",
        "        for start in range(len(tokens) - width + 1):",
        "            end = start + width",
    ]
    for index in categories.values():
        lines.append(f"            matches = tuple(_parse_{index}(tokens, start, end, chart))")
        lines.append(f"            if matches: chart[{index}][start, end] = matches")
    lines += [
        f"    complete = chart[{categories[start]}].get((0, len(tokens)), ())",
        "    return tuple(sorted(complete, key=lambda parse: parse.score, reverse=True))",
    ]
    return compile_residual("preference-parser", "\n".join(lines) + "\n", bindings)
