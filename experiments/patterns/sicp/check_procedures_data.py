"""Check residual behavior and count the static work moved out of each call."""

from collections import Counter, defaultdict
from dis import get_instructions
from fractions import Fraction
from inspect import getclosurevars
from itertools import product
from math import cos, gcd, isclose, pi, sin, sqrt
from operator import add, mul
from random import Random
from unittest.mock import patch

from . import data, procedures


def expect_error(kind, function, *arguments):
    try:
        function(*arguments)
    except kind:
        return
    raise AssertionError(f"expected {kind.__name__} from {function}")


def assert_no_scan(function):
    assert not any(instruction.opname == "FOR_ITER" for instruction in get_instructions(function))


def check_procedures(counts):
    for coefficients, constant, initial in (((1, 1), 0, (0, 1)), ((2, 0, -1), 3, (2, -1, 7)),
                                             ((-2,), -3, (6,))):
        evaluate = procedures.compile_recurrence(coefficients, constant)
        advance = getclosurevars(evaluate).nonlocals["advance"]
        assert_no_scan(advance)
        expected = list(initial)
        for index in range(len(initial), 20):
            expected.append(constant + sum(c * x for c, x in zip(coefficients, reversed(expected))))
        for index, value in enumerate(expected):
            assert evaluate(index, initial) == value
            counts["recurrence coefficient-loop iterations removed"] += max(0, index - len(initial) + 1) * len(initial)
        expect_error(ValueError, evaluate, -1, initial)
    expect_error(ValueError, procedures.compile_recurrence, ())

    for exponent in (0, 1, 2, 7, 8, 31, 32, 79):
        operations = Counter()

        def multiply(left, right):
            operations["multiply"] += 1
            return left * right % 23

        power = procedures.compile_power(exponent, multiply)
        assert_no_scan(power)
        for base in range(-2, 25):
            assert power(base % 23) == pow(base, exponent, 23)
            if exponent:
                counts["exponent-bit decisions removed"] += exponent.bit_length() - 1
        operations_per_power = exponent.bit_length() + bin(exponent).count("1") - 2 if exponent else 0
        assert operations["multiply"] == 27 * operations_per_power
    assert procedures.compile_power(0, identity="unit")(None) == "unit"
    assert procedures.compile_power(5, lambda left, right: left + right, "")("b") == "bbbbb"
    expect_error(ValueError, procedures.compile_power, -1)

    class Value(int):
        def __mod__(self, divisor):
            counts["fixed GCD residual remainder operations"] += 1
            return int(self) % divisor

    for divisor in (1, 9, 12, 97):
        fixed_gcd = procedures.compile_fixed_gcd(divisor)
        assert_no_scan(fixed_gcd)
        for value in range(-120, 121):
            assert fixed_gcd(Value(value)) == gcd(value, divisor)
            left, right = value, divisor
            while right:
                counts["fixed GCD Euclidean remainder operations"] += 1
                left, right = right, left % right
    expect_error(ValueError, procedures.compile_fixed_gcd, 4097)

    for limit in (1, 251, 1000):
        prime = procedures.compile_primality(limit)
        for n in range(limit + 1):
            assert prime(n) == (n > 1 and all(n % d for d in range(2, n)))
            counts["bounded primality verdicts"] += 1
        expect_error(ValueError, prime, limit + 1)
    expect_error(ValueError, procedures.compile_primality, 65537**2)

    for target, scale in ((2, 1), (13, -1)):
        root = procedures.compile_half_interval(lambda x: scale * (x*x - target))
        for left, right in ((0, 4), (1, 12)):
            assert isclose(root(left, right), sqrt(target), abs_tol=1e-10, rel_tol=0)
            counts["half-interval roots from live brackets"] += 1
        expect_error(ValueError, root, 15, 20)
    assert procedures.compile_half_interval(lambda x: x - 1)(1, 2) == 1
    short = procedures.compile_half_interval(lambda x: x*x - 2, max_steps=1)
    expect_error(ArithmeticError, short, 0, 3)
    boundary = procedures.compile_half_interval(lambda x: x - 0.2, tolerance=1, max_steps=1)
    assert boundary(0, 2) == 0.5

    treatments = (("plain", cos, None, 0.7390851332151607),
                  ("damped", lambda x: 2 / x, None, sqrt(2)),
                  ("newton", lambda x: x * x - 2, lambda x: 2 * x, sqrt(2)),
                  ("newton", lambda x: x * x - 2, None, sqrt(2)))
    for transform, function, derivative, value in treatments:
        calls = Counter()

        def observed(argument):
            calls["function"] += 1
            return function(argument)

        solve = procedures.compile_solver(observed, transform=transform, derivative=derivative)
        assert set(getclosurevars(solve).nonlocals) == {"max_steps", "tolerance", "update"}
        for guess in (0.5, 1.0, 7.0):
            assert isclose(solve(guess), value, abs_tol=1e-9, rel_tol=0)
        samples_per_iteration = 3 if transform == "newton" and derivative is None else 1
        assert calls["function"] % samples_per_iteration == 0
        counts["fixed-point transform selections removed"] += calls["function"] // samples_per_iteration
    oscillation = procedures.compile_solver(lambda x: 2 / x, max_steps=8)
    expect_error(ArithmeticError, oscillation, 1.0)
    expect_error(ArithmeticError, procedures.compile_solver(lambda _: float("inf")), 1.0)
    expect_error(ValueError, procedures.compile_solver(cos), float("nan"))


