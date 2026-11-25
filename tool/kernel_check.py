"""Checks of the compiler itself. Run: nix develop -c python3 -m tool check kernel"""
from itertools import product
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from experiments.branching_vm import check as vm
from specializer.backend import generate, normalize
from specializer.replay import native as run_rows
from . import fuzz, maude, shen
from .compile import build, run_init, run_lines, size
from .pe import NUM_MAX, Specializer, Unsupported, derive
from .schema import Schema

HERE = Path(__file__).parent / "kernel"
FUZZ_SEEDS = 120

# A counter that lives only on the operand stack while a runtime test controls the loop.
COUNTDOWN = vm.assemble("""
  push 0
loop:
  push 1
  add
  load 0
  push 1
  sub
  store 0
  load 0
  jz done
  jmp loop
done:
  halt
""")


# Two defects this compiler once had, kept as mutants that the checks below must reject.

class SwappedComparison(Specializer):
    """`<=` and `>=` compiled with each other's meaning."""

    def comparison(self, name, a, b):
        left, right = (b, a) if name in (">", "<=") else (a, b)
        return ("lt" if name in ("<", ">") else "le"), left, right


class UntypedBlockKey(Specializer):
    """Residual blocks named by a key in which static `true` equals 1 and `false` equals 0."""

    def block_key(self, name, skeleton):
        return name, skeleton


def rows(schema):
    slots = [("empty", 0, 0), ("full", 1, 1), ("full", 2, 5), ("full", 1, 0)]
    states = [(mode, owner, flag, (a, b)) for mode in ("idle", "held") for owner in range(3)
              for flag in (False, True) for a in slots for b in slots]
    events = [("put", k, v) for k in (1, 2, 3) for v in (0, 2)]
    events += [(tag, k) for tag in ("get", "lock", "unlock") for k in (0, 1, 2, 3)]
    events += [("math", a, b) for a in range(6) for b in range(5)]
    events += [("flag", False), ("flag", True)]
    return list(product(states, events))


EDGES = [0, 1, 2, 3, 7, -1, -3, -7, 1 << 31, 3037000500, 1 << 60, NUM_MAX - 1, NUM_MAX,
         -(1 << 60), -NUM_MAX]


def edge_rows():
    """Comparisons and arithmetic on small, negative and extreme integers.

    Left out: a zero divisor under a non-negative dividend, where shen-jit does not return.
    """
    state = ("idle", 0, False, (("empty", 0, 0), ("empty", 0, 0)))
    events = [("cmp", a, b) for a in EDGES for b in EDGES]
    events += [("calc", op, a, b) for op in range(7) for a in EDGES for b in EDGES
               if not (op in (3, 4) and b == 0 and a >= 0)]
    return [(state, event) for event in events]


def reference(schema, source, pairs):
    return shen.evaluate([source], (f"({schema.step} {shen.term(s)} {shen.term(e)})"
                                    for s, e in pairs))


def observed(schema, native, pairs):
    out = []
    for state, event in pairs:
        raw = native.raw(schema.encode_input(schema.encode_state(state), event))
        if raw is None:
            out.append(shen.TRAP)
        else:
            after, effects = schema.decode_output(raw)
            out.append((schema.decode_state(after), effects))
    return out


def differing(got, want):
    """Rows that differ. repr also separates true from 1 and an integer from a float."""
    return sum(repr(g) != repr(w) for g, w in zip(got, want))


def clause_selection(work, report):
    schema = Schema(HERE / "node.json")
    pairs = rows(schema)
    native = build(schema, work / "kernel")
    want = reference(schema, HERE / "spec.shen", pairs)
    got = observed(schema, native, pairs)
    assert got == want, next((p, g, w) for p, g, w in zip(pairs, got, want) if g != w)
    traps = sum(w == shen.TRAP for w in want)
    assert traps and native.stats["blocks"] == 0

    # The initial state comes from the native library and from the executable.
    init = schema.encode_state(shen.evaluate([HERE / "spec.shen"], [f"({schema.init} 0 1)"])[0])
    assert native.init(0, 1) == init == run_init(native.executable, 0, 1)

    # The same vectors through the line-oriented executable.
    vectors = [schema.encode_input(schema.encode_state(s), e) for s, e in pairs]
    assert run_lines(native.executable, vectors) == [native.raw(v) for v in vectors]

    # Teeth: compile a specification with two clauses swapped. The comparison with the
    # unedited Shen source must fail.
    body = (HERE / "spec.shen").read_text()
    first = "  [held Who Flag Slots] [lock Who] -> [[held Who (not Flag) Slots] []]\n"
    second = "  [held Owner Flag Slots] [lock Who] -> [[held Owner Flag Slots] [[denied Who Owner]]]\n"
    assert first + second in body
    mutant = build(schema, work / "swapped", body.replace(first + second, second + first))
    wrong = sum(g != w for g, w in zip(observed(schema, mutant, pairs), want))
    assert wrong, "the refinement comparison accepted a specification with swapped clauses"

    # A symbol that reaches runtime data must be in the shared table.
    try:
        build(schema, work / "undeclared", body.replace("[[held Who Flag Slots] [[granted Who]]]",
                                                        "[[taken Who Flag Slots] [[granted Who]]]"))
    except Unsupported as error:
        assert "symbol" in str(error)
    else:
        raise AssertionError("an undeclared symbol was encoded")
    report(f"clause selection: {len(pairs)} state/event rows agree with shen-jit "
           f"({traps} traps), {native.stats}; swapped-clause mutant differs on {wrong} rows")
    return schema, native


