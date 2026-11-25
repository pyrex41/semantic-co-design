"""Compile a lexically scoped Scheme subset with tail calls and call/cc (PAIP 22–23)."""

from dataclasses import dataclass
from fractions import Fraction
from operator import add, eq, ge, gt, le, lt, mul, sub

from .residual import compile_residual
from .rules import _emit


@dataclass(frozen=True)
class _Step:
    procedure: object
    arguments: tuple


def _step(procedure, *arguments):
    return _Step(procedure, arguments)


@dataclass(frozen=True)
class _Result:
    value: object


@dataclass(frozen=True)
class _Closure:
    program: object
    frames: tuple
    arity: int


@dataclass(frozen=True)
class _Continuation:
    receiver: object


def _call_cc(receiver, args):
    if len(args) != 1:
        raise ValueError("call/cc expects one argument")
    return _invoke(args[0], (_Continuation(receiver),), receiver)


def _invoke(function, args, receiver):
    if isinstance(function, _Closure):
        if len(args) != function.arity:
            raise ValueError(f"procedure expects {function.arity} arguments")
        return _step(function.program, (list(args), *function.frames), receiver)
    if isinstance(function, _Continuation):
        if len(args) != 1:
            raise ValueError("continuation expects one argument")
        return _step(function.receiver, args[0])
    if function is _call_cc:
        return _call_cc(receiver, args)
    if callable(function):
        return _step(receiver, function(*args))
    raise ValueError(f"cannot call {function!r}")


def _true(value):
    return value is not False and value != ()


def _divide(left, right):
    return Fraction(left, right)


def _cons(head, tail):
    return (head, *tail)


def _car(expr):
    return expr[0]


def _cdr(expr):
    return expr[1:]


def _list(*args):
    return args


def _not(value):
    return not _true(value)


_PRIMITIVES = {
    "+": add, "-": sub, "*": mul, "/": _divide,
    "=": eq, "<": lt, ">": gt, "<=": le, ">=": ge,
    "cons": _cons, "car": _car, "cdr": _cdr, "list": _list, "not": _not,
    "call/cc": _call_cc, "call-with-current-continuation": _call_cc,
}


def _literal(value):
    if type(value) in (tuple, list):
        return tuple(_literal(child) for child in value)
    if type(value) in (str, int, bool):
        return value
    raise ValueError(f"unsupported Scheme datum {value!r}")


def _parameters(values):
    values = tuple(values)
    if not all(type(value) is str and value for value in values) or len(set(values)) != len(values):
        raise ValueError("parameter names must be nonempty and unique")
    return values


def _address(name, scope):
    for depth, frame in enumerate(scope):
        if name in frame:
            return f"env[{depth}][{frame.index(name)}]"
    return None


def _static_constant(expr):
    return (type(expr) in (int, bool) or expr == () or
            (type(expr) is tuple and len(expr) == 2 and expr[0] == "quote"))


