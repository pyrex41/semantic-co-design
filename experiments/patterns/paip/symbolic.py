"""Compile symbolic rewrite, infix and canonical algebra profiles (PAIP 8, 9, 15)."""

from fractions import Fraction
from functools import cache
from operator import add

from .residual import compile_residual
from .rules import RuleCompiler, _emit, _get_capture, _term, _variable


def _root(pattern):
    if isinstance(pattern, tuple) and pattern:
        head = pattern[0]
        if isinstance(head, str) and not (_variable(head) or head == "_"):
            return head
    return None


def compile_rewriter(rules, *, predicates=None, operations=None, budget=500):
    """Compile ordered (pattern, template) rewrites with fixed operator dispatch.

    Runtime terms use prefix tuples or lists. Children normalize first. Optional
    operator hooks receive the normalized argument tuple and return a replacement
    or None. Hooks and predicates must be pure. Equivalent subtrees share work per
    call. A profile exhausting its rewrite budget raises ValueError.
    """
    if type(budget) is not int or budget < 1:
        raise ValueError("budget must be a positive integer")
    rules = [(_term(pattern), _term(template)) for pattern, template in rules]
    operations = dict(operations or {})
    if not all(type(op) is str and callable(function) for op, function in operations.items()):
        raise ValueError("operations must map operator names to callables")
    compiler = RuleCompiler(predicates, {"capture": _get_capture, "cache": cache})
    roots = sorted({name for pattern, _ in rules if (name := _root(pattern)) is not None} | set(operations))
    branches = []
    for root in roots:
        clauses = []
        if root in operations:
            symbol = f"operation_{len(compiler.bindings)}"
            compiler.bindings[symbol] = operations[root]
            clauses.extend([f"result = {symbol}(value[1:])", "if result is not None: return term(result)"])
        clauses.extend(compiler.clauses(((p, t) for p, t in rules if _root(p) in (None, root)), "value"))
        branches.extend([f"if head == {root!r}:", *("    " + line for line in clauses), "    return value"])
    branches.extend(compiler.clauses(((p, t) for p, t in rules if _root(p) is None), "value"))
    rewrite = _emit([
        "if not isinstance(value, tuple) or not value: return value",
        "head = value[0]",
        *branches,
        "return value",
    ])
    return compiler.compile("rewrite", f"def rewrite(value):\n{rewrite}\n" + f"""
def run(value):
    remaining = {budget!r}
    active = set()

    @cache
    def normalize(value):
        nonlocal remaining
        trail = []
        try:
            while isinstance(value, tuple) and value:
                if value in active:
                    raise ValueError('cyclic rewrite')
                active.add(value)
                trail.append(value)
                current = (value[0], *(normalize(x) for x in value[1:]))
                result = rewrite(current)
                if result == current:
                    return current
                if not remaining:
                    raise ValueError('rewrite budget exhausted')
                remaining -= 1
                value = result
            return value
        finally:
            for item in trail:
                active.remove(item)

    return normalize(term(value))
""")


def _factors(expression):
    powers = {}
    scalar = Fraction(1)

    def collect(value, exponent):
        nonlocal scalar
        if type(value) is int:
            scalar *= Fraction(value) ** exponent
        elif isinstance(value, tuple) and len(value) == 3 and value[0] in ("*", "/"):
            collect(value[1], exponent)
            collect(value[2], -exponent if value[0] == "/" else exponent)
        elif isinstance(value, tuple) and len(value) == 3 and value[0] == "^" and type(value[2]) is int:
            collect(value[1], exponent * value[2])
        else:
            powers[value] = powers.get(value, 0) + exponent

    collect(expression, 1)
    return scalar, {value: power for value, power in powers.items() if power}


def _operand_product(left, right):
    if left == 0 or right == 0:
        return 0
    if left == 1 or right == 1:
        return right if left == 1 else left
    return "*", left, right


def _operand_power(value, exponent):
    if exponent in (0, 1):
        return value if exponent else 1
    return "^", value, exponent


def _operand_number(value):
    return value.numerator if value.denominator == 1 else ("/", value.numerator, value.denominator)


def _unfactor(scalar, powers):
    expression = _operand_number(scalar)
    for base, exponent in powers.items():
        expression = _operand_product(expression, _operand_power(base, exponent))
    return expression


