"""Compile PAIP segment patterns and ordered translation rules (chapters 5–7).

Terms are integers, strings, tuples or lists. In a pattern, a string starting
with ``?`` binds a variable and ``_`` matches anything. ``('?lit', term)`` quotes
literal pattern syntax. ``('?is', pattern, predicate)`` and ``?and``, ``?or``,
``?not`` patterns test one value. A sequence may contain ``('?*', '?var')``,
``('?+', '?var')``, ``('??', '?var')`` and ``('?if', predicate, '?var', ...)``.
Stars and pluses bind tuples, shortest match first. Optionals try one value
before trying no binding. Predicates are trusted callables bound when compiling.
Templates use variables as references, with ``('?lit', term)`` for quotation.
"""

from fractions import Fraction

from .residual import compile_residual


_SEGMENTS = {"?*", "?+", "??", "?if"}


def _term(value):
    if isinstance(value, (tuple, list)):
        return tuple(_term(child) for child in value)
    if type(value) in (int, str):
        return value
    raise ValueError(f"expected a symbolic term, got {value!r}")


def _variable(name):
    return isinstance(name, str) and len(name) > 1 and name.startswith("?")


def _bind(env, name, value):
    if name in env:
        return env if value == env[name] else None
    return {**env, name: value}


def _guard(predicate, env, names):
    return all(name in env for name in names) and predicate(*(env[name] for name in names))


def _emit(lines, offset=1):
    return "\n".join("    " * offset + line for line in lines)


class RuleCompiler:
    """Lower each statically known pattern node to a fixed residual function."""

    def __init__(self, predicates=None, bindings=None):
        self.functions = []
        self.predicates = dict(predicates or {})
        if not all(callable(predicate) for predicate in self.predicates.values()):
            raise ValueError("pattern predicates must be callable")
        self.bindings = {"term": _term, "bind": _bind, "guard": _guard, **(bindings or {})}

    def add(self, args, lines):
        name = f"pattern_{len(self.functions)}"
        self.functions.append(f"def {name}({args}):\n{_emit(lines)}\n")
        return name

    def predicate(self, label):
        if not isinstance(label, str) or label not in self.predicates:
            raise ValueError(f"unknown pattern predicate {label!r}")
        name = f"predicate_{len(self.bindings)}"
        self.bindings[name] = self.predicates[label]
        return name

    def pattern(self, node):
        if isinstance(node, tuple):
            return self.form(node)
        if node == "_":
            return self.add("value, env", ["return env"])
        if _variable(node):
            return self.add("value, env", [f"return bind(env, {node!r}, value)"])
        return self.literal(node)

    def literal(self, node):
        return self.add("value, env", [f"return env if value == {node!r} else None"])

    def form(self, node):
        tag = node[0] if node and isinstance(node[0], str) else None
        if tag == "?lit":
            if len(node) != 2:
                raise ValueError("?lit takes a single term")
            return self.literal(node[1])
        if tag == "?is":
            if len(node) != 3:
                raise ValueError("?is takes a pattern and predicate name")
            check = self.pattern(node[1])
            predicate = self.predicate(node[2])
            return self.add("value, env", [f"env = {check}(value, env)",
                                           f"return env if env is not None and {predicate}(value) else None"])
        if tag in {"?and", "?or", "?not"}:
            checks = [self.pattern(child) for child in node[1:]]
            if tag == "?and":
                lines = [line for check in checks for line in
                         (f"env = {check}(value, env)", "if env is None: return None")]
                return self.add("value, env", [*lines, "return env"])
            lines = [line for check in checks for line in
                     (f"result = {check}(value, env)",
                      "if result is not None: return " + ("None" if tag == "?not" else "result"))]
            return self.add("value, env", [*lines, "return env" if tag == "?not" else "return None"])
        match_tail = self.sequence(node)
        return self.add("value, env", [f"return {match_tail}(value, 0, env) if isinstance(value, tuple) else None"])

    def sequence(self, nodes):
        if not nodes:
            return self.add("values, pos, env", ["return env if pos == len(values) else None"])
        node = nodes[0]
        tail = self.sequence(nodes[1:])
        if isinstance(node, tuple) and node and node[0] in _SEGMENTS:
            return self.segment(node, tail)
        head = self.pattern(node)
        return self.add("values, pos, env", ["if pos == len(values): return None",
                                             f"env = {head}(values[pos], env)",
                                             f"return {tail}(values, pos + 1, env) if env is not None else None"])

    def segment(self, node, tail):
        tag = node[0]
        if tag == "?if":
            if len(node) < 2 or not all(map(_variable, node[2:])):
                raise ValueError("?if takes a predicate name and bound variables")
            predicate = self.predicate(node[1])
            return self.add("values, pos, env", [f"if guard({predicate}, env, {node[2:]!r}):",
                                                 f"    return {tail}(values, pos, env)", "return None"])
        if len(node) != 2 or not _variable(node[1]):
            raise ValueError(f"{tag} requires a single binding variable")
        name = node[1]
        if tag == "??":
            return self.add("values, pos, env", ["if pos < len(values):",
                                                 f"    extended = bind(env, {name!r}, values[pos])",
                                                 "    if extended is not None:",
                                                 f"        result = {tail}(values, pos + 1, extended)",
                                                 "        if result is not None: return result",
                                                 f"return {tail}(values, pos, env)"])
        minimum = "pos + 1" if tag == "?+" else "pos"
        return self.add("values, pos, env", [f"for end in range({minimum}, len(values) + 1):",
                                             f"    extended = bind(env, {name!r}, values[pos:end])",
                                             "    if extended is not None:",
                                             f"        result = {tail}(values, end, extended)",
                                             "        if result is not None: return result",
                                             "return None"])

    def template(self, value, transform="capture"):
        if _variable(value):
            return f"{transform}(env, {value!r})"
        if isinstance(value, tuple):
            if value and value[0] == "?lit":
                if len(value) != 2:
                    raise ValueError("?lit takes a single term")
                return repr(value[1])
            return "(" + ", ".join(self.template(child, transform) for child in value) + ("," if value else "") + ")"
        return repr(value)

    def clauses(self, rows, value, transform="capture", *, prepare=None):
        for pattern, template in rows:
            match = self.pattern(_term(pattern))
            yield f"env = {match}({value}, {{}})"
            yield "if env is not None:"
            if prepare is not None:
                yield f"    env = {prepare}(env)"
            yield f"    return {self.template(_term(template), transform)}"

    def compile(self, name, body):
        return compile_residual(name, "\n".join([*self.functions, body]), self.bindings)


