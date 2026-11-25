"""Run the Shen and Maude interpreters and execute both native residuals."""

import argparse
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from . import native


HERE = Path(__file__).resolve().parent
ARITY = {
    "lit": 1, "arg": 1, "cfg": 1, "var": 1, "letv": 2, "ifv": 3,
    "add": 2, "sub": 2, "mul": 2, "divv": 2, "modv": 2, "eqv": 2, "ltv": 2,
    "minv": 2, "maxv": 2, "andv": 2, "orv": 2, "getv": 1, "lenv": 0, "foldv": 3,
}
ATOMS = {"lit", "arg", "cfg", "var"}
INT = re.compile(r"-?\d+")
TOKEN = re.compile(r"\s+|-?\d+|[a-z][a-z0-9]*|[][(),]")
MARKER = "PROJECTION_RESULT"
TIMEOUT = 120
BATCH_SIZE = 16


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value):
    require(type(value) is int and -(1 << 63) <= value < 1 << 63, f"invalid int64 {value!r}")
    return value


def vector(values):
    require(isinstance(values, (list, tuple)), f"expected integer vector, got {values!r}")
    return tuple(map(integer, values))


def expression(value, *, residual=False):
    require(isinstance(value, (list, tuple)) and bool(value), f"invalid AST node {value!r}")
    op, *children = value
    require(isinstance(op, str) and ARITY.get(op) == len(children), f"invalid operator or arity: {value!r}")
    require(not residual or op != "cfg", f"static configuration survived specialization: {value!r}")
    if op in ATOMS:
        return op, integer(children[0])
    return (op, *(expression(child, residual=residual) for child in children))


def uses_config(tree, length):
    op, *children = tree
    if op == "cfg":
        return 0 <= children[0] < length
    return op not in ATOMS and any(uses_config(child, length) for child in children)


def shen_tree(tree):
    return "[" + " ".join(shen_tree(item) if isinstance(item, tuple) else str(item) for item in tree) + "]"


def maude_tree(tree):
    op, *children = tree
    if not children:
        return op
    args = (maude_tree(child) if isinstance(child, tuple) else str(child) for child in children)
    return f"{op}({', '.join(args)})"


def maude_vector(values):
    return "".join(f"cons({value}, " for value in values) + "nil" + ")" * len(values)


def decode(text, language):
    tokens = TOKEN.findall(text)
    require("".join(tokens) == text, f"{language} returned invalid syntax: {text!r}")
    tokens = iter([item for item in tokens if not item.isspace()] + [None])
    next_token = next(tokens)

    def take(expected=None):
        nonlocal next_token
        token = next_token
        require(token is not None and (expected is None or token == expected),
                f"{language} returned malformed result {text!r}; expected {expected!r}, got {token!r}")
        next_token = next(tokens)
        return token

    def parse():
        if INT.fullmatch(next_token or ""):
            return integer(int(take()))
        if language == "shen":
            take("[")
        op = take()
        require(op in ARITY, f"{language} returned unknown operator {op!r} in {text!r}")
        if language == "maude" and ARITY[op]:
            take("(")
        children = []
        for index in range(ARITY[op]):
            if language == "maude" and index:
                take(",")
            children.append(parse())
        if language == "maude" and ARITY[op]:
            take(")")
        if language == "shen":
            take("]")
        return (op, *children)

    value = parse()
    require(next_token is None, f"{language} returned trailing tokens in {text!r}")
    return value


@dataclass(frozen=True)
class Job:
    name: str
    tree: tuple
    config: tuple
    samples: tuple