def compile_integrator(variable, table=()):
    """Stage symbolic integration for one variable and an antiderivative table.

    ``table`` holds ordered pattern/template pairs for unary functions of an
    argument. Templates express the antiderivative with respect to that argument.
    The residual handles constants, sums, signs, integer powers and substitution
    when a nonzero monomial derivative divides the integrand. Unsupported terms
    are returned as ``('integral', expression, variable)``.
    """
    if type(variable) is not str or not variable:
        raise ValueError("expected an integration variable")
    canonical = compile_canonical((variable,)).run
    compiler = RuleCompiler(bindings={"capture": _get_capture, "factor": _factors,
                                      "unfactor": _unfactor, "canonical": canonical,
                                      "prod": _operand_product, "pow_expr": _operand_power,
                                      "number": _operand_number, "Q": Fraction})
    clauses = _emit([*compiler.clauses(table, "value"), "return None"])
    return compiler.compile("integration", f"def integral_table(value):\n{clauses}\n" + f"""
def depends(value):
    return value == {variable!r} or isinstance(value, tuple) and any(map(depends, value[1:]))

def substitute(scalar, powers, base, needed):
    try:
        derivative = canonical(('d', base, {variable!r}))
    except ValueError:
        return None
    if len(derivative) != 1:
        return None
    ((degree,), coefficient), = derivative
    quotient = powers.copy()
    for factor, exponent in needed:
        quotient[factor] = quotient.get(factor, 0) - exponent
    if degree:
        quotient[{variable!r}] = quotient.get({variable!r}, 0) - degree
    if any(exponent and depends(factor) for factor, exponent in quotient.items()):
        return None
    return unfactor(scalar / coefficient, quotient)

def integrate(value):
    if not depends(value):
        return prod(value, {variable!r})
    if isinstance(value, tuple) and value[0] in ('+', '-') and len(value) in (2, 3):
        return (value[0], *(integrate(item) for item in value[1:]))
    scalar, powers = factor(value)
    if not scalar:
        return 0
    for base, power in powers.items():
        scale = substitute(scalar, powers, base, ((base, power),))
        if scale is not None:
            if power == -1:
                return prod(scale, ('log', base))
            return prod(prod(scale, number(Q(1, power + 1))), pow_expr(base, power + 1))
        if power == 1 and isinstance(base, tuple) and len(base) == 2:
            response = integral_table(base)
            if response is not None:
                scale = substitute(scalar, powers, base[1], ((base, 1),))
                if scale is not None:
                    return prod(scale, response)
    constants = {{base: power for base, power in powers.items() if not depends(base)}}
    variables = {{base: power for base, power in powers.items() if depends(base)}}
    if not variables:
        return prod(unfactor(scalar, constants), {variable!r})
    return prod(unfactor(scalar, constants), ('integral', unfactor(Q(1), variables), {variable!r}))

def run(expression):
    return integrate(term(expression))
""")


def compile_infix(operators):
    """Compile infix precedence and associativity into direct token dispatch.

    Operators map input tokens to ``(precedence, 'left'|'right', prefix_name)``.
    The residual consumes a sequence of integer or string tokens with ``(`` and
    ``)`` grouping. Each unreserved atom is an operand.
    """
    lines = ["def operator(token):"]
    for token, (precedence, associativity, prefix) in operators.items():
        if type(token) is not str or token in ("(", ")") or type(prefix) is not str:
            raise ValueError("expected distinct operator name and string prefix name")
        if type(precedence) is not int or precedence < 0 or associativity not in ("left", "right"):
            raise ValueError("expected nonnegative precedence and left/right associativity")
        lines.append(f"    if token == {token!r}: return {precedence}, {precedence + (associativity == 'left')}, {prefix!r}")
    lines.append("    return None")
    return compile_residual("infix", "\n".join(lines) + """

def run(tokens):
    tokens = tuple(tokens)
    position = 0

    def atom():
        nonlocal position
        if position == len(tokens):
            raise ValueError('expected operand')
        value = tokens[position]
        position += 1
        if value == '(':
            value = expression(0)
            if position == len(tokens) or tokens[position] != ')':
                raise ValueError('unclosed parenthesis')
            position += 1
        elif type(value) not in (int, str) or value == ')' or operator(value) is not None:
            raise ValueError('expected operand')
        return value

    def expression(minimum):
        nonlocal position
        left = atom()
        while position < len(tokens):
            spec = operator(tokens[position])
            if spec is None or spec[0] < minimum:
                break
            position += 1
            left = (spec[2], left, expression(spec[1]))
        return left

    result = expression(0)
    if position != len(tokens):
        raise ValueError('unexpected trailing input')
    return result
""")


def _scale(poly, coefficient):
    return {power: factor * coefficient for power, factor in poly.items() if coefficient}


def _poly_add(left, right):
    result = left.copy()
    for power, coefficient in right.items():
        result[power] = left.get(power, 0) + coefficient
        if not result[power]:
            del result[power]
    return result


def _poly_mul(left, right):
    result = {}
    for lpower, lcoef in left.items():
        for rpower, rcoef in right.items():
            power = tuple(map(add, lpower, rpower))
            result[power] = result.get(power, 0) + lcoef * rcoef
    return {power: coefficient for power, coefficient in result.items() if coefficient}


def _poly_derivative(poly, axis):
    return {power[:axis] + (power[axis] - 1,) + power[axis + 1:]: coefficient * power[axis]
            for power, coefficient in poly.items() if power[axis]}


def _divmod_poly(left, right):
    quotient = {}
    leading = max(right)
    remainder = left
    while remainder and max(remainder) >= leading:
        power = max(remainder)
        term = {(power[0] - leading[0],): remainder[power] / right[leading]}
        quotient.update(term)
        remainder = _poly_add(remainder, _scale(_poly_mul(term, right), -1))
    return quotient, remainder


