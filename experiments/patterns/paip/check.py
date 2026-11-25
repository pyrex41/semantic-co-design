"""Run the PAIP catalog's generated residuals against independent observations."""

from fractions import Fraction as Q

from .edge import compile_edge_table
from .expert import compile_expert
from .rules import compile_matcher, compile_student, compile_translator
from .scheme import compile_scheme
from .symbolic import compile_canonical, compile_infix, compile_integrator, compile_rewriter


def _raises(error, action):
    try:
        action()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def check_segment_matcher():
    repeated = compile_matcher((("?*", "?row"), "|", ("?*", "?row")))
    assert repeated.run(("|",)) == {"?row": ()}
    assert repeated.run((0, "|", 1, "|", 0, "|", 1)) == {"?row": (0, "|", 1)}
    assert repeated.run((0, "|", 1, 0)) is None
    minimum = compile_matcher((("?+", "?row"), "|", ("?+", "?row")))
    assert minimum.run(("|",)) is None
    assert minimum.run((0, "|", 0)) == {"?row": (0,)}
    optional = compile_matcher((("??", "?prefix"), ("?*", "?body")))
    assert optional.run((2, 5, 6)) == {"?prefix": 2, "?body": (5, 6)}
    assert optional.run(()) == {"?body": ()}
    guarded = compile_matcher((("quote", ("?and", ("?not", 0), ("?is", "?one", "int"))),
                               "?two", ("?if", "ascending", "?one", "?two")),
                              predicates={"int": lambda value: type(value) is int, "ascending": lambda a, b: a < b})
    assert guarded.run((("quote", 1), 2)) == {"?one": 1, "?two": 2}
    assert guarded.run((("quote", 1), 0)) is None
    assert guarded.run((("quote", 0), 2)) is None
    assert guarded.run((("quote", "bad"), 2)) is None
    assert compile_matcher((("?or", "?x", "_"), "?x")).run((1, 2)) is None
    _raises(ValueError, lambda: compile_matcher(("?is", "?something", "missing")))
    assert repeated.source != minimum.source and "for end in range(pos + 1," in minimum.source


def check_ordered_translator():
    punctuation = "x'); raise AssertionError('inert literal') #"
    rules = [(("send", "?what"), ("urgent", "?what", "?what")),
             (("send", ("?lit", punctuation)), ("normal", punctuation))]
    transformed = []

    def reflect(value):
        transformed.append(value)
        return ("reflected", value)

    first = compile_translator(rules, transforms={"?what": reflect})
    second = compile_translator(reversed(rules), transforms={"?what": reflect})
    assert first.run(("send", punctuation)) == ("urgent", ("reflected", punctuation), ("reflected", punctuation))
    assert transformed == [punctuation]
    assert second.run(("send", punctuation)) == ("normal", punctuation)
    assert second.run(("say", punctuation)) is None
    assert transformed == [punctuation]
    assert first.source != second.source


def check_student():
    equal = ((("?+", "?left"), "equals", ("?+", "?right")), ("=", "?left", "?right"))
    plus = ((("?+", "?left"), "plus", ("?+", "?right")), ("+", "?left", "?right"))
    twice = (("twice", ("?+", "?left")), ("*", 2, "?left"))
    solver = compile_student([equal, plus, twice], noise=["the"])
    system = [("y", "plus", "x", "equals", 10), ("twice", "the", "x", "equals", 7)]
    assert solver.run(system) == ({"x": Q(7, 2), "y": Q(13, 2)}, ())
    assert solver.run(system[:1]) == ({}, (("=", ("+", "y", "x"), 10),))
    triple = compile_student([equal, plus, (twice[0], ("*", 3, "?left"))], noise=["the"])
    assert triple.run(system) == ({"x": Q(7, 3), "y": Q(23, 3)}, ())
    _raises(ValueError, lambda: solver.run([("x", "equals", 1), ("x", "equals", 2)]))
    _raises(ValueError, lambda: solver.run([("utterly", "unknown")]))
    assert solver.source != triple.source


def check_infix():
    ops = {"+": (1, "left", "+"), "*": (2, "left", "*"), "^": (3, "right", "^"), "-": (1, "left", "-")}
    original = compile_infix(ops)
    edited = compile_infix({**ops, "+": (3, "left", "sum"), "^": (4, "left", "^")})
    tokens = (1, "+", 3, "*", 2, "^", 2, "^", 3)
    assert original.run(tokens) == ("+", 1, ("*", 3, ("^", 2, ("^", 2, 3))))
    assert edited.run(tokens) == ("*", ("sum", 1, 3), ("^", ("^", 2, 2), 3))
    assert original.run(("(", 8, "-", 3, ")", "-", 2)) == ("-", ("-", 8, 3), 2)
    _raises(ValueError, lambda: original.run(("(", 2, "+", 3)))
    _raises(ValueError, lambda: original.run((2, 3)))
    assert original.source != edited.source


