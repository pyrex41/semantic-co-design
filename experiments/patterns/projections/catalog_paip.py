"""Encode observations from the existing PAIP core compilers for parity checks."""

from fractions import Fraction
from math import lcm

from ..paip.edge import compile_edge_table
from ..paip.expert import compile_expert
from ..paip.rules import compile_matcher, compile_student, compile_translator
from ..paip.scheme import compile_scheme
from ..paip.symbolic import compile_canonical, compile_infix, compile_integrator, compile_rewriter
from .check import require


def segment(args, config, changed):
    variable = ("?+" if changed else "?*", "?row")
    result = compile_matcher((variable, config[0], variable)).run(args)
    return -1 if result is None else len(result["?row"])


def translator(args, config, changed):
    send, literal, reflection = config
    rules = [((send, "?what"), ("generic", "?what", "?what")),
             ((send, ("?lit", literal)), ("specific", literal))]
    transformed = []

    def reflect(value):
        transformed.append(value)
        return reflection - value

    if changed:
        rules.reverse()
    result = compile_translator(rules, transforms={"?what": reflect}).run(args)
    if result is None:
        require(not transformed, "PAIP translator transformed an unmatched capture")
        return -1
    if result[0] == "specific":
        require(not transformed, "PAIP translator transformed a capture from an unselected rule")
        return 2000 + result[1]
    _, first, second = result
    require(transformed == args[1:2], "PAIP translator did not reflect exactly one capture")
    return 1000 + 10 * first + second


def student(args, config, changed):
    lexicon = dict(zip(config, ("y", "plus", "x", "equals", "twice", "the"), strict=True))
    equal = ((("?+", "?left"), "equals", ("?+", "?right")), ("=", "?left", "?right"))
    plus = ((("?+", "?left"), "plus", ("?+", "?right")), ("+", "?left", "?right"))
    twice = (("twice", ("?+", "?left")), ("*", 3 if changed else 2, "?left"))
    solver = compile_student((equal, plus, twice), noise=("the",))

    def decode(sentence):
        *words, number = sentence
        return tuple(lexicon.get(word, word) for word in words) + (number,)

    sentences = [decode(args[:5])]
    if len(args) > 5:
        sentences.append(decode(args[5:]))
    try:
        values, pending = solver.run(sentences)
    except ValueError as error:
        if not str(error).startswith("unrecognized phrase"):
            raise
        return -1
    if pending:
        require(not values and pending == (("=", ("+", "y", "x"), args[4]),),
                f"PAIP student produced an unexpected unresolved system: {values}, {pending}")
        return -2
    x, y = values["x"], values["y"]
    denominator = lcm(x.denominator, y.denominator)
    return int(denominator * (10000 * x + 10 * y + 1))


def infix(args, config, changed):
    names = dict(zip(config, ("+", "*", "^"), strict=True))
    digits = {name: digit for digit, name in names.items()} | {"sum": 15}
    profile = {"+": (3, "left", "sum") if changed else (1, "left", "+"),
               "*": (2, "left", "*"),
               "^": (4, "left", "^") if changed else (3, "right", "^")}
    result = compile_infix(profile).run(tuple(names.get(token, token) for token in args))

    def prefix(term):
        if isinstance(term, int):
            yield term
        else:
            operator, left, right = term
            yield digits[operator]
            yield from prefix(left)
            yield from prefix(right)

    packed = 0
    for digit in prefix(result):
        packed = 16 * packed + digit
    return packed


