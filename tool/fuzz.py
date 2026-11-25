"""Seeded random node specifications, compiled and compared with shen-jit row by row.

Two families, both ordinary first-order Shen inside the fragment tool/README.md lists:

* expression programs: helpers with literal, repeated-variable and guarded clauses over
  numbers and booleans, every arithmetic and comparison operator, `if`, `let`, `cases`,
  `and`, `or`, `not`;
* state machines: a tail-recursive loop whose program counter is a static value of mixed
  type (0 1 2 3 true false p q), with a static counter, runtime data and runtime fuel, so
  that specialization points, block names and simultaneous assignment are exercised.

Rows mix small integers with values at the edge of Shen's exact range, so overflow must
trap on both sides. A divisor is never zero when the dividend is non-negative: shen-jit
does not terminate there, so that case cannot be compared.
"""
import random

from . import shen
from .compile import build
from .pe import NUM_MAX, Specializer, Unsupported
from .schema import Schema

SMALL = [0, 1, 2, 3, -1, 5]
EDGES = [NUM_MAX, -NUM_MAX, NUM_MAX - 1, 1 << 60, -(1 << 60), 1 << 31, 3037000500, 7, -7]
PCS = ["0", "1", "2", "3", "true", "false", "p", "q"]
COMPARE = ["=", "<", ">", "<=", ">="]


class Expressions:
    """[A B F] [e X Y] and [A B F] [g G] to a new [A B F] and at most one output."""
    STATE = ["num", "num", "bool"]
    EVENTS = {"e": ["num", "num"], "g": ["bool"]}

    def __init__(self, rng, prefix):
        self.r, self.prefix, self.helpers, self.defs = rng, prefix, [], []

    def call(self, kind, env, depth):
        fitting = [h for h in self.helpers if h[2] == kind]
        if not fitting:
            return None
        name, args, _ = self.r.choice(fitting)
        return "(" + " ".join([name, *(self.expr(a, env, depth - 1) for a in args)]) + ")"

    def expr(self, kind, env, depth):
        return self.num(env, depth) if kind == "num" else self.bool(env, depth)

    def shared(self, kind, env, depth):
        k = self.r.random()
        if depth <= 0 or k > 0.42:
            return None
        if k < 0.12:
            return (f"(if {self.bool(env, depth - 1)} {self.expr(kind, env, depth - 1)} "
                    f"{self.expr(kind, env, depth - 1)})")
        if k < 0.22:
            bound = self.r.choice(["num", "bool"])
            name = self.r.choice(["V", "W", "U"])
            return (f"(let {name} {self.expr(bound, env, depth - 1)} "
                    f"{self.expr(kind, {**env, name: bound}, depth - 1)})")
        if k < 0.36:
            return self.call(kind, env, depth)
        pairs = [self.bool(env, depth - 1), self.expr(kind, env, depth - 1),
                 self.bool(env, depth - 1), self.expr(kind, env, depth - 1)]
        if self.r.random() < 0.8:       # otherwise the cases may run out: a trap
            pairs += ["true", self.expr(kind, env, depth - 1)]
        return "(cases " + " ".join(pairs) + ")"

    def num(self, env, depth):
        made = self.shared("num", env, depth)
        if made:
            return made
        names = [n for n, kind in env.items() if kind == "num"]
        k = self.r.random()
        if depth <= 0 or k < 0.3:
            return self.r.choice(names) if names and self.r.random() < 0.75 else str(
                self.r.choice(SMALL))
        if k < 0.72:
            return f"({self.r.choice('+-*')} {self.num(env, depth - 1)} {self.num(env, depth - 1)})"
        if k < 0.8:
            return f"(shen.mod {self.num(env, depth - 1)} {self.r.choice([2, 3, 5])})"
        # A divisor computed at run time: zero is excluded, negative divisors are not.
        top, divisor = self.num(env, depth - 1), self.num(env, depth - 1)
        if k < 0.9:
            return f"(let D {divisor} (let M {top} (if (= D 0) 7 (shen.mod M D))))"
        return (f"(let D {divisor} (let M {top} (if (= D 0) 7 "
                f"(/ (- M (shen.mod M D)) D))))")

    def bool(self, env, depth):
        made = self.shared("bool", env, depth)
        if made:
            return made
        names = [n for n, kind in env.items() if kind == "bool"]
        k = self.r.random()
        if depth <= 0 or k < 0.15:
            return (self.r.choice(names) if names and self.r.random() < 0.8
                    else self.r.choice(["true", "false"]))
        if k < 0.65:
            return (f"({self.r.choice(COMPARE)} {self.num(env, depth - 1)} "
                    f"{self.num(env, depth - 1)})")
        if k < 0.72:
            return f"(= {self.bool(env, depth - 1)} {self.bool(env, depth - 1)})"
        if k < 0.82:
            return f"(not {self.bool(env, depth - 1)})"
        return (f"({self.r.choice(['and', 'or'])} {self.bool(env, depth - 1)} "
                f"{self.bool(env, depth - 1)})")

    def pattern(self, kind, env):
        k = self.r.random()
        same = [n for n, bound in env.items() if bound == kind]
        if same and k < 0.18:
            return self.r.choice(same)          # a repeated variable is an equality test
        if k < 0.5:
            return str(self.r.choice([0, 1, 2, -1])) if kind == "num" else self.r.choice(
                ["true", "false"])
        if k > 0.92:
            return "_"
        name = f"P{len(env)}"
        env[name] = kind
        return name

    def helper(self, n):
        name = f"{self.prefix}.h{n}"
        args = [self.r.choice(["num", "num", "bool"]) for _ in range(self.r.randint(1, 3))]
        result = self.r.choice(["num", "num", "bool"])
        clauses = []
        for _ in range(self.r.randint(1, 4)):
            env = {}
            patterns = [self.pattern(arg, env) for arg in args]
            body = self.expr(result, env, 2)
            guard = f" where {self.bool(env, 1)}" if self.r.random() < 0.3 else ""
            clauses.append(f"  {' '.join(patterns)} -> {body}{guard}")
        if self.r.random() < 0.8:               # otherwise the function is partial: traps
            env = {f"Z{i}": arg for i, arg in enumerate(args)}
            clauses.append(f"  {' '.join(env)} -> {self.expr(result, env, 2)}")
        self.defs.append(f"(define {name}\n" + "\n".join(clauses) + ")")
        self.helpers.append((name, args, result))

    def result(self, env):
        effects = f"[[o {self.num(env, 2)}]]" if self.r.random() < 0.5 else "[]"
        if self.r.random() < 0.2:
            effects = f"(if {self.bool(env, 2)} [[o {self.num(env, 1)}]] [])"
        return f"[[{self.num(env, 3)} {self.num(env, 3)} {self.bool(env, 3)}] {effects}]"

    def program(self):
        for n in range(self.r.randint(2, 5)):
            self.helper(n)
        state = {"A": "num", "B": "num", "F": "bool"}
        clauses = []
        if self.r.random() < 0.4:
            clauses.append(f"  [A A F] [e X Y] -> {self.result({'A': 'num', 'F': 'bool', 'X': 'num', 'Y': 'num'})}")
        clauses.append(f"  [A B F] [e X Y] -> {self.result({**state, 'X': 'num', 'Y': 'num'})}")
        if self.r.random() < 0.5:
            clauses.append(f"  [A B F] [g F] -> {self.result(state)}")
        clauses.append(f"  [A B F] [g G] -> {self.result({**state, 'G': 'bool'})}")
        self.defs.append(f"(define {self.prefix}.step\n" + "\n".join(clauses) + ")")
        return f"(define {self.prefix}.init _ _ -> [0 0 false])\n\n" + "\n\n".join(self.defs) + "\n"

    def rows(self, count):
        def number():
            return self.r.choice(EDGES) if self.r.random() < 0.12 else self.r.randint(-3, 4)
        out = []
        for _ in range(count):
            state = (number(), number(), self.r.random() < 0.5)
            event = (("e", number(), number()) if self.r.random() < 0.75
                     else ("g", self.r.random() < 0.5))
            out.append((state, event))
        return out


