"""Deterministic metatheory checks against an independent integer oracle.

Run with `python -m experiments.patterns.projections.check_core` in a shell
containing Maude and shen-jit (the repository's Nix development shell does).
"""

import random
from pathlib import Path
from tempfile import TemporaryDirectory

from . import check, native


def at(xs, index):
    return xs[index] if 0 <= index < len(xs) else 0


def oracle(tree, config, args, env=()):
    op, *xs = tree
    if op == "lit":
        return xs[0]
    if op == "cfg":
        return at(config, xs[0])
    if op == "arg":
        return at(args, xs[0])
    if op == "var":
        return at(env, xs[0])
    if op == "lenv":
        return len(args)
    if op == "getv":
        return at(args, oracle(xs[0], config, args, env))
    if op == "letv":
        value = oracle(xs[0], config, args, env)
        return oracle(xs[1], config, args, (value, *env))
    if op == "ifv":
        test = oracle(xs[0], config, args, env)
        return oracle(xs[1 if test else 2], config, args, env)
    if op == "foldv":
        count = oracle(xs[0], config, args, env)
        value = oracle(xs[1], config, args, env)
        for i in range(max(0, count)):
            value = oracle(xs[2], config, args, (value, i, *env))
        return value
    a = oracle(xs[0], config, args, env)
    b = oracle(xs[1], config, args, env)
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if op in ("divv", "modv"):
        if b == 0:
            return 0
        q = (abs(a) // abs(b)) * (-1 if (a < 0) != (b < 0) else 1)
        return q if op == "divv" else a - q * b
    if op == "eqv":
        return int(a == b)
    if op == "ltv":
        return int(a < b)
    if op == "minv":
        return min(a, b)
    if op == "maxv":
        return max(a, b)
    if op == "andv":
        return int(bool(a) and bool(b))
    if op == "orv":
        return int(bool(a) or bool(b))
    raise ValueError(op)


def random_tree(rng, depth):
    atoms = [
        ("lit", rng.randrange(-3, 4)),
        ("arg", rng.randrange(-1, 5)),
        ("cfg", rng.randrange(-1, 4)),
        ("var", rng.randrange(-1, 5)),
        ("lenv",),
    ]
    if depth == 0 or rng.randrange(4) == 0:
        return rng.choice(atoms)
    op = rng.choice(
        [
            "letv",
            "ifv",
            "getv",
            "foldv",
            "add",
            "sub",
            "mul",
            "divv",
            "modv",
            "eqv",
            "ltv",
            "minv",
            "maxv",
            "andv",
            "orv",
        ]
    )
    if op == "foldv":
        # Bound nested iteration while retaining dynamic, zero, and negative counts.
        count = ("minv", ("lit", 3), ("maxv", ("lit", -1), random_tree(rng, depth - 1)))
        return (op, count, random_tree(rng, depth - 1), random_tree(rng, depth - 1))
    return (op, *(random_tree(rng, depth - 1) for _ in range(check.ARITY[op])))


def jobs():
    rng = random.Random(20261124)
    # These force de Bruijn shifting through removed and retained binders.
    targeted = [
        ("letv", ("arg", 0), ("letv", ("lit", 5), ("sub", ("var", 1), ("var", 0)))),
        (
            "letv",
            ("lit", 7),
            (
                "letv",
                ("arg", 0),
                (
                    "foldv",
                    ("lit", 3),
                    ("var", 1),
                    ("add", ("var", 0), ("add", ("var", 1), ("add", ("var", 2), ("var", 3)))),
                ),
            ),
        ),
        ("ifv", ("cfg", 0), ("getv", ("arg", 0)), ("lenv",)),
        ("letv", ("arg", 0), ("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("var", 2)))),
        ("foldv", ("lit", 257), ("lit", 0), ("add", ("var", 0), ("var", 1))),
        ("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("getv", ("var", 1)))),
        ("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("lenv",))),
        ("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("arg", 0))),
        (
            "foldv",
            ("lit", 3),
            ("lit", 0),
            ("foldv", ("arg", 0), ("var", 0), ("add", ("var", 0), ("var", 1))),
        ),
    ]
    for op in ("divv", "modv"):
        for a, b in [(-7, 3), (7, -3), (-7, -3), (7, 0)]:
            targeted.append((op, ("lit", a), ("lit", b)))
    trees = targeted + [random_tree(rng, 4) for _ in range(150)]
    for i, tree in enumerate(trees):
        config = tuple(rng.randrange(-3, 4) for _ in range(rng.randrange(4)))
        # Vary the same live argument vector, including lengths and bad indices.
        vectors = [(), (0,), (1, -2, 3), (-1, 4, 0, 2)]
        samples = tuple((a, oracle(tree, config, a)) for a in vectors)
        yield check.Job(f"core/{i}", tree, config, samples)


def fold_structure_tests(work):
    plus = ("add", ("var", 0), ("var", 1))
    examples = [
        (("foldv", ("cfg", 0), ("cfg", 1), plus), (4, 7), ("lit", 13)),
        (
            (
                "letv",
                ("lit", 2),
                (
                    "foldv",
                    ("lit", 3),
                    ("var", 0),
                    ("ifv", ("var", 1), ("add", ("var", 0), ("lit", 2)), ("var", 0)),
                ),
            ),
            (),
            ("lit", 6),
        ),
        (
            (
                "letv",
                ("arg", 0),
                ("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("var", 2))),
            ),
            (),
            None,
        ),
        (("foldv", ("lit", 257), ("lit", 0), plus), (), None),
        (("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("getv", ("var", 1)))), (), None),
        (("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("lenv",))), (), None),
        (("foldv", ("lit", 3), ("lit", 0), ("add", ("var", 0), ("arg", 0))), (), None),
        (("foldv", ("lit", 3), ("lit", 0), ("foldv", ("arg", 0), ("var", 0), plus)), (), None),
    ]
    tests = tuple(
        check.Job(f"fold/{i}", tree, config, ()) for i, (tree, config, _) in enumerate(examples)
    )

    def has_fold(tree):
        return tree[0] == "foldv" or any(has_fold(x) for x in tree[1:] if isinstance(x, tuple))

    for name, run in [
        ("shen", lambda: check.shen_results(work, tests, {})),
        ("maude", lambda: check.maude_results(work, tests, {})),
    ]:
        for job, residual, (_, _, expected) in zip(tests, run(), examples, strict=True):
            if expected is None:
                check.require(
                    has_fold(residual),
                    f"{name} {job.name}: unexpectedly eliminated dynamic or over-budget fold: {residual}",
                )
            else:
                check.require(
                    residual == expected, f"{name} {job.name}: expected {expected}, got {residual}"
                )


def main():
    cases = tuple(jobs())
    with TemporaryDirectory(prefix="projection-core-") as directory:
        work = Path(directory)
        fold_structure_tests(work)
        for name, run in [
            ("shen", lambda: check.shen_results(work, cases, {})),
            ("maude", lambda: check.maude_results(work, cases, {})),
        ]:
            results = iter(run())
            trees = []
            for job in cases:
                trees.append(check.expression(next(results), residual=True))
                for args, expected in job.samples:
                    got = next(results)
                    check.require(
                        got == expected,
                        f"{name} {job.name}: {args} {got} != {expected}; source {job.tree}",
                    )
            check.require(next(results, None) is None, f"{name} surplus results")
            for job, tree in zip(cases, trees, strict=True):
                for args, expected in job.samples:
                    # Residual cannot depend on any runtime configuration.
                    for changed_config in ((), (999, -999)):
                        got = oracle(tree, changed_config, args)
                        check.require(
                            got == expected,
                            f"{name} {job.name}: residual {tree} args {args}: {got} != {expected}; source {job.tree} config {job.config}",
                        )
            library, _ = native.compile_residuals(work, name, trees)
            groups = [[args for args, _ in job.samples] for job in cases]
            for job, got in zip(cases, native.observations(library, groups), strict=True):
                expected = [value for _, value in job.samples]
                check.require(got == expected, f"{name} {job.name}: native {got} != {expected}")
    print(
        f"PASS {len(cases)} trees, {sum(len(j.samples) for j in cases)} observations per engine and native residual"
    )


if __name__ == "__main__":
    main()