def operators(work, schema, native, report):
    """< > <= >= and + - * shen.mod / on runtime and constant operands, to the edge of the
    exact integer range. Overflow is a trap on both sides and nothing wraps."""
    pairs = edge_rows()
    want = reference(schema, HERE / "spec.shen", pairs)
    got = observed(schema, native, pairs)
    assert not differing(got, want), next(
        (p, g, w) for p, g, w in zip(pairs, got, want) if repr(g) != repr(w))
    traps = sum(w == shen.TRAP for w in want)
    assert 0 < traps < len(pairs)

    # The generated C has no undefined arithmetic: the same rows through a build that
    # aborts on signed overflow or a bad division give the same lines.
    vectors = [schema.encode_input(schema.encode_state(s), e) for s, e in pairs]
    checked = work / "kernel" / "node.ubsan"
    subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-O1",
                    "-Wno-tautological-compare", "-fbracket-depth=8000",
                    "-fsanitize=signed-integer-overflow,integer-divide-by-zero,shift",
                    "-fno-sanitize-recover=all", "-DNODE_MAIN", str(work / "kernel" / "node.c"),
                    "-o", str(checked)], check=True, timeout=300)
    assert run_lines(checked, vectors) == [native.raw(v) for v in vectors]

    # An input outside the exact range is refused with its own code, not computed on.
    outside = schema.encode_input(schema.encode_state(pairs[0][0]), ("cmp", NUM_MAX + 1, 0))
    assert native.code(outside) == -4 and native.code(vectors[0]) > 0
    result = subprocess.run([str(native.executable)], input=" ".join(map(str, outside)) + "\n",
                            text=True, capture_output=True, check=True, timeout=60)
    assert result.stdout == "trap 4\n"

    # Teeth: the comparison defect must show on these rows.
    mutant = build(schema, work / "swapped-comparison", specializer=SwappedComparison)
    wrong = differing(observed(schema, mutant, pairs), want)
    assert wrong, "the edge rows accepted swapped <= and >="
    report(f"operators: {len(pairs)} comparison and arithmetic rows up to +-(2^61 - 1) agree "
           f"with shen-jit ({traps} traps, overflow included); a build with "
           f"-fsanitize=signed-integer-overflow,integer-divide-by-zero gives the same lines; "
           f"an input of 2^61 returns code -4; a compiler with <= and >= swapped differs on "
           f"{wrong} rows")


LOOP = """
(define loop.init _ _ -> [0])

(define loop.step
  [Total] [gcd A B] -> (loop.gcd A B Total)
  State _ -> [State []])

(define loop.gcd
  A 0 Total -> [[(+ Total A)] [[answer A]]]
  A B Total -> (loop.gcd B (shen.mod A B) Total))
"""

# The same loop entered with a boolean on one path and the number Python equates with it
# on another: four specialization points, never fewer.
TYPED = """
(define typed.init _ _ -> [0])

(define typed.step
  [T] [go X M] -> (if (= M 0) (typed.loop 1 X T) (typed.loop true X T))
  [T] [stop X M] -> (if (= M 0) (typed.loop false X T) (typed.loop 0 X T)))

(define typed.loop
  Flag 0 T -> [[(+ T (if (= Flag 1) 111 (if (= Flag true) 222 (if (= Flag 0) 333 444))))] []]
  Flag N T -> (typed.loop Flag (- N 1) T))
"""


def small(work, name, source, events, effects=None, state=("num",)):
    (work / f"{name}.shen").write_text(source)
    return Schema({"name": name, "spec": f"{name}.shen", "step": f"{name}.step",
                   "init": f"{name}.init", "state": list(state), "max_effects": 1,
                   "events": {tag: {"role": "request", "fields": fields}
                              for tag, fields in events.items()},
                   "effects": {tag: {"role": "output", "fields": fields}
                               for tag, fields in (effects or {"o": ["num"]}).items()}}, work)