def check_rewriting():
    rules = [(('pick', '?x'), ('left', '?x')), (('pick', 1), 'one'),
             (('+', 0, '?x'), '?x'), (('same', '?a', '?a'), '?a')]
    operations = {"+": lambda args: sum(args) if all(type(value) is int for value in args) else None}
    before = compile_rewriter(rules, operations=operations)
    after = compile_rewriter(reversed(rules), operations=operations)
    term = ('pick', ('+', 0, 1))
    assert before.run(term) == ('left', 1)
    assert after.run(term) == 'one'
    assert before.run(('same', ('+', 2, 3), ('+', 4, 1))) == 5
    assert before.run(('same', 3, 4)) == ('same', 3, 4)
    assert before.run(('unknown', 'x')) == ('unknown', 'x')
    assert before.source != after.source
    cycle = compile_rewriter([(('up', '?x'), ('down', '?x')), (('down', '?x'), ('up', '?x'))])
    _raises(ValueError, lambda: cycle.run(('up', 1)))
    bounded = compile_rewriter([(('up', 1), ('up', 2)), (('up', 2), ('up', 3))], budget=1)
    _raises(ValueError, lambda: bounded.run(('up', 1)))
    observed = []

    def observe(arguments):
        observed.append(arguments)
        return 3

    memoized = compile_rewriter([], operations={'probe': observe})
    assert memoized.run(('node', ('probe', 1), ('probe', 1))) == ('node', 3, 3)
    assert observed == [(1,)]
    assert memoized.run(('probe', 1)) == 3 and observed == [(1,), (1,)]


def check_integration():
    cosine = [(('cos', '?u'), ('sin', '?u'))]
    integrator = compile_integrator('x', cosine)
    square = ('^', 'x', 2)
    assert integrator.run(('^', 'x', -1)) == ('log', 'x')
    assert integrator.run(('+', 4, 'x')) == ('+', ('*', 4, 'x'), ('*', ('/', 1, 2), square))
    assert integrator.run(('*', ('*', 2, 'x'), ('cos', square))) == ('sin', square)
    assert integrator.run(('cos', square)) == ('integral', ('cos', square), 'x')
    assert integrator.run(('*', 3, ('unknown', 'x'))) == ('*', 3, ('integral', ('unknown', 'x'), 'x'))
    edited = compile_integrator('x', [(('sin', '?u'), ('-', ('cos', '?u')))])
    assert edited.run(('cos', 'x')) == ('integral', ('cos', 'x'), 'x')
    assert edited.run(('sin', 'x')) == ('-', ('cos', 'x'))
    assert edited.source != integrator.source


def check_canonical_algebra():
    polynomial = compile_canonical(('x', 'y'))
    alternate = compile_canonical(('y', 'x'), operators={'mul': 'times'})
    assert polynomial.run(('*', 'x', ('+', 'x', 'y'))) == (((1, 1), 1), ((2, 0), 1))
    assert polynomial.run(('+', ('*', 'x', 'y'), ('*', 'x', 'x'))) == (((1, 1), 1), ((2, 0), 1))
    assert polynomial.run(('d', ('^', ('+', 'x', 'y'), 2), 'x')) == (((0, 1), 2), ((1, 0), 2))
    assert polynomial.run(('-', ('^', 'x', 4), ('^', 'x', 4))) == ()
    assert alternate.run(('times', 'x', 'y')) == (((1, 1), 1),)
    _raises(ValueError, lambda: alternate.run(('*', 'x', 'y')))
    _raises(ValueError, lambda: polynomial.run(('^', 'x', -1)))
    rational = compile_canonical(('x',), rational=True)
    quotient = ('/', ('-', ('^', 'x', 2), 1), ('-', 'x', 1))
    assert rational.run(quotient) == ((((0,), 1), ((1,), 1)), (((0,), 1),))
    assert rational.run(('d', ('^', 'x', -1), 'x')) == ((((0,), -1),), (((2,), 1),))
    assert rational.run(('/', 1, ('*', 2, 'x'))) == ((((0,), Q(1, 2)),), (((1,), 1),))
    _raises(ZeroDivisionError, lambda: rational.run(('/', 1, ('-', 'x', 'x'))))
    assert alternate.source != polynomial.source and rational.source != polynomial.source