class Machines:
    """[A B C] [e N]: N turns of a loop whose program counter and counter K are static."""
    STATE = ["num", "num", "num"]
    EVENTS = {"e": ["num"]}

    def __init__(self, rng, prefix):
        self.r, self.prefix = rng, prefix

    def num(self, depth, names):
        k = self.r.random()
        if depth <= 0 or k < 0.4:
            return self.r.choice(names + [str(self.r.choice([0, 1, 2, -1]))])
        if k < 0.8:
            return f"({self.r.choice('+-*')} {self.num(depth - 1, names)} {self.num(depth - 1, names)})"
        return (f"(if {self.cond(depth - 1, names)} {self.num(depth - 1, names)} "
                f"{self.num(depth - 1, names)})")

    def cond(self, depth, names):
        k = self.r.random()
        if depth <= 0 or k < 0.7:
            return f"({self.r.choice(COMPARE)} {self.num(depth, names)} {self.num(depth, names)})"
        if k < 0.85:
            return f"(not {self.cond(depth - 1, names)})"
        return (f"({self.r.choice(['and', 'or'])} {self.cond(depth - 1, names)} "
                f"{self.cond(depth - 1, names)})")

    def program(self):
        r, run = self.r, f"{self.prefix}.run"
        pcs = r.sample(PCS, r.randint(2, 6))
        names = ["A", "B", "C"]

        def go():
            args = [self.num(2, names) for _ in names]
            if r.random() < 0.3:
                args = r.sample(["A", "B", "C", "A", "B"], 3)       # a permutation of the frame
            k = "(shen.mod (+ K 1) 3)" if r.random() < 0.5 else r.choice(["K", "0", "1"])
            return f"({run} {r.choice(pcs)} {k} {' '.join(args)} (- N 1))"

        def ret():
            effect = r.choice(["[]", f"[[o {self.num(1, names + ['K'])}]]"])
            return (f"[[{self.num(2, names + ['K'])} {self.num(2, names)} {self.num(1, names)}] "
                    f"{effect}]")

        def body(depth):
            k = r.random()
            if depth > 0 and k < 0.45:
                test = (self.cond(1, names) if r.random() < 0.8
                        else f"(= K {r.choice([0, 1, 2])})")
                return f"(if {test} {body(depth - 1)} {body(depth - 1)})"
            return go() if k < 0.9 else ret()

        clauses = [f"  PC K A B C 0 -> {ret()}"]
        for pc in pcs:
            if r.random() < 0.2:
                clauses.append(f"  {pc} K A B C N -> {body(1)} where (= K {r.choice([0, 1, 2])})")
            clauses.append(f"  {pc} K A B C N -> {body(2)}")
        start = f"({run} {r.choice(pcs)} 0 A B C N)"
        if r.random() < 0.5:
            start = (f"(if {self.cond(1, names)} {start} "
                     f"({run} {r.choice(pcs)} {r.choice([0, 1])} B A C N))")
        return (f"(define {self.prefix}.init _ _ -> [0 0 0])\n"
                f"(define {self.prefix}.step [A B C] [e N] -> {start})\n"
                f"(define {run}\n" + "\n".join(clauses) + ")\n")

    def rows(self, count):
        def number():
            return self.r.choice(EDGES) if self.r.random() < 0.05 else self.r.randint(-2, 3)
        return [((number(), number(), number()), ("e", self.r.randint(0, 7)))
                for _ in range(count)]


