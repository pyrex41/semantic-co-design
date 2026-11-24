"""Build a native program from an executable, explicitly staged specification."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
TOKEN = re.compile(r"\s*(box\(|\[|\]|\)|-?\d+|[a-z][a-z0-9-]*)")


def _parse(form, source):
    """Read only constructor normal forms, consuming the entire result."""
    opening, closing = ("[", "]") if source == "shen" else ("box(", ")")
    stack = []
    pos = 0
    value = None
    while pos < len(form):
        match = TOKEN.match(form, pos)
        if not match:
            raise ValueError(f"unexpected {source} output near {form[pos:pos+100]!r}")
        token = match[1]
        pos = match.end()
        if token == opening:
            stack.append([])
            continue
        if token == closing:
            if not stack:
                raise ValueError("unbalanced source result")
            item = stack.pop()
            if source == "shen" and len(item) == 3 and item[0] == "wide":
                item = item[1] * 65536 + item[2]
        elif token in {"[", "]", ")", "box("}:
            raise ValueError(f"unexpected {token}")
        elif token == "nil" and source == "maude":
            if not stack or stack[-1]:
                raise ValueError("unexpected nil")
            continue
        else:
            item = int(token) if re.fullmatch(r"-?\d+", token) else token
        if stack:
            stack[-1].append(item)
        elif value is None:
            value = item
        else:
            raise ValueError("multiple source results")
    if stack or not isinstance(value, list):
        raise ValueError("expected list result")
    return value


def _term(value, source):
    if type(value) is int and -(2**63) <= value < 2**63:
        if source == "shen" and not -(1 << 46) <= value < 1 << 46:
            high, low = divmod(value, 65536)
            return f"[wide {high} {low}]"
        return str(value)
    if isinstance(value, list):
        body = " ".join(_term(x, source) for x in value)
        return f"[{body}]" if source == "shen" else f"box({body or 'nil'})"
    raise ValueError("expected integers or lists of integers")


def _directory(example, directory):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", example):
        raise ValueError("invalid example name")
    path = Path(directory) if directory else ROOT.parent / "examples" / example
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest.get("entry") != "specialize" or not all(
        isinstance(manifest.get(k), str) and manifest[k].strip()
        for k in ("static_type", "dynamic_type", "result_type")
    ):
        raise ValueError("invalid example manifest")
    return path.resolve()


def _evaluate(path, source, static, rows):
    if source not in ("shen", "maude"):
        raise ValueError("unknown source runtime")
    fixed = _term(static, source)
    spec = path / f"spec.{source}"
    library = ROOT / f"stage.{source}"
    count = 1 if rows is None else len(rows)
    if source == "shen":
        cmd = [os.environ.get("SHEN_JIT", "shen-jit"), "eval", "-l", str(library),
               "-l", str(spec), "-e", "(set *maximum-print-sequence-size* 1000000)"]
        expressions = [f"(specialize {fixed})"]
        if rows is not None:
            cmd += ["-e", f"(do (set *stage-program* (specialize {fixed})) 0)"]
            expressions = [f"(stage-eval (value *stage-program*) {_term(r, source)})" for r in rows]
        for expression in expressions:
            cmd.extend(["-e", expression])
        request = ""
    else:
        cmd = [os.environ.get("MAUDE", "maude"), "-no-banner", "-no-advise", "-batch",
               str(library), str(spec)]
        entry = f"specialize({fixed})"
        expressions = [entry] if rows is None else [
            f"run({entry},{_term(r, source)})" for r in rows]
        request = "".join(f"red in SPEC : {e} .\n" for e in expressions) + "quit\n"
    if source == "shen" and sum(len(x) for x in cmd) > 60000:
        # Keep large data vectors out of platform-specific argv limits.
        with tempfile.TemporaryDirectory() as temp:
            driver = Path(temp) / "evaluate.shen"
            driver.write_text("\n".join(expressions) + "\n")
            first = len(cmd) - 2 * len(expressions)
            # The static value can itself exceed the argument limit.
            if rows is not None:
                driver.write_text(cmd[first-1] + "\n" + driver.read_text())
                first -= 2
            cmd = cmd[:first] + ["-l", str(driver)]
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
    else:
        proc = subprocess.run(cmd, input=request, text=True, capture_output=True, timeout=120)
    if proc.returncode or proc.stderr.strip():
        raise ValueError(f"{source} evaluation failed: {proc.stderr[-3000:]}\n{proc.stdout[-1000:]}")
    if source == "shen":
        forms = [line.strip() for line in proc.stdout.splitlines() if line.startswith("[")]
    else:
        forms = re.findall(r"^result \w+: (.*?)(?=^={4,}|^Bye\.|\Z)", proc.stdout,
                           re.MULTILINE | re.DOTALL)
    if len(forms) != count:
        raise ValueError(f"expected {count} {source} results: {proc.stdout[-2000:]}")
    return [_parse(f.strip(), source) for f in forms]


def concrete(example, source, static, rows, *, directory=None):
    path = _directory(example, directory)
    rows = list(rows)
    outputs = []
    for start in range(0, len(rows), 32):
        outputs.extend(_evaluate(path, source, static, rows[start:start + 32]))
    return outputs


def build(example, source, static, binary, *, emit_ir=None, emit_c=None, directory=None):
    from .backend import generate, normalize
    path = _directory(example, directory)
    program, = _evaluate(path, source, static, None)
    program = normalize(program)
    code = generate(program)
    binary = Path(binary)
    with tempfile.TemporaryDirectory(dir=binary.parent) as work:
        temp = Path(work) / "program"
        subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-O2", "-x", "c",
                        "-", "-o", str(temp)], input=code, text=True, check=True, timeout=60)
        os.replace(temp, binary)
    if emit_ir:
        Path(emit_ir).write_text(json.dumps(program, indent=2) + "\n")
    if emit_c:
        Path(emit_c).write_text(code)
    return program
