"""Independent outcomes for changed SICP language and machine profiles."""

from itertools import islice
from math import factorial
import operator as op

from .amb import compile_amb
from .evaluator import apply_procedure, compile_evaluator
from .machine import InstructionSequence, MachineResult, compile_machine, preserving
from .memory import Copy, Ref, compile_collector
from .query import compile_query


def rejects(error, fn):
    try:
        fn()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def check_evaluator():
    for arithmetic, expected in [(op.add, 24), (op.mul, 91)]:
        source = ["begin", ["set!", "x", 7],
                  ["call", ["lambda", ["y"], ["call", "combine", "x", "y"]], "input"]]
        run = compile_evaluator(source, ("x", "input"), primitives={"combine": arithmetic})
        source[1][2] = 900
        assert run(31, 13 if arithmetic is op.mul else 17) == expected
    shadow = ("call", ("lambda", ("op",), ("call", "op", 6, 7)), "fn")
    assert compile_evaluator(shadow, ("fn",), primitives={"op": op.add})(op.mul) == 42
    assert compile_evaluator(("if", 0, 1, 2))() == 1
    unbound = compile_evaluator(("letrec", (("before", "after"), ("after", 3)), "before"))
    rejects(ValueError, unbound)
    # The same captured lexical frame is seen by the setter and getter.
    alias = ("letrec", (("x", 1),
                        ("put", ("lambda", ("v",), ("set!", "x", "v"))),
                        ("get", ("lambda", (), "x"))),
             ("begin", ("call", "put", "input"), ("call", "get")))
    invocation = compile_evaluator(alias, ("input",))
    assert [invocation(n) for n in (25, 40, 0)] == [25, 40, 0]
    factory = compile_evaluator(("lambda", ("x",), ("call", "op", "offset", "x")),
                                ("offset",), primitives={"op": op.add})
    compiled = factory(27)
    assert apply_procedure(compiled, 15) == 42
    assert compile_evaluator(("call", "f", 8), ("f",))(compiled) == 35

    calls = []

    def effect(n):
        calls.append(n)
        return n

    effects = ("call", ("lambda", ("x", "unused"), ("call", "add", "x", "x")),
               ("call", "effect", "n"), ("call", "effect", -99))
    primitives = {"effect": effect, "add": op.add}
    strict = compile_evaluator(effects, ("n",), primitives=primitives)
    lazy = compile_evaluator(effects, ("n",), primitives=primitives, lazy=True)
    assert (strict(8), lazy(9)) == (16, 18)
    assert calls == [8, -99, 9]
    delayed = ("call", ("lambda", ("read",), ("begin", ("set!", "x", 8), "read")), "x")
    assert compile_evaluator(delayed, ("x",))(2) == 2
    assert compile_evaluator(delayed, ("x",), lazy=True)(2) == 8

    countdown = ("letrec", (("loop", ("lambda", ("n", "acc"),
                    ("if", ("call", "eq", "n", 0), "acc",
                     ("call", "loop", ("call", "sub", "n", 1), ("call", "add", "acc", "n"))))),),
                 ("begin", ("quote", ("tail", "position")), ("call", "loop", "n", 0)))
    primitives.update(eq=op.eq, sub=op.sub)
    cases = (0, 9, 5000)
    for lazy in (False, True):
        run = compile_evaluator(countdown, ("n",), primitives=primitives, lazy=lazy)
        assert [run(n) for n in cases] == [n * (n + 1) // 2 for n in cases]
    return 21


def check_amb():
    calls = []

    def observe(n):
        calls.append(n)
        return n

    for choices, accepted in [((2, 4, 6), (2, 4)), ((5, 1, 0), (1, 0))]:
        source = ("amb", ("begin", ("set!", "x", ("amb", *choices)),
                          ("call", "observe", "x"), ("require", ("call", "small", "x")), "x"), "x")
        run = compile_amb(source, ("x",), primitives={"small": lambda x: x < 5, "observe": observe})
        calls.clear()
        answers = run(100)
        assert calls == []
        assert list(answers) == [*accepted, 100]
        assert calls == list(choices)
        assert list(run(200)) == [*accepted, 200]
    persist = compile_amb(("amb", ("begin", ("permanent-set!", "x", 6), ("amb",)), "x"), ("x",))
    assert list(persist(3)) == [6]
    cross = compile_amb(("call", "pair", ("amb", 1, 2), ("amb", 7, 8)),
                        primitives={"pair": lambda x, y: (x, y)})
    assert list(cross()) == [(1, 7), (1, 8), (2, 7), (2, 8)]
    calls.clear()

    def positive(start):
        while True:
            calls.append(start)
            yield start
            start += 1

    runtime = compile_amb(("amb-from", ("call", "sequence", "start")), ("start",),
                          primitives={"sequence": positive})
    assert list(islice(runtime(15), 3)) == [15, 16, 17]
    assert calls == [15, 16, 17]
    return 12


def check_query():
    arities = {"edge": 2, "path": 2, "blocked": 1, "label": 2, "spin": 0}
    reachability = [
        (("path", "?a", "?b"), ("edge", "?a", "?b")),
        (("path", "?a", "?b"), ("and", ("edge", "?a", "?through"), ("path", "?through", "?b"))),
    ]
    facts = {"edge": (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "e")),
             "blocked": (("c",),), "label": (("a", (1, 1)), ("b", (1, 2)), ("c", (3, 3)))}
    for start, expected in [("a", ["b", "d", "d", "e", "e"]), ("b", ["d", "e"])]:
        goal = ("and", ("path", start, "?n"), ("not", ("blocked", "?n")), ("where", "letter", "?n"))
        run = compile_query(goal, arities, rules=reachability, project=("?n",),
                            predicates={"letter": lambda name: len(name) == 1})
        assert sorted(list(run(facts))) == [(name,) for name in expected]
        assert list(run({"edge": ((start, "z"),)})) == [("z",)]
    repeats = compile_query(("label", "?name", ("?n", "?n")), arities, project=("?name", "?n"))
    assert list(repeats(facts)) == [("a", 1), ("c", 3)]
    cyclic = compile_query(("same", "?loop", ("pair", "?loop")), arities)
    assert list(cyclic({})) == []
    spin = (("spin",), ("spin",))
    fair = compile_query(("or", ("spin",), ("same", "?out", 42)), arities, rules=(spin,), project=("?out",))
    assert list(islice(fair({}), 1)) == [(42,)]
    rejects(RuntimeError, lambda: list(fair({}, budget=15)))
    nonground = compile_query(("not", ("blocked", "?any")), arities)
    rejects(ValueError, lambda: list(nonground(facts)))
    quoted = compile_query(("same", "?value", ("quote", "?literal")), arities, project=("?value",))
    assert list(quoted({})) == [("?literal",)]
    return 10