def compile_matcher(pattern, *, predicates=None):
    """The residual returns bindings on success and None on failure."""
    compiler = RuleCompiler(predicates)
    match = compiler.pattern(_term(pattern))
    return compiler.compile("matcher", f"def run(value):\n    return {match}(term(value), {{}})\n")


def _get_capture(env, name):
    # PAIP's substitution leaves an unbound variable symbol in the result.
    return env.get(name, name)


def compile_translator(rules, *, predicates=None, transforms=None):
    """Compile ordered (pattern, template) pairs. The first match wins.

    A per-variable callable may reflect a captured speaker's perspective or
    perform another deliberate transformation of a captured value.
    """
    transforms = dict(transforms or {})
    if not all(_variable(name) and callable(fn) for name, fn in transforms.items()):
        raise ValueError("transforms must map binding variables to callables")

    def reflect(env):
        return {name: transforms[name](value) if name in transforms else value for name, value in env.items()}

    compiler = RuleCompiler(predicates, {"capture": _get_capture, "reflect": reflect})
    clauses = compiler.clauses(rules, "value", prepare="reflect" if transforms else None)
    body = _emit(["value = term(value)", *clauses, "return None"])
    return compiler.compile("translator", f"def run(value):\n{body}\n")


def _unknowns(expr, known):
    if isinstance(expr, tuple):
        return set().union(*(_unknowns(child, known) for child in expr[1:]))
    return {expr} if isinstance(expr, str) and expr not in known else set()


def _affine(expr, known, variable):
    if isinstance(expr, str):
        return (1, 0) if expr == variable else (0, known[expr])
    if type(expr) is int:
        return 0, Fraction(expr)
    if len(expr) != 3:
        raise ValueError(f"expected a binary arithmetic expression, got {expr!r}")
    op, left, right = expr
    a, b = _affine(left, known, variable)
    c, d = _affine(right, known, variable)
    if op == "+":
        return a + c, b + d
    if op == "-":
        return a - c, b - d
    if op == "*" and not (a and c):
        return a * d + c * b, b * d
    if op == "/" and not c and d:
        return a / d, b / d
    raise ValueError(f"unsupported affine operation {expr!r}")


def _solve(equations):
    known = {}
    pending = list(equations)
    while True:
        for i, equation in enumerate(pending):
            if not isinstance(equation, tuple) or len(equation) != 3 or equation[0] != "=":
                raise ValueError(f"expected an equation, got {equation!r}")
            _, left, right = equation
            unknowns = _unknowns(left, known) | _unknowns(right, known)
            if len(unknowns) > 1:
                continue
            variable = next(iter(unknowns), None)
            a, b = _affine(left, known, variable)
            c, d = _affine(right, known, variable)
            if a != c:
                known[variable] = Fraction(d - b, a - c)
            elif b != d:
                raise ValueError(f"inconsistent equation {equation!r}")
            elif unknowns:
                continue
            pending.pop(i)
            break
        else:
            return known, tuple(pending)


def compile_student(rules, *, noise=(), predicates=None):
    """Recursively translate word equations, then propagate single unknowns.

    Give ``run`` an iterable of token sequences. A terminal is a one-token
    variable or integer. Recursive captures must be shorter than their enclosing
    phrase. It returns a dict of exact solutions and unresolved equations.
    """
    noise = tuple(_term(word) for word in noise)
    compiler = RuleCompiler(predicates, {"solve": _solve})
    clauses = compiler.clauses(rules, "phrase", "translate_capture")
    body = _emit([
        "def translate_capture(env, name):",
        "    if name not in env: raise ValueError(f'unbound template variable {name}')",
        "    captured = env[name]",
        "    if not isinstance(captured, tuple): return captured",
        "    if len(captured) >= len(phrase): raise ValueError('non-decreasing translation')",
        "    return translate(captured)",
        *clauses,
        "if len(phrase) == 1: return phrase[0]",
        "raise ValueError(f'unrecognized phrase {phrase!r}')",
    ])
    return compiler.compile("student", f"def translate(phrase):\n{body}\n"
                            "def run(equations):\n"
                            f"    return solve(translate(tuple(word for word in term(eq) if word not in {noise!r}))\n"
                            "                 for eq in equations)\n")