def generate(seed):
    """Seed to (name, family instance, Shen source). Even seeds are expression programs."""
    rng = random.Random(seed)
    name = f"z{seed}"
    family = (Expressions if seed % 2 == 0 else Machines)(rng, name)
    return name, family, family.program()


def differential(work, seeds, *, rows=60, specializer=Specializer, first_only=False, chunk=20):
    """Compile each seed's program and compare `rows` random rows with shen-jit.

    Returns {"compiled", "refused", "rows", "traps", "blocks", "bad": [(seed, state, event,
    native, shen)]} with one entry in `bad` per mismatching program. `first_only` stops
    after the first chunk of seeds that shows a mismatch, which is all a mutant run needs.
    """
    stats = {"compiled": 0, "refused": 0, "rows": 0, "traps": 0, "blocks": 0, "bad": []}
    seeds = list(seeds)
    work.mkdir(parents=True, exist_ok=True)
    for start in range(0, len(seeds), chunk):
        compare(work, seeds[start:start + chunk], rows, specializer, stats)
        if first_only and stats["bad"]:
            break
    return stats


def compare(work, seeds, rows, specializer, stats):
    batch = []
    for seed in seeds:
        name, family, source = generate(seed)
        (work / f"{name}.shen").write_text(source)
        schema = Schema({"name": name, "spec": f"{name}.shen", "step": f"{name}.step",
                         "init": f"{name}.init", "symbols": ["p", "q"], "state": family.STATE,
                         "max_effects": 1,
                         "events": {tag: {"role": "request", "fields": fields}
                                    for tag, fields in family.EVENTS.items()},
                         "effects": {"o": {"role": "output", "fields": ["num"]}}}, work)
        try:
            native = build(schema, work / name, executable=False, max_blocks=400, fuel=200000,
                           specializer=specializer)
        except Unsupported:
            stats["refused"] += 1
            continue
        stats["compiled"] += 1
        stats["blocks"] += native.stats["blocks"]
        batch.append((seed, schema, native, family.rows(rows)))
    # One shen-jit process reads every program of the chunk; the sources share no names.
    values = iter(shen.evaluate(
        [work / f"{schema.name}.shen" for _, schema, _, _ in batch],
        [f"({schema.step} {shen.term(state)} {shen.term(event)})"
         for _, schema, _, pairs in batch for state, event in pairs], timeout=300))
    for seed, schema, native, pairs in batch:
        found = None
        for state, event in pairs:
            want = next(values)
            raw = native.raw(schema.encode_input(schema.encode_state(state), event))
            if raw is None:
                got = shen.TRAP
            else:
                after, effects = schema.decode_output(raw)
                got = (schema.decode_state(after), effects)
            stats["rows"] += 1
            stats["traps"] += want == shen.TRAP
            # repr also separates true from 1 and an integer from a float.
            if found is None and repr(got) != repr(want):
                found = (seed, state, event, got, want)
        if found:
            stats["bad"].append(found)