def check_representations(counts):
    for denominator, offset in ((12, Fraction(7, 18)), (-6, Fraction(1, 6)), (1, Fraction(0))):
        translate = data.compile_rational_add(denominator, offset)
        assert "lcm" not in translate.__code__.co_names
        with patch.object(data, "Fraction", wraps=Fraction) as construct:
            for numerator in range(-11, 12):
                assert translate(numerator) == Fraction(numerator, denominator) + offset
            assert construct.call_count == 23
        counts["rational residual constructions"] += 23
        counts["rational generic input and result constructions"] += 46

    class Endpoint(int):
        def __rmul__(self, left):
            counts["interval residual endpoint products"] += 1
            return left * int(self)

    intervals = ((-7, -3), (-7, 0), (0, 0), (0, 4), (2, 3), (-5, 7), (1, 1))
    for left in intervals:
        multiply = data.compile_interval_product(data.Interval(*left))
        for right in intervals:
            candidates = tuple(x * y for x, y in product(left, right))
            counts["interval generic endpoint products"] += len(candidates)
            expected = data.Interval(min(candidates), max(candidates))
            assert multiply(data.Interval(*map(Endpoint, right))) == expected
    assert counts["interval residual endpoint products"] == 86
    expect_error(ValueError, data.Interval, 2, 1)
    expect_error(ValueError, data.compile_rational_add, 0, 1)


def check_tree_and_painter(counts):
    def evaluate_tree(shape, values):
        counts["tree descriptor visits removed"] += 1
        if isinstance(shape, int):
            return values[shape]
        return tuple(evaluate_tree(child, values) for child in shape)

    for shape in (2, ((0, 1), (2, 0)), (2, (1, (0, 2)))):
        fold = data.compile_tree_fold(shape, lambda a, b: (a, b))
        if isinstance(shape, tuple):
            assert "shape" not in getclosurevars(fold).nonlocals
        for leaves in ((1, 2, 3), ("red", "green", "blue"), (None, (), False)):
            assert fold(leaves) == evaluate_tree(shape, leaves)
    expect_error(ValueError, data.compile_tree_fold, (0,), add)

    def point(frame, value):
        return tuple(origin + a * value[0] + b * value[1]
                     for origin, a, b in zip(frame.origin, frame.edge1, frame.edge2))

    def draw(node, frames):
        counts["painter descriptor visits removed"] += 1
        if isinstance(node, data.Transform):
            return draw(node.painter, (node.frame, *frames))
        if isinstance(node, data.Overlay):
            return tuple(line for child in node.painters for line in draw(child, frames))
        result = []
        for line in node.lines:
            for frame in frames:
                counts["painter generic endpoint maps"] += len(line)
                line = tuple(point(frame, value) for value in line)
            result.append(line)
        return tuple(result)

    diagonal = data.Segments((((0, 0), (1, 1)),))
    border = data.Segments((((0, 0), (1, 0)), ((1, 0), (1, 1))))
    reflect = data.Frame((1, 0), (-1, 0), (0, 1))
    half = data.Frame((Fraction(1, 2), 0), (Fraction(1, 2), 0), (0, 1))
    painter = data.Overlay((border, data.Transform(half, data.Overlay((
        data.Transform(reflect, diagonal), border)))))
    render = data.compile_painter(painter)
    assert set(getclosurevars(render).nonlocals) == {"lines"}
    for frame in (data.UNIT_FRAME, reflect, data.Frame((3, 2), (1, -4), (6, 1))):
        output = render(frame)
        assert output == draw(painter, (frame,))
        counts["painter residual endpoint maps"] += sum(map(len, output))
    outer_half = data.compile_painter(data.Transform(half, data.Transform(reflect, diagonal)))
    outer_reflect = data.compile_painter(data.Transform(reflect, data.Transform(half, diagonal)))
    assert outer_half(data.UNIT_FRAME) == (((1, 0), (Fraction(1, 2), 1)),)
    assert outer_reflect(data.UNIT_FRAME) == (((Fraction(1, 2), 0), (0, 1)),)
    assert data.compile_painter(data.Overlay(()))(reflect) == ()