def residual_loop(work, report):
    """A step whose recursion is controlled by runtime data becomes a loop behind the ABI."""
    schema = small(work, "loop", LOOP, {"gcd": ["num", "num"]}, {"answer": ["num"]})
    native = build(schema, work / "loop")
    pairs = [((total,), ("gcd", a, b)) for total in (0, 5) for a in range(40) for b in range(40)]
    assert observed(schema, native, pairs) == reference(schema, work / "loop.shen", pairs)
    assert native.stats["blocks"] >= 1
    report(f"residual loop: gcd inside a step agrees with shen-jit on {len(pairs)} rows, "
           f"{native.stats}")


def block_names(work, report):
    """A static boolean and the number Python equates with it name different blocks."""
    schema = small(work, "typed", TYPED, {"go": ["num", "num"], "stop": ["num", "num"]})
    native = build(schema, work / "typed")
    pairs = [((t,), (tag, x, m)) for t in (0, 7) for tag in ("go", "stop") for x in range(4)
             for m in (0, 1)]
    want = reference(schema, work / "typed.shen", pairs)
    assert not differing(observed(schema, native, pairs), want)
    assert native.stats["blocks"] == 4, native.stats
    mutant = build(schema, work / "untyped", specializer=UntypedBlockKey)
    wrong = differing(observed(schema, mutant, pairs), want)
    assert wrong and mutant.stats["blocks"] == 2, (wrong, mutant.stats)
    report(f"block names: a loop entered with true, 1, false and 0 is {native.stats['blocks']} "
           f"blocks and agrees with shen-jit on {len(pairs)} rows; a compiler whose block key "
           f"ignores the type makes {mutant.stats['blocks']} blocks and differs on {wrong} rows")


def refusals(work, report):
    """Sources outside the fragment are refused with a reason; none crashes the compiler."""
    step = "(define r.init _ _ -> [0])\n(define r.step [T] [e X] -> {})\n{}"
    table = "\n".join(f"  {n} -> {n + 1}" for n in range(2500))
    cases = {
        "a number in exponent notation": (
            step.format("[[(if (= X 1e2) 1 0)] []]", ""), "not a plain integer"),
        "a decimal number": (step.format("[[(if (= X 1.5) 1 0)] []]", ""), "not a plain integer"),
        "an integer literal that Shen reads as a float": (
            step.format(f"[[(+ X {NUM_MAX + 1})] []]", ""), "outside the range"),
        "a loop that never ends on a path the run never takes": (
            step.format("(if (= X 99) (r.spin T) [[(+ T X)] []])",
                        "(define r.spin N -> (r.spin N))"), "unfolding too deep"),
        "recursion on a runtime number outside tail position": (
            step.format("[[(r.sum X)] []]", "(define r.sum 0 -> 0  N -> (+ N (r.sum (- N 1))))"),
            "unfolding too deep"),
        "static recursion 3,000 calls deep": (
            step.format("[[(+ X (r.count 3000))] []]",
                        "(define r.count 0 -> 0  N -> (+ 1 (r.count (- N 1))))"),
            "unfolding too deep"),
        "a table of 2,500 literal clauses": (
            step.format("[[(r.table X)] []]", f"(define r.table\n{table})"), "cannot be lowered"),
    }
    for label, (source, reason) in cases.items():
        schema = small(work, "r", source, {"e": ["num"]})
        try:
            build(schema, work / "refused")
        except Unsupported as error:
            assert reason in str(error), (label, str(error))
        else:
            raise AssertionError(f"{label} was compiled")
    report(f"refusals: {len(cases)} sources outside the fragment are each refused with a "
           f"reason ({'; '.join(cases)})")


def differential(work, report):
    """Seeded random programs against shen-jit, and the two known defects as mutants."""
    stats = fuzz.differential(work / "fuzz", range(FUZZ_SEEDS))
    assert not stats["bad"], stats["bad"][:3]
    assert stats["compiled"] >= FUZZ_SEEDS * 3 // 4 and stats["blocks"] and stats["traps"]
    found = {}
    for mutant in (SwappedComparison, UntypedBlockKey):
        broken = fuzz.differential(work / mutant.__name__, range(FUZZ_SEEDS), specializer=mutant,
                                   first_only=True)
        assert broken["bad"], f"random programs do not expose {mutant.__name__}"
        found[mutant.__name__] = broken["bad"][0][0]
    report(f"differential: {stats['compiled']} of {FUZZ_SEEDS} seeded random programs compile "
           f"({stats['refused']} refused) and agree with shen-jit on all {stats['rows']} rows "
           f"({stats['traps']} traps, {stats['blocks']} residual blocks); first seed exposing "
           f"each retained defect: {found}")