def _reduced(left, right):
    if not right:
        raise ZeroDivisionError("zero polynomial denominator")
    a, b = left, right
    while b:
        a, b = b, _divmod_poly(a, b)[1]
    denominator = _divmod_poly(right, a)[0]
    factor = 1 / denominator[max(denominator)]
    return (_scale(_divmod_poly(left, a)[0], factor), _scale(denominator, factor))


def _rational_add(left, right):
    ln, ld = left
    rn, rd = right
    return _reduced(_poly_add(_poly_mul(ln, rd), _poly_mul(rn, ld)), _poly_mul(ld, rd))


def _rational_mul(left, right):
    return _reduced(_poly_mul(left[0], right[0]), _poly_mul(left[1], right[1]))


def _rational_neg(value):
    return (_scale(value[0], -1), value[1])


def _rational_div(left, right):
    return _rational_mul(left, (right[1], right[0]))


def _rational_derivative(value, axis):
    n, d = value
    numerator = _poly_add(_poly_mul(_poly_derivative(n, axis), d), _scale(_poly_mul(n, _poly_derivative(d, axis)), -1))
    return _reduced(numerator, _poly_mul(d, d))


def _power(value, exponent, one, multiply, reciprocal=None):
    if type(exponent) is not int or (exponent < 0 and reciprocal is None):
        raise ValueError("expected an integer exponent in the supported domain")
    if exponent < 0:
        value, exponent = reciprocal(one, value), -exponent
    result = one
    while exponent:
        if exponent & 1:
            result = multiply(result, value)
        exponent >>= 1
        if exponent:
            value = multiply(value, value)
    return result


_DEFAULT_OPERATORS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "pow": "^", "diff": "d"}


def compile_canonical(variables, *, rational=False, operators=None):
    """Compile canonical prefix algebra with exact coefficients.

    Output polynomials are sorted ``(exponent_tuple, Fraction)`` pairs. Rational
    mode accepts one variable and emits a reduced numerator and monic denominator.
    The latter uses formal rational-function equivalence, which cancels removable
    singularities. Expressions are integer/Fraction leaves and binary prefix
    operators ``+ - * / ^ d``. The right operand of ``d`` is a variable name.
    An optional operator mapping changes the syntax with the same arithmetic.
    """
    variables = tuple(variables)
    if not variables or not all(type(v) is str for v in variables) or len(set(variables)) != len(variables):
        raise ValueError("variables must be distinct names")
    if rational and len(variables) != 1:
        raise ValueError("rational mode takes one variable")
    operators = dict(_DEFAULT_OPERATORS if operators is None else operators)
    if not operators.keys() <= _DEFAULT_OPERATORS.keys() or not all(type(op) is str for op in operators.values()):
        raise ValueError("expected known canonical operator names and string tokens")
    if len(set(operators.values())) != len(operators):
        raise ValueError("operator tokens must be distinct")
    zero_power = (0,) * len(variables)
    one = {zero_power: Fraction(1)}
    sum_poly, multiply = (_rational_add, _rational_mul) if rational else (_poly_add, _poly_mul)
    negate = _rational_neg if rational else lambda value: _scale(value, -1)
    bindings = {
        "Fraction": Fraction,
        "combine": sum_poly,
        "negate": negate,
        "multiply": multiply,
        "derivative": _rational_derivative if rational else _poly_derivative,
        "divide": _rational_div,
        "one": (one, one) if rational else one,
        "power": _power,
    }
    lines = ["def convert(expr):", "    if type(expr) in (int, Fraction):",
             f"        poly = {{{zero_power!r}: Fraction(expr)}} if expr else {{}}",
             "        return (poly, one[1])" if rational else "        return poly"]
    for index, variable in enumerate(variables):
        exp = tuple(int(i == index) for i in range(len(variables)))
        lines += [f"    if expr == {variable!r}:", f"        poly = {{{exp!r}: Fraction(1)}}",
                  "        return (poly, one[1])" if rational else "        return poly"]
    lines += ["    if not isinstance(expr, (tuple, list)) or len(expr) != 3:",
              "        raise ValueError(f'invalid expression {expr!r}')", "    op, left, right = expr"]
    operations = {"add": "combine(convert(left), convert(right))", "sub": "combine(convert(left), negate(convert(right)))",
                  "mul": "multiply(convert(left), convert(right))",
                  "pow": "power(convert(left), right, one, multiply" + (", divide)" if rational else ")"),
                  "div": "divide(convert(left), convert(right))"}
    for kind, token in operators.items():
        if kind == "diff":
            lines.append(f"    if op == {token!r}:")
            for i, variable in enumerate(variables):
                lines.append(f"        if right == {variable!r}: return derivative(convert(left), {i})")
            lines.append("        raise ValueError(f'unknown differentiation variable {right!r}')")
        elif kind != "div" or rational:
            lines.append(f"    if op == {token!r}: return {operations[kind]}")
    lines += ["    raise ValueError(f'unsupported canonical operator {op!r}')", "", "def run(expr):",
              "    value = convert(expr)",
              "    return tuple(tuple(sorted(poly.items())) for poly in value)" if rational else
              "    return tuple(sorted(value.items()))"]
    return compile_residual("canonical", "\n".join(lines) + "\n", bindings)
