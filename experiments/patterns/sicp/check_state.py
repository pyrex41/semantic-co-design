"""Independent outcome checks for staged state and lazy stream profiles."""

import ast
from fractions import Fraction
from itertools import count, product
from threading import Barrier, Thread

from .state import (
    Contradiction,
    bind_transfer,
    specialize_account,
    specialize_circuit,
    specialize_constraints,
    specialize_memoized,
    specialize_table,
)
from .streams import (
    from_iterable,
    interleave,
    specialize_accumulator,
    specialize_feedback,
    specialize_pairs,
    specialize_pipeline,
    specialize_window,
    take,
)


def _raises(exception_type, call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except exception_type as error:
        return error
    raise AssertionError(f"expected {exception_type.__name__}")


def _no_loops(factory):
    loop_types = (ast.For, ast.While, ast.comprehension)
    assert not any(isinstance(node, loop_types) for node in ast.walk(ast.parse(factory.source)))


def _accounts():
    for fee, withdrawals in ((0, (70, None, 0)), (2, (68, None, 1))):
        make = specialize_account(fee)
        first, second = make(100), make(100)
        alias = first
        assert alias is first and second is not first
        assert (first.withdraw(30), first.withdraw(75), first.withdraw(65 if fee else 70)) == withdrawals
        assert second.balance() == 100
        assert second.deposit(10) == 110
        assert alias.balance() == withdrawals[-1]
        assert not bind_transfer(first, second)(2)
        assert not bind_transfer(first, first)(2)
        assert bind_transfer(first, first)(0)
        assert (first.balance(), second.balance()) == (withdrawals[-1], 110)

    make = specialize_account(3)
    left, right = make(1_000), make(1_000)
    barrier = Barrier(2, timeout=3)
    outcomes = [False, False]
    transfers = (bind_transfer(left, right), bind_transfer(right, left))
    assert all("sorted" not in transfer.__code__.co_names for transfer in transfers)

    def chase(i, amount):
        barrier.wait()
        outcomes[i] = all(transfers[i](amount) for _ in range(100))

    threads = [Thread(target=chase, args=(i, amount), daemon=True) for i, amount in enumerate((3, 5))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert all(outcomes) and not any(t.is_alive() for t in threads)
    assert (left.balance(), right.balance()) == (1_200, 800)
    return 200


def _tables():
    for schema, expected, aliases in (
        ({("user", "balance"): 0, ("posting", "available"): 0, ("user", "fees"): 1},
         (90, 90, 11), True),
        ({("user", "balance"): 1, ("posting", "available"): 0, ("user", "fees"): 1},
         (90, 30, 90), False),
    ):
        # Keep the initial amount of the independent fee slot distinguishable.
        initial = (30, 11)
        keys = (("user", "balance"), ("posting", "available"), ("user", "fees"))
        make = specialize_table(schema)
        _no_loops(make)
        first, second = make(*initial), make(*initial)
        assert (first[keys[0]] is first[keys[1]]) == aliases
        assert first[keys[0]] is not second[keys[0]]
        first[keys[0]].write(90)
        assert tuple(first[key].read() for key in keys) == expected
        assert second[keys[0]].read() == (30 if aliases else 11)
    return 2


def _memoization():
    observed = []

    def fib(rec, n):
        observed.append(n)
        if n < 2:
            return n
        return rec(n - 2) + rec(n - 1)

    make = specialize_memoized(fib)
    a, b = make(), make()
    assert (a(10), a(9), a(0)) == (55, 34, 0)
    assert sorted(observed) == list(range(11))
    observed.clear()
    assert b(6) == 8
    assert sorted(observed) == list(range(7))

    seen = []

    def lookup(rec, text):
        seen.append(text)
        return text.casefold() if text else None

    normalized = specialize_memoized(lookup, key=str.casefold)()
    assert [normalized(word) for word in ("AbC", "abc", "", "")] == ["abc", "abc", None, None]
    assert seen == ["AbC", ""]
    cycle = specialize_memoized(lambda rec, n: rec(n))()
    _raises(ValueError, cycle, 4)
    _raises(ValueError, cycle, 4)
    return len(seen) + 11 + len(observed)


def _circuits():
    processed = 0
    for delay in (1, 4):
        make = specialize_circuit(("a", "b"), (("not", ("a",), "b", delay),), probes=("b",))
        _no_loops(make)
        circuit, independent = make(), make()
        circuit.inputs["a"](1)
        circuit.inputs["a"](0)
        circuit.inputs["a"](1)
        assert circuit.agenda.run(until=delay - 1) == 0
        _raises(RuntimeError, circuit.agenda.run, max_events=1)
        assert circuit.trace == [(0, "b", 0), (delay, "b", 1)]
        assert circuit.agenda.run() == 3
        assert circuit.trace == [(0, "b", 0)] + [(delay, "b", bit) for bit in (1, 0, 1, 0)]
        assert circuit.wires["b"]() == 0 and independent.wires["a"]() == 0
        assert independent.agenda.run() == 1 and independent.wires["b"]() == 1
        processed += circuit.agenda.processed + independent.agenda.processed

    for delays in ((3, 2, 1, 2), (1, 5, 2, 1)):
        gates = tuple(zip(
            ("or", "and", "not", "and"),
            (("a", "b"), ("a", "b"), ("carry",), ("either", "no_carry")),
            ("either", "carry", "no_carry", "total"),
            delays,
        ))
        make = specialize_circuit(("a", "b", "either", "carry", "no_carry", "total"), gates)
        _no_loops(make)
        for a, b in product((0, 1), repeat=2):
            circuit = make()
            circuit.agenda.run()
            circuit.inputs["a"](a)
            circuit.inputs["b"](b)
            circuit.agenda.run()
            assert circuit.wires["total"]() == (a + b) % 2
            assert circuit.wires["carry"]() == (a + b) // 2
            processed += circuit.agenda.processed

    oscillator = specialize_circuit(("x",), (("not", ("x",), "x", 1),), probes=("x",))()
    assert oscillator.agenda.run(until=3) == 3
    _raises(RuntimeError, oscillator.agenda.run, max_events=2)
    assert oscillator.trace == [(i, "x", i % 2) for i in range(6)]
    return processed + oscillator.agenda.processed


def _constraints():
    operations = 0
    for factor, bias, start, end in ((9, 32, Fraction(100, 5), 212), (2, -1, Fraction(1, 3), Fraction(-1, 3))):
        make = specialize_constraints(
            ("input", "factor", "scaled", "bias", "output"),
            (("product", "input", "factor", "scaled"), ("sum", "scaled", "bias", "output")),
            constants={"factor": factor, "bias": bias},
        )
        syntax = ast.parse(make.source)
        for function in ast.walk(syntax):
            if isinstance(function, ast.FunctionDef) and function.name.startswith(("put", "infer")):
                assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(function))
        for value in (start, start + 1, -start):
            direct, reverse = make(), make()
            expected = factor * value + bias
            assert direct["input"].assert_value(value)
            assert direct["output"].read() == expected
            assert reverse["output"].assert_value(expected)
            assert reverse["input"].read() == value
            assert not reverse["input"].assert_value(value)
            assert direct["input"].retract()
            assert direct["scaled"].read() is None and direct["output"].read() is None
            assert direct["factor"].read() == factor and direct["bias"].read() == bias
            assert reverse["input"].read() == value
            operations += 4
        assert factor * start + bias == end

    make = specialize_constraints(
        ("a", "b", "product", "n", "sum"),
        (("sum", "n", "a", "sum"), ("product", "a", "b", "product")),
        constants={"n": 7},
    )
    net = make()
    source = object()
    assert net["product"].assert_value(5, source)
    error = _raises(Contradiction, net["a"].assert_value, 0)
    assert error.args[0] == ("product", 5, 0)
    assert net["a"].read() is None and net["sum"].read() is None
    assert not net["product"].retract()
    assert net["product"].read() == 5
    assert net["a"].assert_value(2)
    assert net["b"].read() == Fraction(5, 2) and net["sum"].read() == 9
    assert net["product"].retract(source)
    assert net["a"].read() == 2 and net["b"].read() is None and net["sum"].read() == 9
    assert net["a"].retract() and net["sum"].read() is None
    assert net["a"].assert_value(0)
    assert net["product"].read() == 0 and net["b"].read() is None
    operations += 7

    cycle = specialize_constraints(
        ("a", "b", "c", "zero"),
        (("sum", "a", "zero", "b"), ("sum", "b", "zero", "c"), ("sum", "c", "zero", "a")),
        constants={"zero": 0},
    )()
    assert cycle["a"].assert_value(42) and cycle["c"].read() == 42
    assert cycle["a"].retract() and cycle["c"].read() is None
    return operations + 2


def _streams():
    forces = 0
    even = lambda value: value % 2 == 0
    increment = lambda value: value + 1
    for stages, expected, seen_until in (
        ((("filter", even), ("map", increment)), [1, 3, 5], 5),
        ((("map", increment), ("filter", even)), [2, 4, 6], 6),
    ):
        seen = []

        def observe(value):
            seen.append(value)
            return value

        run = specialize_pipeline((("map", observe), *stages))
        assert run.source.count("while ") == 1 and " for " not in run.source
        stream = run(from_iterable(count()))
        assert take(stream, 3) == expected
        assert take(stream, 3) == expected
        assert seen == list(range(seen_until))
        forces += len(seen)

    for width, stride, combine, expected in (
        (2, 2, lambda left, right: left * right, [0, 6, 20]),
        (3, 1, lambda left, center, right: left - 2 * center + right, [2, 2, 2]),
    ):
        run = specialize_window(width, stride, combine)
        _no_loops(run)
        samples = range(7) if width == 2 else (value * value for value in count())
        assert take(run(from_iterable(samples)), 3) == expected
        assert run(from_iterable(range(width - 1))) is None
    assert take(interleave(from_iterable([1, 2]), from_iterable(count(10))), 6) == [1, 10, 2, 11, 12, 13]
    for combine, expected in (
        (lambda a, b: (a, b), [(0, 0), (0, 1), (1, 1), (0, 2), (1, 2), (2, 2)]),
        (lambda a, b: a * 10 + b, [0, 1, 11, 2, 12, 22]),
    ):
        source = from_iterable(range(3))
        assert take(specialize_pairs(combine)(source, source), 10) == expected
    integers = from_iterable(count())
    fair = take(specialize_pairs(lambda a, b: (a, b))(integers, integers), 100)
    assert len(set(fair)) == 100 and all(a <= b for a, b in fair)
    assert {(4, 4), (3, 5), (0, 10)} <= set(fair)
    assert take(specialize_accumulator(lambda a, x: a + x)(10, from_iterable([3, -4, 8])), 10) == [13, 9, 17]
    assert take(specialize_accumulator(lambda a, x: a * x)(2, from_iterable([3, -4, 8])), 10) == [6, -24, -192]
    for step, slope, expected in (
        (Fraction(1, 2), 1, [2, 3, Fraction(9, 2), Fraction(27, 4), Fraction(81, 8)]),
        (2, -1, [2, -2, 2, -2, 2]),
    ):
        calls = []

        def derivative(value):
            calls.append(value)
            return slope * value

        solution = specialize_feedback(derivative, step)(2)
        assert solution.head == 2 and not calls
        assert take(solution, 5) == take(solution, 5) == expected
        assert calls == expected[:-1]
        forces += len(calls)
    return forces


def check():
    transfers, tables, memoized, events, operations, forces = (
        _accounts(), _tables(), _memoization(), _circuits(), _constraints(), _streams()
    )
    print(f"Chapter 3 verified {transfers} concurrent transfers, {tables} table schemas, "
          f"{memoized} computations for distinct cache keys, {events} agenda events, "
          f"{operations} constraint operations, and {forces} counted stream evaluations.")


if __name__ == "__main__":
    check()