def rewriting(args, config, changed):
    pick, same, sum_tag, specific = config
    rules = [(("pick", "?x"), ("left", "?x")), (("pick", specific), ("one",)),
             (("same", "?x", "?x"), "?x")]
    if changed:
        rules.reverse()
    rewriter = compile_rewriter(rules, operations={"+": sum})

    def child(tag, left, right):
        return ("+", left, right) if tag == sum_tag else left

    lhs, rhs = child(*args[1:4]), child(*args[4:7])
    root = {pick: "pick", same: "same"}[args[0]]
    term = (root, lhs) if root == "pick" else (root, lhs, rhs)
    result = rewriter.run(term)
    if isinstance(result, int):
        return result
    if result[0] == "left":
        return 100 + result[1]
    if result == ("one",):
        return 300
    operator, left, right = result
    require(operator == "same", f"unexpected PAIP rewrite result: {result}")
    return 1000 + 20 * left + right


def _factors(term):
    """Flatten multiplication for the observation encoding."""
    if isinstance(term, tuple) and term[0] == "*" and len(term) == 3:
        yield from _factors(term[1])
        yield from _factors(term[2])
    else:
        yield term


def _scaled_term(term):
    scale = Fraction(1)
    terms = []
    for item in _factors(term):
        if isinstance(item, int):
            scale *= item
        elif isinstance(item, tuple) and item[0] == "/" and len(item) == 3:
            scale *= Fraction(item[1], item[2])
        elif isinstance(item, tuple) and item[0] == "-" and len(item) == 2:
            scale *= -1
            terms.append(item[1])
        else:
            terms.append(item)
    return scale, terms


def _x_power(power):
    if power == 0:
        return 1
    return "x" if power == 1 else ("^", "x", power)


def _exponent(term):
    if term == "x":
        return 1
    require(isinstance(term, tuple) and len(term) == 3 and term[:2] == ("^", "x"),
            f"expected a power of x, got {term}")
    return term[2]


def _integer_coefficient(value):
    require(value.denominator == 1, f"expected integral encoded coefficient, got {value}")
    return value.numerator


def _integral_observation(result, functions):
    scale, terms = _scaled_term(result)
    term, = terms
    if isinstance(term, tuple) and term[0] == "log":
        require(term == ("log", "x"), f"unexpected logarithm: {term}")
        return 200000 + _integer_coefficient(scale)
    if term == "x" or term[0] == "^":
        power = _exponent(term)
        return 100000 + 100 * _integer_coefficient(scale * power) + power
    if term[0] == "integral":
        _, integrand, variable = term
        require(variable == "x", f"unexpected integration variable: {variable}")
        factor = 0
        for item in _factors(integrand):
            if isinstance(item, tuple) and item[0] in functions:
                function, operand = item
            else:
                factor = _exponent(item)
        return 600000 + 1000 * _integer_coefficient(scale) + 100 * (factor + 1) + 10 * functions[function] + _exponent(operand)
    function, operand = term
    power = _exponent(operand)
    tag = {"sin": 400000, "cos": 500000}[function]
    return tag + 100 * (10 + _integer_coefficient(scale * power)) + 11 * power


def integration(args, config, changed):
    bare, cosine, sine = config
    coefficient, factor, function, power = args
    table = [(("sin", "?u"), ("-", ("cos", "?u")))] if changed else [(("cos", "?u"), ("sin", "?u"))]
    variables = _x_power(factor)
    if function != bare:
        variables = ("*", variables, ({cosine: "cos", sine: "sin"}[function], _x_power(power)))
    result = compile_integrator("x", table).run(("*", coefficient, variables))
    return _integral_observation(result, {"cos": cosine, "sin": sine})


def canonical(args, config, changed):
    operation, x, y, *coefficients = args

    def polynomial(values):
        a00, a01, a10, a11 = values
        return ("+", ("+", a00, ("*", a01, "y")),
                ("+", ("*", a10, "x"), ("*", a11, ("*", "x", "y"))))

    expr = ("*", polynomial(coefficients[:4]), polynomial(coefficients[4:]))
    if operation == config[0]:
        expr = ("d", expr, "y" if changed else "x")
    result = compile_canonical(("x", "y")).run(expr)
    return dict(result).get((x, y), 0)


