"""Waltz-style filtering and labeling search (PAIP chapter 17).

specialize_constraints(domains, constraints) takes an ordered mapping from
string names to ordered domains. A value is a string, integer, or tuple of
such values. A constraint is (left_name, right_name, allowed_pairs) over two
distinct variables. Multiple relations on one pair are intersected. Values
outside a variable's declared domain are invalid in a relation.

specialize_waltz(vertices) builds those relations from an ordered mapping of
vertex names to (junction_type, ordered_neighbors). An L has two neighbors
and Y, T, and W have three. Every connection must appear at both endpoints.
Junction labels follow PAIP's order of incident lines, with opposite
endpoint labels L and R, and equal endpoint labels for + and -.

Both residuals take an optional mapping of variable names to allowed runtime
values. They return a ConstraintResult with the domains after propagation
and all consistent assignments. Both are ordered by the static variable and
value order. A propagation contradiction gives domains=None. A nonempty
fixed point can still have no solution.
"""

from collections import deque
from dataclasses import dataclass
from textwrap import dedent

from .residual import compile_residual


Label = str | int | tuple["Label", ...]
Domain = tuple[tuple[str, tuple[Label, ...]], ...]
Assignment = tuple[tuple[str, Label], ...]


@dataclass(frozen=True)
class ConstraintResult:
    domains: Domain | None
    solutions: tuple[Assignment, ...]


def _require_label(value):
    if type(value) is tuple:
        for item in value:
            _require_label(item)
    elif type(value) not in (str, int):
        raise ValueError("labels must be strings, integers or tuples of labels")


def specialize_constraints(domains, constraints):
    variables = tuple(domains)
    if any(type(name) is not str or not name for name in variables):
        raise ValueError("variable names must be nonempty strings")
    labels = tuple(tuple(domains[name]) for name in variables)
    positions = {name: index for index, name in enumerate(variables)}
    bits = []
    for values in labels:
        for value in values:
            _require_label(value)
        indexes = {value: 1 << index for index, value in enumerate(values)}
        if len(indexes) != len(values):
            raise ValueError("domain labels must be unique")
        bits.append(indexes)

    relations = {}
    for left, right, pairs in constraints:
        if left not in positions or right not in positions or left == right:
            raise ValueError("relations require two distinct declared variables")
        lpos, rpos = positions[left], positions[right]
        normalized = set()
        for a, b in pairs:
            _require_label(a)
            _require_label(b)
            if a not in bits[lpos] or b not in bits[rpos]:
                raise ValueError(f"relation value outside domain of {left!r} or {right!r}")
            normalized.add((a, b) if lpos < rpos else (b, a))
        key = (min(lpos, rpos), max(lpos, rpos))
        if key in relations:
            relations[key].intersection_update(normalized)
        else:
            relations[key] = normalized

    lines = [
        f"VARIABLES = {variables!r}",
        f"POSITIONS = {positions!r}",
        f"LABELS = {labels!r}",
        f"LABEL_BITS = {tuple(bits)!r}",
        f"FULL = {tuple((1 << len(values)) - 1 for values in labels)!r}",
    ]
    incoming = [[] for _ in variables]
    arcs = []
    for (left, right), allowed in relations.items():
        directions = ((left, right, allowed),
                      (right, left, {(b, a) for a, b in allowed}))
        for target, neighbor, pairs in directions:
            index = len(arcs)
            incoming[neighbor].append(index)
            arcs.append((target, f"_revise_{index}"))
            supports = {}
            for a, b in pairs:
                supports[a] = supports.get(a, 0) | bits[neighbor][b]
            clauses = [
                f"({bit} if adjacent & {supports[a]} else 0)"
                for a, bit in bits[target].items() if a in supports
            ]
            lines.extend([
                "",
                f"def _revise_{index}(state):",
                f"    adjacent = state[{neighbor}]",
                "    supported = (" + " |\n                 ".join(clauses or ["0"]) + ")",
                f"    return state[{target}] & supported",
            ])

    entries = ", ".join(f"({target}, {name})" for target, name in arcs)
    lines.extend([
        "",
        f"ARCS = ({entries},)" if arcs else "ARCS = ()",
        f"INCOMING = {tuple(tuple(items) for items in incoming)!r}",
        dedent(_SOLVER).strip("\n"),
    ])
    return compile_residual(
        "constraint_search", "\n".join(lines) + "\n",
        {"ConstraintResult": ConstraintResult, "deque": deque},
    )


