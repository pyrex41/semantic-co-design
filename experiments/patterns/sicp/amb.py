"""Analyze ordered choices and rollback into a reusable answer generator."""

from .evaluator import _address, _literal, _names


def compile_amb(expression, inputs=(), *, primitives=None):
    """Stage an ordered search over lexical slots, with reversible assignment.

    In addition to literals, quote, if, begin and strict primitive calls, this
    fragment has amb, amb-from, require and set!. The program returns a lazy iterator.
    Each search has its own input slots and yields answers in depth-first order.
    """
    names = _names(inputs)
    primitives = dict(primitives or {})

    def slot(name):
        address = _address(name, (names,))
        if address is None:
            raise ValueError(f"unknown search variable {name!r}")
        return address[1]

    def analyze(expr):
        if isinstance(expr, str):
            offset = slot(expr)
            return lambda frame: iter((frame[offset],))
        if not isinstance(expr, (list, tuple)):
            literal = _literal(expr)
            return lambda frame: iter((literal,))
        tag, *parts = expr
        if tag == "quote":
            value, = parts
            literal = _literal(value)
            return lambda frame: iter((literal,))
        if tag == "amb":
            choices = tuple(map(analyze, parts))

            def choose(frame):
                for choice in choices:
                    yield from choice(frame)

            return choose
        if tag == "amb-from":
            producer, = map(analyze, parts)

            def choose_values(frame):
                for values in producer(frame):
                    yield from values

            return choose_values
        if tag == "require":
            condition, = map(analyze, parts)

            def require(frame):
                for value in condition(frame):
                    if value is not False:
                        yield None

            return require
        if tag == "if":
            condition, yes, no = map(analyze, parts)

            def conditional(frame):
                for value in condition(frame):
                    yield from (no if value is False else yes)(frame)

            return conditional
        if tag in {"set!", "permanent-set!"}:
            name, rhs = parts
            offset = slot(name)
            value = analyze(rhs)
            undo = tag == "set!"

            def assign(frame):
                for result in value(frame):
                    previous = frame[offset]
                    frame[offset] = result
                    try:
                        yield None
                    finally:
                        if undo:
                            frame[offset] = previous

            return assign
        if tag == "begin":
            *prefix, last = map(analyze, parts)
            continuation = last
            for command in reversed(prefix):
                def sequence(frame, command=command, rest=continuation):
                    for _ in command(frame):
                        yield from rest(frame)

                continuation = sequence
            return continuation
        if tag == "call":
            name, *args = parts
            fn = primitives[name]
            if not callable(fn):
                raise ValueError(f"primitive {name!r} is not callable")
            arguments = tuple(map(analyze, args))

            def call(frame):
                def collect(position, values):
                    if position == len(arguments):
                        yield fn(*values)
                        return
                    for value in arguments[position](frame):
                        yield from collect(position + 1, (*values, value))

                yield from collect(0, ())

            return call
        raise ValueError(f"unknown search expression {tag!r}")

    run = analyze(expression)

    def answers(*values):
        if len(values) != len(names):
            raise TypeError("incorrect input arity")
        return run(list(values))

    return answers
