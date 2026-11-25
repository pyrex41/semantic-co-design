"""Check bounded chapter 4–5 projection observations through the chapter APIs."""

from operator import add, eq, lt, mod, mul, sub

from ..sicp.amb import compile_amb
from ..sicp.evaluator import apply_procedure, compile_evaluator
from ..sicp.machine import InstructionSequence, compile_machine, preserving
from ..sicp.memory import Ref, compile_collector
from ..sicp.query import compile_query
from .check import require


def packed(values, radix):
    value = 0
    for digit in values:
        value = radix * value + digit
    return value


def lexical_addressing(config, edited):
    increment, = config
    expression = (
        "letrec",
        (("put", ("lambda", (), ("set!", "cell", ("call", "add", "cell", increment)))),
         ("get", ("lambda", ("operation",), ("call", "operation", "cell", "operand")))),
        ("begin", ("call", "put"),
         ("call", "get", ("if", "multiply", "mul", "fallback"))),
    )
    run = compile_evaluator(expression, ("cell", "operand", "multiply"),
                            primitives={"add": add, "mul": mul, "operation": mod,
                                        "fallback": sub if edited else add})
    return lambda cell, operand, tag: run(cell, operand, bool(tag))


def call_by_need(config, edited):
    increment, = config
    force_trace = []

    def observe(tag, value):
        force_trace.append(tag)
        return value

    def operand(site):
        def tag_is(slot):
            return "call", "eq", ("call", "digit", "schedule", site), slot

        first, second = ("right", "left") if edited else ("left", "right")
        return "if", tag_is(1), first, ("if", tag_is(2), second, 0)

    expression = (
        "call",
        ("lambda", ("left", "right"),
         ("begin", ("set!", "cell", ("call", "add", "cell", increment)),
          ("call", "sum", *(operand(site) for site in range(3))))),
        ("call", "observe", 1, "cell"),
        ("call", "observe", 2, "other"),
    )
    run = compile_evaluator(expression, ("cell", "other", "schedule"), lazy=True,
                            primitives={"observe": observe, "add": add, "eq": eq,
                                        "digit": lambda n, i: n // 3**i % 3,
                                        "sum": lambda *values: sum(values)})

    def observe_call(cell, other, schedule):
        force_trace.clear()
        total = run(cell, other, schedule)
        return 256 * total + packed(force_trace, 16)

    return observe_call


def explicit_control(config, edited):
    increment, = config
    expression = (
        "letrec",
        (("loop", ("lambda", ("n", "value"),
                   ("if", ("call", "eq", "n", 0), "value",
                    ("if", ("call", "eq", ("call", "mod", "n", 2), int(edited)),
                     ("call", "add", "n", ("call", "loop", ("call", "sub", "n", 1), "value")),
                     ("call", "loop", ("call", "sub", "n", 1),
                      ("call", "add", ("call", "mul", 2, "value"), increment)))))),),
        "loop",
    )
    entry = compile_evaluator(expression, primitives={"eq": eq, "mod": mod, "add": add,
                                                       "sub": sub, "mul": mul})()
    return lambda n, initial: apply_procedure(entry, n, initial)


def reversible_assignment(config, edited):
    inner_count, modulus = config
    expression = (
        "amb",
        ("begin",
         ("set!", "i", ("amb-from", ("call", "range", "outer"))),
         ("set!", "j", ("amb-from", ("call", "range", inner_count))),
         ("set!", "x", ("call", "candidate", "start", "i", "j")),
         ("require", ("call", "allowed", ("call", "mod", "x", modulus), "residue")),
         "x"),
        "x",
    )
    search = compile_amb(expression, ("x", "start", "residue", "outer", "i", "j"),
                         primitives={"range": range, "mod": mod, "allowed": lt if edited else eq,
                                     "candidate": lambda start, i, j: start + 3 * i + j})
    return lambda x, start, residue, outer: packed(search(x, start, residue, outer, 0, 0), 16)


def rule_based_query(config, edited):
    size, = config
    rules = (
        (("path", "?source", "?target"), ("edge", "?source", "?target")),
        (("path", "?source", "?target"),
         ("and", ("edge", "?source", "?via"), ("path", "?via", "?target"))),
    )
    query = ("and", ("root", "?source"), ("path", "?source", "?target"),
             ("not", ("blocked", "?target")), ("where", "guard", "?target"))
    run = compile_query(query, {"root": 1, "edge": 2, "path": 2, "blocked": 1},
                        rules=rules, project=("?target",),
                        predicates={"guard": lambda target: not edited or target >= 2})

    def count_proofs(start, blocked, *edges):
        facts = {
            "root": ((start,),),
            "blocked": tuple((vertex,) for vertex in range(size) if blocked & (1 << vertex)),
            "edge": tuple((source, target) for source in range(size)
                          for target in range(source + 1, size)
                          for _ in range(edges[size * source + target])),
        }
        return sum(1 for _ in run(facts))

    return count_proofs