def expert(args, config, changed):
    cutoff, strict_cutoff, repair, attention, positive, negative = (Fraction(n, 100) for n in config)
    hot, manual = (Fraction(n, 100) for n in args)
    parameters = {name: "rules_first" for name in ("repair", "attention", "unsafe")}
    parameters.update({"hot": "ask_first", "manual": "ask_first"})
    goals = (("repair", 1), ("attention", 1), ("unsafe", 1))
    hot_fact, manual_fact = ("hot", 1), ("manual", 1)
    rules = [(goals[0], (hot_fact,), repair),
             (goals[0], (), positive),
             (goals[0], (), negative),
             (goals[1], (hot_fact,), attention),
             (goals[2], (hot_fact, manual_fact), 1)]
    consulted = []

    def ask(_context, parameter, _path):
        evidence = {"hot": hot, "manual": manual}
        if parameter in evidence:
            consulted.append(parameter)
            return {1: evidence[parameter]}
        return None

    residual = compile_expert(parameters, rules, cutoff=strict_cutoff if changed else cutoff)
    findings = residual.run([name for name, _ in goals], ask=ask)
    require(consulted.count("hot") == 1, f"PAIP expert shared premise consultation: {consulted}")
    repair, attention, unsafe = (int(100 * findings[name].get(value, 0)) for name, value in goals)
    return 1000000 * repair + 1000 * attention + unsafe


def edge(args, config, changed):
    windows = ((1, 0), (7, 6)) if changed else ((0, 1), (6, 7))

    def score(pattern):
        left, right = pattern
        return config[3 * left + right]

    cells = ".XO"
    player, *board = args
    rows = ["".join(cells[cell] for cell in board[start:start + 3]) for start in range(0, 9, 3)]
    return compile_edge_table(3, windows, score).run(rows, cells[player])


def scheme(args, config, changed):
    mode, _, _ = args
    increment = 2 if changed else 1
    resume = ("let", (("saved", False), ("n", 0)),
              ("let", (("value", ("call/cc", ("lambda", ("k",), ("set!", "saved", "k"), "initial"))),),
               ("if", ("<", "n", "count"),
                ("begin", ("set!", "n", ("+", "n", 1)), ("saved", ("+", "value", increment))),
                ("list", "n", "value"))))
    tail = ("letrec", (("loop", ("lambda", ("n", "acc"),
                                  ("if", ("=", "n", 0), "acc",
                                   ("loop", ("-", "n", 1), ("+", "acc", ("*", increment, "n")))))),),
            ("loop", "count", "initial"))
    escape = ("+", config[0], ("call/cc", ("lambda", ("exit",), ("+", 100, ("exit", "initial")))))
    program = ("if", ("=", "mode", 0), resume, ("if", ("=", "mode", 1), tail, escape))
    result = compile_scheme(program, parameters=("mode", "count", "initial")).run(*args)
    if mode == 0:
        counter, value = result
        return 100 * counter + value
    return result


CORE_ORACLES = {
    "paip-segment-matcher": segment,
    "paip-ordered-translator": translator,
    "paip-student": student,
    "paip-infix": infix,
    "paip-rewriting": rewriting,
    "paip-integration": integration,
    "paip-canonical-algebra": canonical,
    "paip-expert-system": expert,
    "paip-edge-table": edge,
    "paip-scheme-compiler": scheme,
}


def check_paip_core(cases):
    """Return the number of families and original/edited observations checked."""
    observations = 0
    for slug, oracle in CORE_ORACLES.items():
        case = cases[slug]
        for args, original, edited in case["samples"]:
            for changed, expected in ((False, original), (True, edited)):
                actual = oracle(args, case["config"], changed)
                version = "edited" if changed else "original"
                require(actual == expected,
                        f"{slug}/{version}: PAIP oracle {args} returned {actual}, fixture expects {expected}")
                observations += 1
    return len(CORE_ORACLES), observations