def check_symbolic_and_coding(counts):
    expression = ("*", ("+", ("**", "x", 3), "y"), ("+", "x", 2))
    derivative = data.compile_derivative(expression, "x", ("x", "y"))
    assert_no_scan(derivative)
    assert not derivative.__code__.co_names
    for x, y in product(range(-4, 5), repeat=2):
        assert derivative(x, y) == 4 * x**3 + 6 * x**2 + y
        counts["symbolic derivative calls with zero runtime rule dispatch"] += 1
    assert data.compile_derivative(("*", ("**", "x", 0), "z"), "x", ("x", "z"))(9, 12) == 0
    assert data.compile_derivative(("*", "x", ("**", -2, 2)), "x", ("x",))(5) == 4
    expect_error(ValueError, data.compile_derivative, ("**", "x", -1), "x", ("x",))

    members = (17, 13, 5, 13, -2, 21, 8, 1)
    contains = data.compile_set_membership(members)
    assert isinstance(contains.__self__, frozenset)
    contains_ordered = data.compile_set_membership(members, representation="ordered")
    assert set(getclosurevars(contains_ordered).nonlocals) == {"entries"}
    for value in range(-5, 23):
        expected = False
        for member in members:
            counts["set linear membership equality decisions"] += 1
            if member == value:
                expected = True
                break
        assert contains(value) == expected
        assert contains_ordered(value) == expected
        counts["set hash membership requests"] += 1
        counts["set ordered membership requests"] += 1
    assert not data.compile_set_membership(())(0)
    assert not data.compile_set_membership((), representation="ordered")(0)

    for tree, codes in ((('A', ('B', ('C', 'D'))), {'A': (0,), 'B': (1, 0), 'C': (1, 1, 0), 'D': (1, 1, 1)}),
                        ((('D', 'B'), ('A', 'C')), {'D': (0, 0), 'B': (0, 1), 'A': (1, 0), 'C': (1, 1)})):
        codec = data.compile_huffman(tree)
        assert set(getclosurevars(codec.encode).nonlocals) == {"codes"}
        assert set(getclosurevars(codec.decode).nonlocals) == {"transitions"}
        for length in range(4):
            for message in product(codes, repeat=length):
                bits = tuple(bit for symbol in message for bit in codes[symbol])
                assert codec.encode(message) == bits
                assert codec.decode(bits) == message
                counts["Huffman precomputed encoder code lookups"] += len(message)
                counts["Huffman decoder table transitions"] += len(bits)
        expect_error(ValueError, codec.decode, (2,))
        expect_error(KeyError, codec.encode, ("missing",))
    expect_error(ValueError, data.compile_huffman, ('A', 'A'))
    expect_error(ValueError, data.compile_huffman(('A', ('B', 'C'))).decode, (1,))


def check_generic_and_polynomial(counts):
    casts = Counter()

    def to_rational(value):
        casts["rational"] += 1
        return Fraction(value)

    def to_real(value):
        casts["real"] += 1
        return float(value)

    tower = data.ArithmeticTower(("int", "rational", "real"), {
        ("add", "int", "int"): add,
        ("add", "rational", "rational"): add,
        ("multiply", "real", "real"): mul,
        ("difference", "int", "rational"): lambda a, b: a - b,
    }, (to_rational, to_real))
    run = tower.bind_binary("multiply", "int", "real")
    assert set(getclosurevars(run).nonlocals) == {"left", "method", "right"}
    add_mixed = tower.bind_binary("add", "rational", "int")
    reverse_mixed = tower.bind_binary("difference", "int", "rational")
    for value in range(-8, 8):
        assert run(value, 2.5) == 2.5 * value
        assert add_mixed(Fraction(1, 3), value) == Fraction(1 + 3 * value, 3)
        assert reverse_mixed(value, Fraction(1, 3)) == Fraction(3 * value - 1, 3)
        counts["generic calls with zero runtime method resolution"] += 3
    assert casts == {"rational": 32, "real": 16}
    expect_error(TypeError, tower.bind_binary, "unsupported", "int", "real")
    complex_types = data.ArithmeticTower(("polar", "rectangular"), {
        ("add", "rectangular", "rectangular"): add,
    }, (lambda value: complex(value[0] * cos(value[1]), value[0] * sin(value[1])),))
    polar_add_rect = complex_types.bind_binary("add", "polar", "rectangular")
    assert abs(polar_add_rect((2, pi / 2), 3 + 4j) - (3 + 6j)) < 1e-12

    generator = Random(7310)
    for left_powers, right_powers in (((7, 0, 3), (1, 4)), (range(4), range(3)), ((), (6,))):
        powers, multiply = data.compile_polynomial_product(left_powers, right_powers)
        assert_no_scan(multiply)
        for _ in range(30):
            left = tuple(generator.randint(-3, 3) for _ in left_powers)
            right = tuple(generator.randint(-3, 3) for _ in right_powers)
            reference = defaultdict(int)
            for a, u in zip(left_powers, left):
                for b, v in zip(right_powers, right):
                    reference[a + b] += u * v
                    counts["polynomial degree additions and bucket selections removed"] += 1
            assert tuple(sorted(reference)) == powers
            assert multiply(left, right) == tuple(reference[power] for power in powers)
        expect_error(ValueError, multiply, (0,) * (len(left_powers) + 1), (0,) * len(right_powers))
    expect_error(ValueError, data.compile_polynomial_product, (1, 1), (0,))


def check():
    counts = Counter()
    check_procedures(counts)
    check_representations(counts)
    check_tree_and_painter(counts)
    check_symbolic_and_coding(counts)
    check_generic_and_polynomial(counts)
    return dict(counts)


if __name__ == "__main__":
    for label, value in check().items():
        print(f"{label}: {value}")
