"""Bind data representations and descriptions from SICP Chapter 2."""

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from math import lcm
from operator import itemgetter


def compile_rational_add(denominator, offset):
    """Add a fixed rational to fractions with a fixed input denominator."""
    if type(denominator) is not int or denominator == 0:
        raise ValueError("the denominator must be a nonzero integer")
    offset = Fraction(offset)
    common = lcm(denominator, offset.denominator)
    scale = common // denominator
    shift = offset.numerator * (common // offset.denominator)
    return lambda numerator: Fraction(numerator * scale + shift, common)


@dataclass(frozen=True)
class Interval:
    lower: int
    upper: int

    def __post_init__(self):
        if self.lower > self.upper:
            raise ValueError("interval endpoints must be ordered")


def compile_interval_product(left):
    """Multiply any interval by one fixed interval, selecting its sign class once."""
    low, high = left.lower, left.upper
    if low == high == 0:
        return lambda right: left
    if low >= 0:
        def multiply_positive(right):
            if right.lower >= 0:
                return Interval(low * right.lower, high * right.upper)
            if right.upper <= 0:
                return Interval(high * right.lower, low * right.upper)
            return Interval(high * right.lower, high * right.upper)

        return multiply_positive
    if high <= 0:
        def multiply_negative(right):
            if right.lower >= 0:
                return Interval(low * right.upper, high * right.lower)
            if right.upper <= 0:
                return Interval(high * right.upper, low * right.lower)
            return Interval(low * right.upper, low * right.lower)

        return multiply_negative

    def multiply_crossing(right):
        if right.lower >= 0:
            return Interval(low * right.upper, high * right.upper)
        if right.upper <= 0:
            return Interval(high * right.lower, low * right.lower)
        return Interval(min(low * right.upper, high * right.lower),
                        max(low * right.lower, high * right.upper))

    return multiply_crossing


def compile_tree_fold(shape, combine):
    """Fold a fixed binary topology with live leaves at the supplied indices."""
    if type(shape) is int and shape >= 0:
        return itemgetter(shape)
    if not isinstance(shape, tuple) or len(shape) != 2:
        raise ValueError("a tree shape needs a nonnegative leaf index or two child shapes")
    left = compile_tree_fold(shape[0], combine)
    right = compile_tree_fold(shape[1], combine)
    return lambda leaves: combine(left(leaves), right(leaves))


@dataclass(frozen=True)
class Frame:
    origin: tuple
    edge1: tuple
    edge2: tuple

    def vector(self, vector):
        x, y = vector
        return (x * self.edge1[0] + y * self.edge2[0],
                x * self.edge1[1] + y * self.edge2[1])

    def point(self, point):
        x, y = self.vector(point)
        return self.origin[0] + x, self.origin[1] + y

    def compose(self, inner):
        return Frame(self.point(inner.origin), self.vector(inner.edge1), self.vector(inner.edge2))


UNIT_FRAME = Frame((0, 0), (1, 0), (0, 1))


@dataclass(frozen=True)
class Segments:
    lines: tuple


@dataclass(frozen=True)
class Transform:
    frame: Frame
    painter: object


@dataclass(frozen=True)
class Overlay:
    painters: tuple


def compile_painter(painter):
    """Collapse static painter transforms into ordered lines in the unit frame."""
    def flatten(node, frame):
        if isinstance(node, Segments):
            yield from ((frame.point(start), frame.point(end)) for start, end in node.lines)
        elif isinstance(node, Transform):
            yield from flatten(node.painter, frame.compose(node.frame))
        elif isinstance(node, Overlay):
            for child in node.painters:
                yield from flatten(child, frame)
        else:
            raise TypeError(f"unknown painter {node!r}")

    lines = tuple(flatten(painter, UNIT_FRAME))
    return lambda frame: tuple((frame.point(start), frame.point(end)) for start, end in lines)


def _sum(left, right):
    if left == 0:
        return right
    if right == 0:
        return left
    if isinstance(left, int) and isinstance(right, int):
        return left + right
    return "+", left, right


def _product(left, right):
    if left == 0 or right == 0:
        return 0
    if left == 1:
        return right
    if right == 1:
        return left
    if isinstance(left, int) and isinstance(right, int):
        return left * right
    return "*", left, right


def _power(base, exponent):
    if exponent == 0:
        return 1
    if exponent == 1:
        return base
    return "**", base, exponent


def differentiate(expression, variable):
    """Differentiate integer, variable, sum, product and natural-power expressions."""
    if type(expression) is int:
        return 0
    if isinstance(expression, str):
        return int(expression == variable)
    if not isinstance(expression, tuple) or len(expression) != 3:
        raise ValueError(f"invalid expression {expression!r}")
    operator, left, right = expression
    if operator == "**":
        if type(right) is not int or right < 0:
            raise ValueError("only nonnegative integer powers are supported")
        derivative = differentiate(left, variable)
        return 0 if right == 0 else _product(_product(right, _power(left, right - 1)), derivative)
    derivative_left = differentiate(left, variable)
    derivative_right = differentiate(right, variable)
    if operator == "+":
        return _sum(derivative_left, derivative_right)
    if operator == "*":
        return _sum(_product(derivative_left, right), _product(left, derivative_right))
    raise ValueError(f"unknown operator {operator!r}")


def compile_derivative(expression, variable, parameters):
    """Build a residual arithmetic function taking values in parameter order."""
    parameters = tuple(parameters)
    slots = {name: index for index, name in enumerate(parameters)}
    if len(slots) != len(parameters) or variable not in slots:
        raise ValueError("parameters must be unique and include the differentiation variable")

    def source(node):
        if type(node) is int:
            return f"({node})"
        if isinstance(node, str):
            return f"parameter_{slots[node]}"
        # differentiate() has checked operators in the original expression.
        operator, left, right = node
        syntax = {"+": "+", "*": "*", "**": "**"}[operator]
        return f"({source(left)} {syntax} {source(right)})"

    derivative = differentiate(expression, variable)
    formals = ", ".join(f"parameter_{index}" for index in range(len(parameters)))
    namespace = {}
    exec(f"def evaluate({formals}):\n    return {source(derivative)}", namespace)
    return namespace["evaluate"]


def compile_set_membership(members, *, representation="hash"):
    """Bind a fixed set as a hash index or an ordered sequence for membership."""
    if representation == "hash":
        return frozenset(members).__contains__
    if representation == "ordered":
        entries = tuple(sorted(members))

        def contains(value):
            position = bisect_left(entries, value)
            return position < len(entries) and entries[position] == value

        return contains
    raise ValueError(f"unknown set representation {representation!r}")


@dataclass(frozen=True)
class HuffmanCodec:
    encode: object
    decode: object


def compile_huffman(tree):
    """Build an encoder and indexed decoder for a binary tree with string leaves."""
    codes = {}
    rows = []

    def visit(node, prefix):
        if isinstance(node, str):
            if node in codes:
                raise ValueError(f"duplicate Huffman symbol {node!r}")
            codes[node] = prefix
            return 0, node
        if not isinstance(node, tuple) or len(node) != 2:
            raise ValueError("Huffman branches need two children")
        state = len(rows)
        rows.append(None)
        rows[state] = (visit(node[0], (*prefix, 0)), visit(node[1], (*prefix, 1)))
        return state, None

    if isinstance(tree, str):
        raise ValueError("Huffman trees need at least two symbols")
    visit(tree, ())
    transitions = tuple(rows)

    def encode(message):
        return tuple(bit for symbol in message for bit in codes[symbol])

    def decode(bits):
        message = []
        state = 0
        for bit in bits:
            if bit != 0 and bit != 1:
                raise ValueError(f"invalid Huffman bit {bit!r}")
            state, symbol = transitions[state][int(bit)]
            if symbol is not None:
                message.append(symbol)
        if state != 0:
            raise ValueError("incomplete Huffman codeword")
        return tuple(message)

    return HuffmanCodec(encode, decode)


class ArithmeticTower:
    """Use explicit methods first, then promote both operands up an ordered tower."""
    def __init__(self, order, methods, raise_to_next):
        order = tuple(order)
        self._levels = {tag: level for level, tag in enumerate(order)}
        self._methods = dict(methods)
        self._raises = tuple(raise_to_next)
        if len(self._levels) != len(order) or len(order) != len(self._raises) + 1:
            raise ValueError("the tower needs unique tags and one raise per adjacent pair")
        self._order = order

    def bind_binary(self, operation, left_tag, right_tag):
        """Return a binary method for live untagged payloads of the stated types."""
        left_level = self._levels[left_tag]
        right_level = self._levels[right_tag]
        method = self._methods.get((operation, left_tag, right_tag))
        if method is not None:
            return method

        def convert_between(start, end):
            def identity(value):
                return value

            def compose(raise_one, previous):
                return lambda value: raise_one(previous(value))

            convert = identity
            for raise_one in self._raises[start:end]:
                convert = compose(raise_one, convert)
            return convert

        for level in range(max(left_level, right_level), len(self._order)):
            tag = self._order[level]
            method = self._methods.get((operation, tag, tag))
            if method is not None:
                left = convert_between(left_level, level)
                right = convert_between(right_level, level)
                return lambda first, second: method(left(first), right(second))
        raise TypeError(f"no method for {operation!r} on {left_tag!r} and {right_tag!r}")


def compile_polynomial_product(left_powers, right_powers):
    """Bind sparse supports (or dense ranges), leaving coefficient vectors live."""
    left_powers = tuple(left_powers)
    right_powers = tuple(right_powers)
    for powers in (left_powers, right_powers):
        if any(type(power) is not int or power < 0 for power in powers):
            raise ValueError("polynomial powers must be nonnegative integers")
        if len(set(powers)) != len(powers):
            raise ValueError("polynomial powers must be unique")
    by_power = defaultdict(list)
    for left, left_power in enumerate(left_powers):
        for right, right_power in enumerate(right_powers):
            by_power[left_power + right_power].append(f"left[{left}] * right[{right}]")
    result_powers = tuple(sorted(by_power))
    coefficients = "".join(" + ".join(by_power[power]) + ", " for power in result_powers)
    namespace = {}
    # Generated text contains only validated arities and internal integer positions.
    exec(f"def multiply(left, right):\n"
         f"    if len(left) != {len(left_powers)} or len(right) != {len(right_powers)}:\n"
         f"        raise ValueError('coefficients must match their respective supports')\n"
         f"    return ({coefficients})", namespace)
    return result_powers, namespace["multiply"]