_SOLVER = """

def _propagate(state, initial):
    pending = deque(initial)
    queued = set(pending)
    while pending:
        arc = pending.popleft()
        queued.remove(arc)
        target, revise = ARCS[arc]
        narrowed = revise(state)
        if not narrowed:
            return False
        if narrowed != state[target]:
            state[target] = narrowed
            for dependent in INCOMING[target]:
                if dependent not in queued:
                    pending.append(dependent)
                    queued.add(dependent)
    return True


def _solutions(state):
    for index, mask in enumerate(state):
        if mask.bit_count() > 1:
            for value in range(len(LABELS[index])):
                bit = 1 << value
                if mask & bit:
                    candidate = state.copy()
                    candidate[index] = bit
                    if _propagate(candidate, INCOMING[index]):
                        yield from _solutions(candidate)
            return
    yield tuple((name, values[mask.bit_length() - 1])
                for name, values, mask in zip(VARIABLES, LABELS, state))


def run(restrictions=None):
    state = list(FULL)
    if restrictions is not None:
        for variable, allowed in restrictions.items():
            if variable not in POSITIONS:
                raise ValueError(f"unknown variable {variable!r}")
            index = POSITIONS[variable]
            mask = 0
            for value in allowed:
                if value not in LABEL_BITS[index]:
                    raise ValueError(f"{value!r} is outside the domain of {variable!r}")
                mask |= LABEL_BITS[index][value]
            state[index] &= mask
    if not all(state) or not _propagate(state, range(len(ARCS))):
        return ConstraintResult(None, ())
    domains = tuple(
        (name, tuple(value for index, value in enumerate(values) if mask & (1 << index)))
        for name, values, mask in zip(VARIABLES, LABELS, state)
    )
    return ConstraintResult(domains, tuple(_solutions(state)))
"""


_JUNCTION_LABELS = {
    "L": (("R", "L"), ("L", "R"), ("+", "R"), ("L", "+"), ("-", "L"), ("R", "-")),
    "Y": (("+", "+", "+"), ("-", "-", "-"), ("L", "R", "-"),
          ("-", "L", "R"), ("R", "-", "L")),
    "T": (("R", "L", "+"), ("R", "L", "-"), ("R", "L", "L"), ("R", "L", "R")),
    "W": (("L", "R", "+"), ("-", "-", "+"), ("+", "+", "-")),
}


def specialize_waltz(vertices):
    domains = {}
    neighbors = {}
    for name, (junction, adjacent) in vertices.items():
        if junction not in _JUNCTION_LABELS:
            raise ValueError(f"unknown junction type {junction!r}")
        domain = _JUNCTION_LABELS[junction]
        edges = tuple(adjacent)
        if len(edges) != len(domain[0]) or len(set(edges)) != len(edges) or name in edges:
            raise ValueError(f"invalid neighbors for vertex {name!r}")
        domains[name] = domain
        neighbors[name] = edges

    reverse = {"L": "R", "R": "L", "+": "+", "-": "-"}
    constraints = []
    for left, adjacent in neighbors.items():
        for lpos, right in enumerate(adjacent):
            if right not in neighbors or left not in neighbors[right]:
                raise ValueError(f"edge {left!r}–{right!r} must exist at both endpoints")
            rpos = neighbors[right].index(left)
            pairs = [(a, b) for a in domains[left] for b in domains[right]
                     if reverse[a[lpos]] == b[rpos]]
            if left < right:
                constraints.append((left, right, pairs))
    return specialize_constraints(domains, constraints)