def register(name):
    return "reg", name


def label(name):
    return "label", name


def constant(value):
    return "const", value


def operation(name, *operands):
    return "op", name, *operands


def assemble_machine(config, edited):
    bound, = config
    first, second = ("c", "b") if edited else ("b", "c")
    active_steps = []

    def observe():
        active_steps.append(None)

    controller = (
        ("assign", "divisor", register(first)),
        ("assign", "continue", label("second")),
        label("euclid"),
        ("test", operation("zero", register("divisor"))),
        ("branch", "return"),
        ("assign", "remainder", operation("mod", register("a"), register("divisor"))),
        ("assign", "a", register("divisor")),
        ("assign", "divisor", register("remainder")),
        ("perform", operation("observe")),
        ("goto", label("euclid")),
        label("return"),
        ("perform", operation("observe")),
        ("goto", register("continue")),
        label("second"),
        ("assign", "divisor", register(second)),
        ("assign", "continue", label("done")),
        ("perform", operation("observe")),
        ("goto", label("euclid")),
        label("done"),
        ("halt",),
    )
    run = compile_machine(("a", "b", "c", "divisor", "remainder", "continue"), controller,
                          inputs=("a", "b", "c"), outputs=("a",),
                          operations={"zero": lambda value: value == 0, "mod": mod, "observe": observe})

    def execute(a, b, c):
        active_steps.clear()
        result = run(a, b, c, budget=7 * bound + 3)
        # The projection counts controller transitions, each covering multiple
        # instructions in the assembled machine.
        return result.outputs[0] + 32 * len(active_steps)

    return execute


def preserve_registers(config, edited):
    replacement, = config
    read = "x" if edited else "n"
    first = InstructionSequence(
        frozenset({"x"}), frozenset({"n", "x"}),
        (("assign", "n", constant(replacement)),
         ("assign", "x", operation("add", register("x"), register("n")))),
    )
    second = InstructionSequence(frozenset({read}), frozenset({"out"}),
                                 (("assign", "out", register(read)),))
    code = preserving(("n",), first, second).code
    run = compile_machine(("n", "x", "out"), code, inputs=("n", "x"),
                          outputs=("out",), operations={"add": add})

    def execute(n, x):
        result = run(n, x)
        return 2 * result.outputs[0] + result.pushes

    return execute


def copying_collection(config, edited):
    size, = config
    collect = compile_collector({"record": ("atom", "ref", "ref")}, ("ref",))

    def reference(value):
        return Ref(value - 1) if value else None

    def execute(root, *fields):
        heap = []
        for i in range(size):
            first, second = map(reference, fields[2 * i:2 * i + 2])
            pointers = (second, first) if edited else (first, second)
            # An opaque identity payload exposes the API's forwarding order.
            heap.append(("record", (i + 1, *pointers)))
        copy = collect(heap, (reference(root),))
        expected_root = Ref(0) if root else None
        require(copy.roots == (expected_root,), "collector did not forward the root")
        forwarded = [row[0] for _, row in copy.heap]
        remapped = [ref.index + 1 if ref else 0 for _, row in copy.heap for ref in row[1:]]
        return packed(reversed(forwarded + [0] * (size - len(forwarded)) + remapped), 5)

    return execute


CHECKERS = {
    "sicp-lexical-addressing": lexical_addressing,
    "sicp-call-by-need": call_by_need,
    "sicp-explicit-control": explicit_control,
    "sicp-amb": reversible_assignment,
    "sicp-rule-based-query": rule_based_query,
    "sicp-register-machine-assembly": assemble_machine,
    "sicp-register-preservation": preserve_registers,
    "sicp-copying-collection": copying_collection,
}


def check(cases):
    observations = {}
    for case_id, compile_reference in CHECKERS.items():
        if case_id not in cases:
            continue
        case = cases[case_id]
        for edited in (False, True):
            run = compile_reference(case["config"], edited)
            for args, original, changed in case["samples"]:
                value = run(*args)
                expected = changed if edited else original
                variant = "edited" if edited else "original"
                require(value == expected,
                        f"{case_id}/{variant}: chapter API returned {value} for {args}, expected {expected}")
        observations[case_id] = 2 * len(case["samples"])
    return observations
