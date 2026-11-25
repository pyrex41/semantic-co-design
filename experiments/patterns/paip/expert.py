"""Compile EMYCIN-style backward rules with exact certainty factors (PAIP 16)."""

from fractions import Fraction

from .residual import compile_residual
from .rules import _emit


def _confidence(value):
    if type(value) not in (int, float, Fraction):
        raise ValueError("certainty factor must be numeric")
    value = Fraction(str(value))
    if not -1 <= value <= 1:
        raise ValueError("certainty factor must lie in [-1, 1]")
    return value


def _coordinate(value):
    if type(value) not in (int, str):
        raise ValueError("fact values must be integers or strings")
    return value


def _answers(values):
    if values is None:
        return {}
    return {_coordinate(value): _confidence(cf) for value, cf in dict(values).items()}


def _combine(a, b):
    if a > 0 and b > 0:
        return a + b - a * b
    if a < 0 and b < 0:
        return a + b + a * b
    divisor = 1 - min(abs(a), abs(b))
    if not divisor:
        raise ValueError("contradictory fully certain evidence")
    return (a + b) / divisor


def compile_expert(parameters, rules, *, cutoff=Fraction(1, 5)):
    """Compile an ordered, acyclic, single-context certainty rule network.

    ``parameters`` maps names to ``'ask_first'`` or ``'rules_first'``. Each rule
    has ``(head, premises, weight)`` with one ``(parameter, value)`` head and a
    sequence of equality premises of that shape. A query supplies parameter
    names, optional answers already known, and an optional ask callback of
    ``(context, parameter, dependency_path) -> {value: confidence} | None``.
    The residual returns certainty factors for each requested parameter and
    caches consultation results only for that call. Known evidence supplies
    authoritative parameter answers. Fully contradictory certainty is invalid.
    """
    parameters = dict(parameters)
    if not parameters or not all(type(p) is str and kind in ("ask_first", "rules_first")
                                 for p, kind in parameters.items()):
        raise ValueError("expected named expert parameters and consultation policy")
    cutoff = _confidence(cutoff)
    if not 0 <= cutoff < 1:
        raise ValueError("cutoff must lie in [0, 1)")
    index = {p: i for i, p in enumerate(parameters)}
    by_parameter = {p: [] for p in parameters}

    def fact(pair):
        parameter, value = pair
        if type(parameter) is not str or parameter not in index:
            raise ValueError(f"unknown parameter {parameter!r}")
        return parameter, _coordinate(value)

    for head, premises, weight in rules:
        p, value = fact(head)
        by_parameter[p].append((value, tuple(map(fact, premises)), _confidence(weight)))

    visiting, visited = set(), set()

    def require_acyclic(p):
        if p in visited:
            return
        if p in visiting:
            raise ValueError(f"cyclic expert dependency at {p!r}")
        visiting.add(p)
        for _, premises, _ in by_parameter[p]:
            for dependency, _ in premises:
                require_acyclic(dependency)
        visiting.remove(p)
        visited.add(p)

    for parameter in parameters:
        require_acyclic(parameter)

    def fraction(value):
        return f"Q({value.numerator}, {value.denominator})"

    bindings = {"Q": Fraction, "answers": _answers, "combine": _combine}
    lines = [f"cutoff = {fraction(cutoff)}", f"false_cutoff = {fraction(cutoff - 1)}", "unknown = Q(0)"]
    for p, mode in parameters.items():
        slot = index[p]
        body = [f"if {slot} in known: return", f"path = path + ({p!r},)",
                f"known.add({slot})", f"current = data[{slot}]"]
        answer_call = f"consult(ask, context, {p!r}, path, current)"
        if mode == "ask_first":
            body.append(f"if {answer_call}: return")
        body.append("proved = False")
        for value, premises, weight in by_parameter[p]:
            conditions = " or ".join(
                f"data[{index[d]}].get({v!r}, unknown) < false_cutoff" for d, v in premises
            )
            outer = [f"if not ({conditions}):"] if conditions else []
            rule = ["support = Q(1)"]
            for d, v in premises:
                n = index[d]
                rule.extend(["if support > cutoff:", f"    infer_{n}(data, known, ask, context, path)",
                             f"    support = min(support, data[{n}].get({v!r}, unknown))"])
            rule.extend(["if support > cutoff:", "    proved = True",
                         f"    current[{value!r}] = combine(current.get({value!r}, unknown), "
                         f"support * {fraction(weight)})"])
            body.extend(outer + ["    " * bool(outer) + line for line in rule])
        if mode == "rules_first":
            body.append(f"if not proved: {answer_call}")
        lines.append(f"def infer_{slot}(data, known, ask, context, path):\n{_emit(body)}")
    names = ", ".join(f"{p!r}: {i}" for p, i in index.items())
    functions = ", ".join(f"infer_{i}" for i in index.values())
    lines += [f"parameters = {{{names}}}", f"inference = ({functions},)", """
def consult(ask, context, parameter, path, current):
    if ask is not None:
        current.update(answers(ask(context, parameter, path)))
    return bool(current)

def run(goals, evidence=None, ask=None, context=None):
    data = [{} for _ in inference]
    known = set()
    for key, values in dict(evidence or {}).items():
        slot = parameters[key]
        data[slot] = answers(values)
        known.add(slot)
    result = {}
    for goal in goals:
        slot = parameters[goal]
        inference[slot](data, known, ask, context, ())
        result[goal] = data[slot].copy()
    return result
"""]
    return compile_residual("expert", "\n\n".join(lines), bindings)