def check_expert_system():
    parameters = {'decision': 'rules_first', 'reason': 'rules_first', 'report': 'ask_first',
                  'manual': 'ask_first', 'hard_fail': 'ask_first'}
    rules = [(('decision', 'repair'), [('report', 'hot')], Q(3, 4)),
             (('decision', 'repair'), [], Q(1, 2)),
             (('decision', 'repair'), [], Q(-2, 5)),
             (('reason', 'attention'), [('report', 'hot')], Q(1, 2)),
             (('reason', 'unsafe'), [('manual', 'needs_review'), ('hard_fail', 1)], 1)]
    expert = compile_expert(parameters, rules)
    prompts = []

    def ask(context, parameter, path):
        prompts.append((context, parameter, path))
        return {'hot': Q(4, 5)} if parameter == 'report' else None

    findings = expert.run(['decision', 'reason'], {'hard_fail': {1: -1}}, ask, 'unit-2')
    assert findings == {'decision': {'repair': Q(2, 3)}, 'reason': {'attention': Q(2, 5)}}
    assert prompts == [('unit-2', 'report', ('decision', 'report'))]
    strict = compile_expert(parameters, rules, cutoff=Q(4, 5))
    assert strict.run(['reason'], {'report': {'hot': Q(4, 5)}, 'hard_fail': {1: -1}}) == {'reason': {}}
    assert strict.source != expert.source
    cycle = [(('a', 1), [('b', 1)], 1), (('b', 1), [('a', 1)], 1)]
    _raises(ValueError, lambda: compile_expert({'a': 'rules_first', 'b': 'rules_first'}, cycle))


def check_edge_table():
    observed = []

    def score(pattern):
        observed.append(pattern)
        left, right = pattern
        return (5 if left == 1 else -5 if left == 2 else 0) + (3 if left == right == 1 else 0)

    edges = ((0, 1), (6, 7))
    scorer = compile_edge_table(3, edges, score)
    assert len(observed) == len(set(observed)) == 9
    board = ('XX.', '...', 'OX.')
    assert scorer.run(board, 'X') == (8 - 5)
    assert scorer.run(board, 'O') == (-5 + 5)
    assert len(observed) == 9
    edited = compile_edge_table(3, ((1, 0), (7, 6)), score)
    assert edited.run(board, 'X') == (8 + 5)
    assert edited.source != scorer.source
    _raises(ValueError, lambda: scorer.run(('XX.', '...', 'OX!'), 'X'))
    _raises(ValueError, lambda: compile_edge_table(3, ((0, 0),), score))


def check_scheme_compiler():
    loop = ('letrec', (('loop', ('lambda', ('n', 'a'),
                                ('if', ('=', 'n', 0), 'a', ('loop', ('-', 'n', 1), ('+', 'a', 'n'))))),),
            ('loop', 'count', 0))
    program = compile_scheme(loop, parameters=('count',))
    assert program.run(7000) == sum(range(7001))
    assert program.run(0) == 0
    _raises(ValueError, lambda: program.run(7000, fuel=5))
    assert 'env[0][0]' in program.source and 'letrec' not in program.source
    escape = compile_scheme(('+', 1, ('call/cc', ('lambda', ('escape',), ('+', 100, ('escape', 5))))))
    assert escape.run() == 6

    def resumable(repetitions):
        return ('let', (('saved', False), ('n', 0)),
                ('let', (('value', ('call/cc', ('lambda', ('k',), ('set!', 'saved', 'k'), 5))),),
                 ('if', ('<', 'n', repetitions),
                  ('begin', ('set!', 'n', ('+', 'n', 1)), ('saved', ('+', 'value', 1))),
                  ('list', 'n', 'value'))))

    twice = compile_scheme(resumable(2))
    thrice = compile_scheme(resumable(3))
    assert twice.run() == (2, 7)
    assert twice.run() == (2, 7)
    assert thrice.run() == (3, 8) and thrice.source != twice.source
    assert compile_scheme(('let', (('+', ('lambda', ('a', 'b'), ('-', 'a', 'b'))),), ('+', 12, 5))).run() == 7
    events = []

    def record(value):
        events.append(value)
        return value

    effects = compile_scheme(('begin', 987654321, ('if', False, ('record', ('quote', 'bad')), 0),
                              (('begin', ('record', ('quote', 'procedure')), 'list'),
                               ('record', ('quote', 'operand')))), primitives={'record': record})
    assert effects.run() == ('operand',)
    assert events == ['operand', 'procedure']
    assert '987654321' not in effects.source and 'bad' not in effects.source
    _raises(ValueError, lambda: compile_scheme(('set!', 'unbound', 1)))


def check_core():
    checks = (check_segment_matcher, check_ordered_translator, check_student, check_infix,
              check_rewriting, check_integration, check_canonical_algebra, check_expert_system,
              check_edge_table, check_scheme_compiler)
    for check in checks:
        check()
    return len(checks)


def main():
    from .check_grammar_logic import check_grammar_logic
    from .check_search_constraints import (check_adversarial_search, check_constraint_search, check_graph_search,
                                          check_planning, check_waltz_labeling)

    core = check_core()
    grammar_logic_assertions = check_grammar_logic()
    search = (check_graph_search, check_planning, check_constraint_search, check_waltz_labeling,
              check_adversarial_search)
    for check in search:
        check()
    # Grammar and logic expose five compilers, with two tests of the DCG compiler.
    count = core + len(search) + 5
    print(f"PAIP: {count} specialization families passed behavioral checks "
          f"({grammar_logic_assertions} grammar and logic assertions).")


if __name__ == '__main__':
    main()
