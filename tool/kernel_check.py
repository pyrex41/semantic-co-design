"""Checks of the compiler itself. Run: nix develop -c python3 -m tool check kernel"""
from itertools import product
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from experiments.branching_vm import check as vm
from specializer.backend import generate, normalize
from specializer.replay import native as run_rows
from . import shen
from .compile import build, initial_state, run_lines, size
from .pe import Unsupported, derive
from .schema import Schema

HERE = Path(__file__).parent / "kernel"

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


def rows(schema):
    slots = [("empty", 0, 0), ("full", 1, 1), ("full", 2, 5), ("full", 1, 0)]
    states = [(mode, owner, flag, (a, b)) for mode in ("idle", "held") for owner in range(3)
              for flag in (False, True) for a in slots for b in slots]
    events = [("put", k, v) for k in (1, 2, 3) for v in (0, 2)]
    events += [(tag, k) for tag in ("get", "lock", "unlock") for k in (0, 1, 2, 3)]
    events += [("math", a, b) for a in range(6) for b in range(5)]
    events += [("flag", False), ("flag", True)]
    return list(product(states, events))


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


def clause_selection(work, report):
    schema = Schema(HERE / "node.json")
    pairs = rows(schema)
    native = build(schema, work / "kernel")
    want = reference(schema, HERE / "spec.shen", pairs)
    got = observed(schema, native, pairs)
    assert got == want, next((p, g, w) for p, g, w in zip(pairs, got, want) if g != w)
    traps = sum(w == shen.TRAP for w in want)
    assert traps and native.stats["blocks"] == 0
    assert initial_state(schema, 0, 1) == schema.encode_state(
        shen.evaluate([HERE / "spec.shen"], [f"({schema.init} 0 1)"])[0])

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


LOOP = """
(define loop.init _ _ -> [0])

(define loop.step
  [Total] [gcd A B] -> (loop.gcd A B Total)
  State _ -> [State []])

(define loop.gcd
  A 0 Total -> [[(+ Total A)] [[answer A]]]
  A B Total -> (loop.gcd B (shen.mod A B) Total))
"""


def residual_loop(work, report):
    """A step whose recursion is controlled by runtime data becomes a loop behind the ABI."""
    (work / "loop.shen").write_text(LOOP)
    schema = Schema({"name": "loop", "spec": "loop.shen", "step": "loop.step",
                     "init": "loop.init", "state": ["num"], "max_effects": 1,
                     "events": {"gcd": {"role": "request", "fields": ["num", "num"]}},
                     "effects": {"answer": {"role": "output", "fields": ["num"]}}}, work)
    native = build(schema, work / "loop")
    pairs = [((total,), ("gcd", a, b)) for total in (0, 5) for a in range(40) for b in range(40)]
    assert observed(schema, native, pairs) == reference(schema, work / "loop.shen", pairs)
    assert native.stats["blocks"] >= 1
    report(f"residual loop: gcd inside a step agrees with shen-jit on {len(pairs)} rows, "
           f"{native.stats}")


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
        clause_selection(work, report)
        residual_loop(work, report)
        binding_times(work, report)
        sharing(work, report)
    report("kernel: ok")


if __name__ == "__main__":
    main()