def check_machine():
    def reg(name):
        return ("reg", name)

    def label(name):
        return ("label", name)

    def const(value):
        return ("const", value)

    seen = []
    controller = [
        ("assign", "continue", label("done")), label("loop"),
        ("test", ("op", "eq", reg("n"), const(0))), ("branch", "base"),
        ("save", "continue"), ("save", "n"), ("assign", "n", ("op", "sub", reg("n"), const(1))),
        ("assign", "continue", label("after")), ("goto", label("loop")), label("after"),
        ("restore", "n"), ("restore", "continue"),
        ("assign", "value", ("op", "combine", reg("n"), reg("value"))), ("goto", reg("continue")),
        label("base"), ("assign", "value", const(1)), ("goto", reg("continue")),
        label("done"), ("perform", ("op", "observe", reg("value"))), ("halt",),
    ]
    for combine, independent in [(op.mul, factorial), (op.add, lambda n: 1 + n * (n + 1) // 2)]:
        operations = {"eq": op.eq, "sub": op.sub, "combine": combine, "observe": seen.append}
        run = compile_machine(("n", "value", "continue"), controller, inputs=("n",), outputs=("value",),
                              operations=operations)
        operations["combine"] = op.sub
        for n in range(10):
            assert run(n) == MachineResult((independent(n),), 7 + 11 * n, 2 * n, 2 * n)
            assert seen[-1] == independent(n)
        rejects(RuntimeError, lambda: run(100, budget=8))
    rejects(ValueError, lambda: compile_machine((), (label("dup"), label("dup"))))
    underflow = compile_machine(("x",), (("restore", "x"),))
    rejects(ValueError, underflow)

    setup = InstructionSequence(frozenset(), frozenset({"n", "x"}),
                                (("assign", "n", const(10)), ("assign", "x", const(7))))
    for source, expected in [("n", MachineResult((46,), 5, 1, 1)), ("x", MachineResult((7,), 3, 0, 0))]:
        use = InstructionSequence(frozenset({source}), frozenset({"out"}),
                                  (("assign", "out", reg(source)),))
        protected = preserving(("n",), setup, use)
        run = compile_machine(("n", "x", "out"), protected.code, inputs=("n",), outputs=("out",))
        assert run(46) == expected
        assert protected.needs == ({"n"} if source == "n" else set())
    return 26


def check_memory():
    heap = (("pair", (Ref(2), "s")), ("pair", ("garbage", "unused")),
            ("pair", (Ref(0), Ref(3))), ("wrap", ("title", Ref(2))))
    layouts = {"pair": ("slot", "slot"), "wrap": ("atom", "ref")}
    first = compile_collector(layouts, ("ref", "slot", "atom"))
    second = compile_collector(layouts, ("ref",))
    assert first(heap, (Ref(3), Ref(2), 7)) == Copy((Ref(0), Ref(1), 7), (
        ("wrap", ("title", Ref(1))), ("pair", (Ref(2), Ref(0))), ("pair", (Ref(1), "s"))))
    assert second(heap, (Ref(0),)) == Copy((Ref(0),), (
        ("pair", (Ref(1), "s")), ("pair", (Ref(0), Ref(2))), ("wrap", ("title", Ref(1)))))
    assert second(heap, (None,)) == Copy((None,), ())
    changed_layout = compile_collector({"pair": ("ref", "atom")}, ("ref",))
    valid = (("pair", (None, "visit")), ("pair", (Ref(99), 1)))
    assert changed_layout(valid, (Ref(0),)) == Copy((Ref(0),), (valid[0],))
    rejects(ValueError, lambda: changed_layout(valid, (Ref(1),)))
    rejects(ValueError, lambda: first(heap, (Ref(0), 5, Ref(2))))
    return 6


def check():
    return {"evaluator scenarios": check_evaluator(), "amb scenarios": check_amb(),
            "query scenarios": check_query(), "machine scenarios": check_machine(),
            "collector scenarios": check_memory()}


if __name__ == "__main__":
    print(check())