def translation(work, report):
    """The Shen-to-Maude translator on the cases where a careless one drifts from Shen."""
    source = (HERE / "translate.shen").read_text()
    calls = [("t.wild", 5, 6), ("t.wild", 5, 5), ("t.repeat", 1, 1), ("t.repeat", 1, 2),
             ("t.repeat", True, 1)]
    calls += [(name, x) for name in ("t.guard", "t.guard2", "t.guard3", "t.eq", "t.mixed",
                                     "t.unused", "t.lazy", "t.and", "t.or")
              for x in (0, 1, 3, 5, -1)]
    values = [0, 1, 2, 3, 7, -1, -3, -7, 1 << 60, NUM_MAX, -NUM_MAX]
    calls += [("t.compare", a, b) for a in values for b in values]
    calls += [("t.calc", op, a, b) for op in range(5) for a in values for b in values
              if not (op in (3, 4) and b == 0 and a >= 0)]
    want = shen.evaluate([HERE / "translate.shen"],
                         ["(" + " ".join([name, *map(shen.term, args)]) + ")"
                          for name, *args in calls])
    want = [None if value == shen.TRAP else value for value in want]
    terms = [f"f.{name}({', '.join(map(maude.term, args))})" for name, *args in calls]
    got = maude.reduce(maude.step_module(source), terms)
    assert not differing(got, want), next(
        (c, g, w) for c, g, w in zip(calls, got, want) if repr(g) != repr(w))
    errors = sum(value is None for value in want)
    assert errors
    # Teeth: a translation of a source with one operator changed must differ.
    edited = source.replace("(<= A B)", "(< A B)")
    assert edited != source
    wrong = differing(maude.reduce(maude.step_module(edited), terms), want)
    assert wrong
    report(f"maude translation: {len(calls)} calls (wildcards, repeated variables, guards that "
           f"are false or errors, strict =, unused bindings, every operator to the edge of the "
           f"integer range) reduce to what shen-jit returns, {errors} of them errors; a "
           f"translation with one operator changed differs on {wrong}")


def compile_vm(work, name, code, arity, **limits):
    tree, blocks = derive(vm.SHEN, "vm.run", [code, 0, [], [{"input": n} for n in range(arity)]],
                          **limits)
    binary = work / name
    subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-O2", "-x", "c", "-", "-o",
                    binary], input=generate(normalize(tree)), text=True, check=True, timeout=60)
    return binary, blocks


def binding_times(work, report):
    inputs = [[1], [2], [3], [7], [40], [1000]]
    expected = [n for n, in inputs]
    assert vm.source_results(vm.SHEN, COUNTDOWN, lambda row: row, inputs) == expected
    try:
        derive(vm.SHEN, "vm.run", [COUNTDOWN, 0, [], [{"input": 0}]], max_blocks=64)
    except Unsupported as error:
        assert "block budget" in str(error)
    else:
        raise AssertionError("the default division was expected to exhaust the block budget")
    # Widening: the stack counter becomes a runtime value after four distinct values.
    binary, widened = compile_vm(work, "widened", COUNTDOWN, 1, widen=4, max_blocks=64)
    assert run_rows(binary, inputs) == [[n] for n in expected]
    # Annotation at the entry: code and program counter static, stack and locals dynamic.
    binary, annotated = compile_vm(work, "annotated", COUNTDOWN, 1, max_blocks=64,
                                   division={"vm.run": ["S", "S", "D", "D"]})
    assert run_rows(binary, inputs) == [[n] for n in expected]
    assert annotated < widened <= 64
    report(f"binding times: stack-only counter fails the default division, "
           f"{widened} blocks with widen=4, {annotated} blocks with an annotated division")


def sharing(work, report):
    source = work / "double.shen"
    source.write_text("(define double 0 X -> X  N X -> (double (- N 1) (+ X X)))\n")
    tree, _ = derive(source, "double", [40, {"input": 0}])
    shared = size(normalize(tree))
    binary = work / "double"
    subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-O2", "-x", "c", "-", "-o",
                    binary], input=generate(normalize(tree)), text=True, check=True, timeout=60)
    inputs = [[0], [1], [3], [100]]
    assert run_rows(binary, inputs) == [[n * 2**40] for n, in inputs]
    assert shen.evaluate([source], ["(double 40 3)"]) == [3 * 2**40]
    # Teeth: without sharing the same evaluator doubles the expression at every level.
    unshared = size(derive(source, "double", [14, {"input": 0}], share=False)[0])
    assert shared < 400 and unshared > 2**14, (shared, unshared)
    report(f"sharing: 40 doublings lower to {shared} IR nodes; 14 doublings without sharing "
           f"lower to {unshared}")


def main(report=print):
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        schema, native = clause_selection(work, report)
        operators(work, schema, native, report)
        residual_loop(work, report)
        block_names(work, report)
        refusals(work, report)
        differential(work, report)
        translation(work, report)
        binding_times(work, report)
        sharing(work, report)
    report("kernel: ok")


if __name__ == "__main__":
    main()