def jobs_for_case(case):
    slug = case["id"]
    require(isinstance(slug, str) and bool(re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", slug)),
            f"invalid case id {slug!r}")
    initial, changed = map(expression, (case["program"], case["changed"]))
    config = vector(case["config"])
    samples = tuple((vector(args), integer(base), integer(edit)) for args, base, edit in case["samples"])
    require(initial != changed, f"{slug}: edited program is identical")
    require(uses_config(initial, len(config)), f"{slug}: original program needs a static input")
    require(any(base != edit for _, base, edit in samples), f"{slug}: no sample observes the source edit")
    require(len({args for args, _, _ in samples}) == len(samples), f"{slug}: duplicate sample inputs")
    return tuple(
        Job(f"{slug}/{label}", tree, config, tuple((sample[0], sample[column]) for sample in samples))
        for label, tree, column in (("original", initial, 1), ("changed", changed, 2))
    )


def run(command, *, request=None):
    result = subprocess.run(command, input=request, text=True, capture_output=True, timeout=TIMEOUT)
    require(result.returncode == 0 and not result.stderr.strip(),
            f"{command[0]} failed ({result.returncode}):\n{result.stderr}\n{result.stdout[-2000:]}")
    return result.stdout


def shen_results(work, jobs, replay):
    queries = []
    for job in jobs:
        tree = shen_tree(job.tree)
        config = shen_tree(job.config)
        queries.append(f"(proj-pe {tree} {config})")
        queries.extend(f"(proj-run {tree} {config} {shen_tree(args)})" for args, _ in job.samples)
    request = work / "batch.shen"
    request.write_text((HERE / "core.shen").read_text() + "\n" + "\n".join(
        f'(output "{MARKER} {index} ~S~%" {query})' for index, query in enumerate(queries)
    ) + "\n")
    command = [os.environ.get("SHEN_JIT", "shen-jit"), "script", str(request)]
    replay["shen"] = {"command": [*command[:-1], request.name]}
    output = run(command)
    rows = re.findall(rf"^{MARKER} (\d+) (.+)$", output, re.MULTILINE)
    require([int(index) for index, _ in rows] == list(range(len(queries))),
            f"Shen returned {len(rows)} ordered results for {len(queries)} queries:\n{output[-2000:]}")
    return [decode(row, "shen") for _, row in rows]


def maude_results(work, jobs, replay):
    queries = []
    for job in jobs:
        tree = maude_tree(job.tree)
        config = maude_vector(job.config)
        queries.append(f"specialize({tree}, {config})")
        queries.extend(f"eval({tree}, {config}, {maude_vector(args)})" for args, _ in job.samples)
    request = "".join(f"red in PROJECTION : {query} .\n" for query in queries) + "quit\n"
    request_path = work / "maude.in"
    request_path.write_text(request)
    core = work / "core.maude"
    core.write_text((HERE / core.name).read_text())
    command = [os.environ.get("MAUDE", "maude"), "-no-banner", "-no-advise", "-batch", str(core)]
    replay["maude"] = {"command": [*command[:-1], core.name], "stdin": request_path.name}
    output = run(command, request=request)
    rows = re.findall(r"^result [^:\n]+: (.+?)(?=^={10,}$|^Bye\.$|\Z)", output, re.MULTILINE | re.DOTALL)
    require(len(rows) == len(queries),
            f"Maude returned {len(rows)} results for {len(queries)} queries:\n{output[-2000:]}")
    return [decode(row.strip(), "maude") for row in rows]


def residuals(jobs, results, language):
    results = iter(results)
    trees = []
    for job in jobs:
        require((tree := next(results, None)) is not None, f"{job.name}: missing {language} specialization")
        try:
            trees.append(expression(tree, residual=True))
        except ValueError as error:
            raise ValueError(f"{job.name}: {language}: {error}") from error
        require(tree != job.tree, f"{job.name}: {language} returned the original program")
        for args, expected in job.samples:
            observed = next(results, None)
            require(type(observed) is int and observed == expected,
                    f"{job.name}: {language} interpreter {args} returned {observed!r}, expected {expected}")
    require(next(results, None) is None, f"{language} returned surplus results")
    return trees


def verify_native(work, jobs, trees, language, replay=None):
    library, compiler = native.compile_residuals(work, language, trees)
    request = json.dumps([[args for args, _ in job.samples] for job in jobs])
    request_path = work / f"{language}-native-input.json"
    request_path.write_text(request + "\n")
    command = [sys.executable, str(Path(native.__file__).resolve()), str(library)]
    if replay is not None:
        replay[f"{language}-native"] = {
            "compile": [*compiler[:-3], f"{language}.c", "-o", library.name],
            "command": [sys.executable, "native.py", library.name],
            "stdin": request_path.name,
        }
    groups = json.loads(run(command, request=request))
    require(len(groups) == len(jobs), f"{language} native runner returned the wrong number of sample groups")
    for job, values in zip(jobs, groups):
        require(len(values) == len(job.samples), f"{job.name}: {language} native runner lost samples")
        for (args, expected), observed in zip(job.samples, values):
            require(type(observed) is int and observed == expected,
                    f"{job.name}: {language} native residual {args} returned {observed}, expected {expected}")
    return groups


def negative_controls():
    job = Job("gate/control", ("cfg", 0), (3,), (((1,), 5),))
    unchanged = Job("gate/unchanged", ("lit", 5), (), job.samples)
    controls = [
        (lambda: residuals([job], [("cfg", 0), 5], "control"), "survived specialization"),
        (lambda: residuals([unchanged], [("lit", 5), 5], "control"), "original program"),
        (lambda: residuals([job], [("lit", 5), 6], "control"), "interpreter"),
        (lambda: residuals([job], [("lit", 5)], "control"), "interpreter"),
        (lambda: decode("lit(5) unexpected", "maude"), "trailing tokens"),
        (lambda: decode("[add [arg 0] [lit 2]", "shen"), "malformed result"),
        (lambda: expression(("arg", True)), "invalid int64"),
    ]
    with TemporaryDirectory(prefix="projection-control-") as directory:
        work = Path(directory)
        controls.append((lambda: verify_native(work, [job], [("lit", 6)], "control"), "native residual"))
        for trigger, diagnostic in controls:
            try:
                trigger()
            except ValueError as error:
                require(diagnostic in str(error), f"unexpected negative control failure: {error}")
            else:
                raise ValueError(f"negative control accepted: {diagnostic}")


def check_batch(cases, work, emit):
    jobs = tuple(job for case in cases for job in jobs_for_case(case))
    replay = {}
    reports = [{
        "id": job.name, "program": job.tree, "config": job.config,
        "samples": [{"args": args, "expected": expected} for args, expected in job.samples],
    } for job in jobs]
    for language, run_language in (("shen", shen_results), ("maude", maude_results)):
        results = run_language(work, jobs, replay)
        trees = residuals(jobs, results, language)
        groups = verify_native(work, jobs, trees, language, replay if emit else None)
        if emit:
            source_results = iter(results)
            for report, tree, values in zip(reports, trees, groups):
                next(source_results)  # The runtime returns the residual before the sample outcomes.
                report[f"{language}_residual"] = tree
                for sample, observed in zip(report["samples"], values):
                    sample[f"{language}_interpreter"] = next(source_results)
                    sample[f"{language}_native"] = observed
    if emit:
        (work / "native.py").write_text(Path(native.__file__).read_text())
        (work / "replay.json").write_text(json.dumps(replay, indent=2) + "\n")
        (work / "evidence.json").write_text(json.dumps(reports, indent=2) + "\n")
    return sum(len(job.samples) for job in jobs)


def check(cases, emit=None):
    require(bool(cases), "no projection cases selected")
    ids = [case["id"] for case in cases]
    require(len(set(ids)) == len(ids), "duplicate projection case IDs")
    observations = 0
    for start in range(0, len(cases), BATCH_SIZE):
        batch = cases[start:start + BATCH_SIZE]
        if emit is not None:
            work = emit if len(cases) <= BATCH_SIZE else emit / f"batch-{start // BATCH_SIZE + 1:02d}"
            work.mkdir(parents=True, exist_ok=True)
            observations += check_batch(batch, work, True)
        else:
            with TemporaryDirectory(prefix="projection-check-") as directory:
                observations += check_batch(batch, Path(directory), False)
    return len(cases), observations


def discover():
    for path in sorted((HERE / "cases").glob("*.py")):
        if not path.name.startswith("_"):
            yield from importlib.import_module(f"{__package__}.cases.{path.stem}").CASES


def catalog():
    cases = list(discover())
    inventory = json.loads((HERE / "inventory.json").read_text())
    require(isinstance(inventory, list) and len(inventory) == 75
            and all(isinstance(slug, str) for slug in inventory) and len(set(inventory)) == len(inventory),
            "inventory.json must declare 75 unique case IDs")
    by_id = {case["id"]: case for case in cases}
    require(len(by_id) == len(cases), "duplicate catalog case IDs")
    require(set(by_id) == set(inventory),
            f"inventory mismatch: missing {sorted(set(inventory) - set(by_id))}, "
            f"unexpected {sorted(set(by_id) - set(inventory))}")
    return [by_id[slug] for slug in inventory]


SMOKE = {
    "id": "projection-smoke",
    "program": ("add", ("cfg", 0), ("arg", 0)),
    "changed": ("sub", ("cfg", 0), ("arg", 0)),
    "config": [3],
    "samples": [([2], 5, 1), ([-2], 1, 5), ([], 3, 3)],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="run a small end-to-end case")
    parser.add_argument("--case", action="append", help="select one or more catalog cases by ID")
    parser.add_argument("--emit", type=Path, help="save commands, residual source, and observations")
    args = parser.parse_args()
    require(not (args.smoke and args.case), "--smoke and --case are mutually exclusive")
    cases = [SMOKE] if args.smoke else catalog()
    if args.case:
        wanted = set(args.case)
        require(wanted <= {case["id"] for case in cases}, "unknown projection case ID")
        cases = [case for case in cases if case["id"] in wanted]
    negative_controls()
    emit = args.emit.resolve() if args.emit is not None else None
    require(emit is None or not emit.exists() or (emit.is_dir() and not any(emit.iterdir())),
            "evidence output directory must be empty")
    count, observations = check(cases, emit)
    print(f"PASS {count} cases, {observations} source/sample observations across all four paths")
    if emit is not None:
        print(f"Replay artifacts in {emit}")


if __name__ == "__main__":
    main()
