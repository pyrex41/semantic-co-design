"""Analyze strict and call-by-need programs against a lexical environment."""

from dataclasses import dataclass


_UNASSIGNED = object()


@dataclass
class _Procedure:
    arity: int
    body: object
    frames: tuple


@dataclass
class _Jump:
    code: object
    frames: tuple


@dataclass
class _Demand:
    value: object


@dataclass
class _Await:
    action: object
    continuation: object


@dataclass(eq=False)
class _Thunk:
    pending: _Jump
    value: object = None


def _resume(continuation, value):
    try:
        return _Await(continuation.send(value), continuation)
    except StopIteration as finished:
        return finished.value


def _suspend(procedure):
    return lambda frames: _resume(procedure(frames), None)


def _force(thunk):
    value = yield thunk.pending
    return (yield _Demand(value))


def _run(action):
    continuations = []
    forcing = set()
    while True:
        if isinstance(action, _Jump):
            action = action.code(action.frames)
        elif isinstance(action, _Await):
            continuations.append(action.continuation)
            action = action.action
        elif isinstance(action, _Demand):
            action = action.value
            if isinstance(action, _Thunk):
                if action.pending is None:
                    action = action.value
                else:
                    if action in forcing:
                        raise ValueError("recursive demand for a thunk")
                    forcing.add(action)
                    continuations.append(action)
                    action = _resume(_force(action), None)
        elif continuations:
            continuation = continuations.pop()
            if isinstance(continuation, _Thunk):
                continuation.value = action
                continuation.pending = None
                forcing.remove(continuation)
            else:
                action = _resume(continuation, action)
        else:
            return action


def _strict_arguments(arguments, frames):
    values = []
    for argument in arguments:
        value = yield _Jump(argument, frames)
        values.append((yield _Demand(value)))
    return values


def _lookup(frames, depth, offset):
    value = frames[depth][offset]
    if value is _UNASSIGNED:
        raise ValueError("reference to an unassigned recursive binding")
    return value


def _names(names):
    if isinstance(names, str):
        raise ValueError("expected a sequence of names")
    names = tuple(names)
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise ValueError("a frame must have distinct string names")
    return names


def _address(name, layout):
    for depth, frame in enumerate(layout):
        if name in frame:
            return depth, frame.index(name)
    return None


def _literal(value):
    if isinstance(value, (list, tuple)):
        return tuple(map(_literal, value))
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise ValueError(f"unsupported literal {value!r}")


def apply_procedure(procedure, *arguments):
    """Invoke a compiled closure or a host callable with already-evaluated values."""
    if isinstance(procedure, _Procedure):
        if procedure.arity != len(arguments):
            raise TypeError("incorrect procedure arity")
        frames = (list(arguments), *procedure.frames)
        return _run(_Demand(_run(_Jump(procedure.body, frames))))
    return procedure(*arguments)


def compile_evaluator(expression, inputs=(), *, primitives=None, lazy=False):
    """Compile an expression and return a function of positional input values.

    Strings are variable names, while ``('quote', datum)`` is literal data.
    Other forms are if, begin, lambda, set!, letrec and call. Only False is
    false in conditionals. Each invocation creates a fresh input frame.
    """
    primitives = dict(primitives or {})
    inputs = _names(inputs)
    if any(not callable(procedure) for procedure in primitives.values()):
        raise ValueError("primitive bindings must be callable")

    def analyze(expr, layout):
        if isinstance(expr, str):
            address = _address(expr, layout)
            if address is not None:
                depth, offset = address
                return lambda frames: _lookup(frames, depth, offset)
            procedure = primitives[expr]
            return lambda frames: procedure
        if not isinstance(expr, (list, tuple)):
            value = _literal(expr)
            return lambda frames: value
        if not expr:
            raise ValueError("empty expression")
        tag, *parts = expr
        if tag == "quote":
            datum, = parts
            value = _literal(datum)
            return lambda frames: value
        if tag == "if":
            test, yes, no = (analyze(part, layout) for part in parts)

            @_suspend
            def conditional(frames):
                value = yield _Jump(test, frames)
                truth = yield _Demand(value)
                target = no if truth is False else yes
                return _Jump(target, frames)

            return conditional
        if tag == "begin":
            *prefix, last = (analyze(part, layout) for part in parts)

            @_suspend
            def sequence(frames):
                for command in prefix:
                    yield _Jump(command, frames)
                return _Jump(last, frames)

            return sequence
        if tag == "lambda":
            parameters, body = parts
            parameters = _names(parameters)
            code = analyze(body, (parameters, *layout))
            arity = len(parameters)
            return lambda frames: _Procedure(arity, code, frames)
        if tag == "set!":
            name, value = parts
            address = _address(name, layout)
            if address is None:
                raise ValueError(f"cannot assign unbound lexical variable {name!r}")
            depth, offset = address
            code = analyze(value, layout)

            @_suspend
            def assign(frames):
                frames[depth][offset] = yield _Jump(code, frames)

            return assign
        if tag == "letrec":
            bindings, body = parts
            names = _names(name for name, _ in bindings)
            inner_layout = (names, *layout)
            initializers = tuple(analyze(value, inner_layout) for _, value in bindings)
            code = analyze(body, inner_layout)

            @_suspend
            def enter(frames):
                local = [_UNASSIGNED] * len(initializers)
                inner = (local, *frames)
                for offset, initializer in enumerate(initializers):
                    local[offset] = yield _Jump(initializer, inner)
                return _Jump(code, inner)

            return enter
        if tag == "call":
            operator, *operands = parts
            arguments = tuple(analyze(operand, layout) for operand in operands)
            if isinstance(operator, str) and _address(operator, layout) is None:
                primitive = primitives[operator]

                @_suspend
                def direct_call(frames):
                    values = yield from _strict_arguments(arguments, frames)
                    return primitive(*values)

                return direct_call
            callee = analyze(operator, layout)

            @_suspend
            def call(frames):
                value = yield _Jump(callee, frames)
                procedure = yield _Demand(value)
                if isinstance(procedure, _Procedure):
                    if procedure.arity != len(arguments):
                        raise TypeError("incorrect procedure arity")
                    # Delay at a compound application. Primitives are strict in both modes.
                    if lazy:
                        local = [_Thunk(_Jump(arg, frames)) for arg in arguments]
                    else:
                        local = yield from _strict_arguments(arguments, frames)
                    return _Jump(procedure.body, (local, *procedure.frames))
                values = yield from _strict_arguments(arguments, frames)
                return procedure(*values)

            return call
        raise ValueError(f"unknown expression form {tag!r}")

    code = analyze(expression, (inputs,))

    def execute(*values):
        if len(values) != len(inputs):
            raise TypeError("incorrect input arity")
        return _run(_Demand(_run(_Jump(code, (list(values),)))))

    return execute
