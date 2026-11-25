"""Stage numerical process descriptions from SICP Chapter 1."""

from functools import partial
from math import gcd, isfinite, isqrt
from operator import mul


def compile_recurrence(coefficients, constant=0):
    """Bind an affine recurrence and leave its index and initial values live.

    Coefficients multiply the most recent values first. Initial values are in
    chronological order and the index is zero-based.
    """
    coefficients = tuple(coefficients)
    if not coefficients:
        raise ValueError("a recurrence needs at least one coefficient")
    order = len(coefficients)
    # The only interpolated text is an integer position generated here.
    terms = [f"coefficients[{i}] * state[{order - i - 1}]" for i in range(order)]
    namespace = {"coefficients": coefficients, "constant": constant}
    exec("def advance(state):\n    return " + " + ".join(["constant", *terms]), namespace)
    advance = namespace["advance"]

    def evaluate(index, initial):
        state = tuple(initial)
        if len(state) != order or index < 0:
            raise ValueError("initial values must match the order and index must be nonnegative")
        if index < order:
            return state[index]
        for _ in range(order, index + 1):
            state = (*state[1:], advance(state))
        return state[-1]

    return evaluate


def compile_power(exponent, multiply=mul, identity=1):
    """Bind the exponent and multiplication law in exponentiation by squaring."""
    if type(exponent) is not int or exponent < 0:
        raise ValueError("exponent must be a nonnegative integer")
    if exponent == 0:
        return lambda base: identity
    lines = ["def power(base):", "    result = base"]
    for bit in bin(exponent)[3:]:
        lines.append("    result = multiply(result, result)")
        if bit == "1":
            lines.append("    result = multiply(result, base)")
    lines.append("    return result")
    namespace = {"multiply": multiply}
    exec("\n".join(lines), namespace)
    return namespace["power"]


def compile_fixed_gcd(divisor, *, max_entries=4096):
    """Precompute Euclidean results for the residue classes of one divisor."""
    if type(divisor) is not int or type(max_entries) is not int or not 0 < divisor <= max_entries:
        raise ValueError("positive divisor must fit in the residue table budget")
    residues = tuple(gcd(residue, divisor) for residue in range(divisor))
    return lambda value: residues[value % divisor]


def compile_primality(limit, *, max_trial_bound=65_536):
    """Sieve possible divisors once, then classify integers through limit."""
    if type(limit) is not int or limit < 0:
        raise ValueError("candidate limit must be a nonnegative integer")
    bound = isqrt(limit)
    if type(max_trial_bound) is not int or not 0 <= bound <= max_trial_bound:
        raise ValueError("candidate limit exceeds the trial divisor budget")
    composite = bytearray(bound + 1)
    for p in range(2, isqrt(bound) + 1):
        if not composite[p]:
            square = p * p
            composite[square::p] = b"\x01" * ((bound - square) // p + 1)
    divisors = tuple((p * p, p) for p in range(2, bound + 1) if not composite[p])

    def is_prime(candidate):
        if type(candidate) is not int or not 0 <= candidate <= limit:
            raise ValueError("candidate must be an integer within the profile's limit")
        if candidate < 2:
            return False
        for square, divisor in divisors:
            if square > candidate:
                break
            if candidate % divisor == 0:
                return False
        return True

    return is_prime


def _validate_convergence(function, tolerance, max_steps):
    if not callable(function):
        raise TypeError("expected a function")
    if not isfinite(tolerance) or tolerance <= 0 or type(max_steps) is not int or max_steps <= 0:
        raise ValueError("convergence needs a positive finite tolerance and step limit")


def compile_half_interval(function, *, tolerance=1e-10, max_steps=1000):
    """Bind a continuous function and solve on fresh sign-changing brackets."""
    _validate_convergence(function, tolerance, max_steps)

    def sample(point):
        value = function(point)
        if not isfinite(value):
            raise ArithmeticError("function returned a nonfinite value")
        return value

    def solve(left, right):
        if not isfinite(left) or not isfinite(right) or left >= right:
            raise ValueError("expected finite, ascending bracket endpoints")
        at_left, at_right = sample(left), sample(right)
        if at_left == 0:
            return left
        if at_right == 0:
            return right
        negative_left = at_left < 0
        if negative_left == (at_right < 0):
            raise ValueError("bracket endpoints must have opposing signs")
        for _ in range(max_steps):
            middle = left / 2 + right / 2
            if (right - left) <= tolerance:
                return middle
            if left >= middle or middle >= right:
                raise ArithmeticError("no representable midpoint within tolerance")
            value = sample(middle)
            if value == 0:
                return middle
            if (value < 0) == negative_left:
                left = middle
            else:
                right = middle
        if right - left <= tolerance:
            return left / 2 + right / 2
        raise ArithmeticError("bracket did not converge within the step limit")

    return solve


def _average_damp(function, value):
    return (value + function(value)) / 2


def _central_difference(function, step, value):
    return (function(value + step) - function(value - step)) / (2 * step)


def _newton_step(function, derivative, value):
    return value - function(value) / derivative(value)


def compile_solver(function, *, transform="plain", derivative=None, difference_step=1e-5,
                   tolerance=1e-10, max_steps=1000):
    """Choose a fixed-point transform and convergence policy for new guesses.

    A Newton transform solves function(x) = 0. The other transforms find a
    fixed point of function. Stop on absolute change between iterates.
    """
    _validate_convergence(function, tolerance, max_steps)
    if transform == "plain":
        update = function
    elif transform == "damped":
        update = partial(_average_damp, function)
    elif transform == "newton":
        if derivative is None:
            if not isfinite(difference_step) or difference_step <= 0:
                raise ValueError("the difference step must be positive and finite")
            derivative = partial(_central_difference, function, difference_step)
        update = partial(_newton_step, function, derivative)
    else:
        raise ValueError(f"unknown fixed-point transform {transform!r}")

    def solve(guess):
        if not isfinite(guess):
            raise ValueError("the initial guess must be finite")
        for _ in range(max_steps):
            following = update(guess)
            if not isfinite(following):
                raise ArithmeticError("iteration produced a nonfinite value")
            if abs(following - guess) <= tolerance:
                return following
            guess = following
        raise ArithmeticError("iteration did not converge within the step limit")

    return solve