class _Compiler:
    def __init__(self, primitives):
        self.functions = []
        self.bindings = {"step": _step, "Step": _Step, "Result": _Result, "Closure": _Closure,
                         "invoke": _invoke, "truth": _true}
        self.primitives = {}
        for name, function in primitives.items():
            symbol = f"primitive_{len(self.primitives)}"
            self.bindings[symbol] = function
            self.primitives[name] = symbol

    def add(self, lines, args="env, done"):
        name = f"expression_{len(self.functions)}"
        self.functions.append(f"def {name}({args}):\n{_emit(lines)}\n")
        return name

    def constant(self, value):
        return self.add([f"return step(done, {value!r})"])

    def begin(self, expressions, scope):
        if not expressions:
            return self.constant(())
        tail = self.expression(expressions[-1], scope)
        for expression in reversed(expressions[:-1]):
            if _static_constant(expression):
                continue
            first = self.expression(expression, scope)
            tail = self.add([f"return step({first}, env, lambda _: step({tail}, env, done))"])
        return tail

    def branch(self, test, yes, no, scope):
        if _static_constant(test):
            value = test[1] if isinstance(test, tuple) and test else test
            return self.expression(yes if _true(value) else no, scope)
        test = self.expression(test, scope)
        yes = self.expression(yes, scope)
        no = self.expression(no, scope)
        return self.add(["def branch(value):", f"    return step({yes} if truth(value) else {no}, env, done)",
                         f"return step({test}, env, branch)"])

    def local_form(self, opcode, bindings, body, scope):
        if not isinstance(bindings, tuple) or any(not isinstance(row, tuple) or len(row) != 2 for row in bindings):
            raise ValueError("invalid let bindings")
        names = _parameters(name for name, _ in bindings)
        values = tuple(value for _, value in bindings)
        if opcode == "letrec":
            if any(not isinstance(value, tuple) or not value or value[0] != "lambda" for value in values):
                raise ValueError("letrec bindings must be lambdas")
            body = (*(('set!', name, value) for name, value in bindings), *body)
            values = (False,) * len(names)
        return self.expression((("lambda", names, *body), *values), scope)

    def boolean_list(self, mode, expressions, scope):
        if not expressions:
            return self.constant(mode == "and")
        tail = self.expression(expressions[-1], scope)
        for expression in reversed(expressions[:-1]):
            first = self.expression(expression, scope)
            test = "truth(value)" if mode == "and" else "not truth(value)"
            tail = self.add(["def finish(value):", f"    if {test}: return step({tail}, env, done)",
                             "    return step(done, value)", f"return step({first}, env, finish)"])
        return tail

    def application(self, function, operands, scope):
        procedures = [self.expression(value, scope) for value in operands]
        if isinstance(function, str) and _address(function, scope) is None and function in self.primitives:
            symbol = self.primitives[function]
            if self.bindings[symbol] is _call_cc:
                code = f"return invoke({symbol}, values, done)"
            else:
                code = f"return step(done, {symbol}(*values))"
        else:
            procedures.append(self.expression(function, scope))
            code = "return invoke(values[-1], values[:-1], done)"
        tail = self.add([code], "env, done, values")
        for procedure in reversed(procedures):
            tail = self.add(["def received(value):", f"    return step({tail}, env, done, values + (value,))",
                             f"return step({procedure}, env, received)"], "env, done, values")
        return self.add([f"return step({tail}, env, done, ())"])

    def expression(self, expr, scope):
        if _static_constant(expr):
            return self.constant(expr[1] if isinstance(expr, tuple) and expr else expr)
        if isinstance(expr, str):
            address = _address(expr, scope) or self.primitives.get(expr)
            if address is None:
                raise ValueError(f"unbound name {expr!r}")
            return self.add([f"return step(done, {address})"])
        op, *values = expr
        if op == "if" and len(values) in (2, 3):
            test, yes, *alternative = values
            return self.branch(test, yes, alternative[0] if alternative else (), scope)
        if op == "begin":
            return self.begin(values, scope)
        if op == "set!" and len(values) == 2:
            address = _address(values[0], scope)
            if address is None:
                raise ValueError(f"cannot assign unknown lexical variable {values[0]!r}")
            source = self.expression(values[1], scope)
            return self.add(["def store(value):", f"    {address} = value", "    return step(done, value)",
                             f"return step({source}, env, store)"])
        if op == "lambda" and len(values) >= 2 and isinstance(values[0], tuple):
            parameters = _parameters(values[0])
            body = self.begin(values[1:], (parameters, *scope))
            return self.add([f"return step(done, Closure({body}, env, {len(parameters)}))"])
        if op in ("let", "letrec") and len(values) >= 2:
            return self.local_form(op, values[0], values[1:], scope)
        if op in ("and", "or"):
            return self.boolean_list(op, values, scope)
        if op in ("if", "set!", "lambda", "let", "letrec", "quote"):
            raise ValueError(f"invalid {op} form")
        return self.application(op, values, scope)


def compile_scheme(program, *, parameters=(), primitives=None):
    """Compile tuple/list programs to CPS steps using direct lexical addresses.

    The residual accepts one runtime argument per named parameter and an optional
    keyword-only instruction budget. It supports quote, begin, lambda, if,
    set!, let, function-only letrec, and/or, and first-class multi-shot call/cc
    within a run. Forms evaluate arguments left to right and then the procedure,
    as in PAIP's compiler. Booleans and the empty tuple encode truth values with
    the book's NIL convention. Primitives may be injected at compile time.
    """
    names = _parameters(parameters)
    installed = {**_PRIMITIVES, **dict(primitives or {})}
    if not all(type(name) is str and callable(value) for name, value in installed.items()):
        raise ValueError("primitives must map names to callable functions")
    compiler = _Compiler(installed)
    entry = compiler.expression(_literal(program), (names,))
    source = "\n".join(compiler.functions) + f"""
def run(*args, fuel=1_000_000):
    if len(args) != {len(names)}:
        raise ValueError('wrong number of entry arguments')
    if type(fuel) is not int or fuel < 1:
        raise ValueError('fuel must be a positive integer')
    action = step({entry}, (list(args),), Result)
    for _ in range(fuel):
        if isinstance(action, Step):
            action = action.procedure(*action.arguments)
        else:
            return action.value
    if isinstance(action, Result):
        return action.value
    raise ValueError('Scheme instruction budget exhausted')
"""
    return compile_residual("scheme", source, compiler.bindings)
