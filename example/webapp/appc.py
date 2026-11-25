"""Interpret App Shen or specialize its manifest entries to native functions."""
import argparse
import hashlib
from importlib.metadata import version
import json
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from values import PURE, Problem, same


LANGUAGE = "app-shen/1"


class Symbol(str):
    pass


class Call(tuple):
    pass


class ListExpr(tuple):
    pass


class Variable(str):
    pass


class SourceError(ValueError):
    pass


TOKEN = re.compile(r'\s+|\\\\[^\n]*|"(?:""|[^"])*"|[()\[\]]|[^()\[\]\s"]+')


def read_program(path):
    raw = path.read_text()
    tokens = []
    seen, line = 0, 1
    for match in TOKEN.finditer(raw):
        if match.start() != seen:
            raise SourceError(f"{path}:{line}: invalid source token")
        token = match.group()
        if not token.isspace() and not token.startswith("\\\\"):
            tokens.append((token, line))
        seen = match.end()
        line += token.count("\n")
    if seen != len(raw):
        raise SourceError(f"{path}:{line}: invalid source token")
    pos = 0

    def read():
        nonlocal pos
        if pos == len(tokens):
            raise SourceError(f"{path}: unexpected end of source")
        token, line = tokens[pos]
        pos += 1
        if token in ("(", "["):
            end = ")" if token == "(" else "]"
            result = []
            while pos < len(tokens) and tokens[pos][0] != end:
                result.append(read())
            if pos == len(tokens):
                raise SourceError(f"{path}:{line}: missing {end}")
            pos += 1
            return (Call if token == "(" else ListExpr)(result)
        if token in (")", "]"):
            raise SourceError(f"{path}:{line}: unexpected {token}")
        if token.startswith('"'):
            return token[1:-1].replace('""', '"')
        if re.fullmatch(r"-?\d+", token):
            integer = int(token)
            if not -(2**63) <= integer < 2**63:
                raise SourceError(f"{path}:{line}: integer outside signed 64-bit range")
            return integer
        if token in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[token]
        return Symbol(token)

    definitions = {}
    locations = {}
    while pos < len(tokens):
        _, line = tokens[pos]
        form = read()
        if not isinstance(form, Call) or len(form) < 4 or form[0] != "define":
            raise SourceError(f"{path}:{line}: expected a function definition")
        try:
            _, name, *rules = form
            clauses = []
            while rules:
                arrow = rules.index(Symbol("->"))
                pattern = rules[:arrow]
                if any(isinstance(term, (Call, ListExpr)) for term in pattern):
                    raise ValueError("only scalar and variable patterns are supported")
                variables = [term for term in pattern if is_variable(term)]
                if len(set(variables)) != len(variables):
                    raise ValueError("repeated variables in a pattern")
                clauses.append((pattern, rules[arrow + 1]))
                rules = rules[arrow + 2:]
            if name in definitions or len({len(p) for p, _ in clauses}) != 1:
                raise ValueError("duplicate definition or inconsistent arity")
            definitions[name] = clauses
            locations[name] = f"{path.name}:{line}"
        except (ValueError, IndexError) as exc:
            raise SourceError(f"{path}:{line}: {exc}") from exc
    return definitions, locations


def is_variable(value):
    return isinstance(value, Symbol) and value[0].isupper()


def literal(value):
    return str(value) if isinstance(value, Symbol) else value


class Evaluator:
    def __init__(self, definitions, locations, capabilities):
        self.definitions = definitions
        self.locations = locations
        self.capabilities = capabilities

    def validate(self):
        def check(term, scope):
            if is_variable(term):
                if term not in scope:
                    raise SourceError(f"unbound variable {term}")
                return
            if isinstance(term, ListExpr):
                for value in term:
                    check(value, scope)
                return
            if not isinstance(term, Call):
                return
            if not term or not isinstance(term[0], Symbol):
                raise SourceError("expected a named first-order call")
            name, *args = term
            if name == "let":
                if len(args) != 3 or not is_variable(args[0]):
                    raise SourceError("invalid let")
                check(args[1], scope)
                check(args[2], scope | {args[0]})
                return
            special = {"if": 3, "and": 2, "or": 2, "do": 2}
            if name in special:
                arity = special[name]
            elif name in PURE:
                arity = PURE[name][0]
            elif name in self.capabilities:
                arity = self.capabilities[name]
            elif name in self.definitions:
                arity = len(self.definitions[name][0][0])
            else:
                raise SourceError(f"undefined function {name}")
            if len(args) != arity:
                raise SourceError(f"{name} expects {arity} arguments, received {len(args)}")
            for arg in args:
                check(arg, scope)

        for name, clauses in self.definitions.items():
            try:
                for pattern, body in clauses:
                    check(body, {p for p in pattern if is_variable(p)})
            except SourceError as exc:
                raise SourceError(f"{self.locations[name]}: {name}: {exc}") from exc

    def eval(self, term, env, ctx):
        if isinstance(term, ListExpr):
            return [self.eval(x, env, ctx) for x in term]
        if not isinstance(term, Call):
            return env[term] if is_variable(term) else literal(term)
        name, *args = term
        if name == "let":
            return self.eval(args[2], {**env, args[0]: self.eval(args[1], env, ctx)}, ctx)
        if name in ("if", "and", "or"):
            value = self.eval(args[0], env, ctx)
            if name == "and":
                return bool(value) and bool(self.eval(args[1], env, ctx))
            if name == "or":
                return bool(value) or bool(self.eval(args[1], env, ctx))
            return self.eval(args[1 if value else 2], env, ctx)
        if name == "do":
            self.eval(args[0], env, ctx)
            return self.eval(args[1], env, ctx)
        values = [self.eval(x, env, ctx) for x in args]
        if name in PURE:
            return PURE[name][1](*values)
        if name in self.capabilities:
            return ctx.effect(name, *values)
        return self.run(name, values, ctx)

    def run(self, name, values, ctx):
        for patterns, term in self.definitions[name]:
            scope = {}
            for pattern, value in zip(patterns, values):
                if is_variable(pattern):
                    scope[pattern] = value
                elif pattern != "_" and not same(literal(pattern), value):
                    break
            else:
                return self.eval(term, scope, ctx)
        raise SourceError(f"{self.locations[name]}: no matching clause for {name}")


DYNAMIC = object()


class Specializer:
    """Eliminate known clauses/branches and emit direct native residual calls."""
    def __init__(self, evaluator, pure_only=False):
        self.e = evaluator
        self.pure_only = pure_only
        self.instances = {}
        self.pending = []
        self.serial = 0

    def variable(self):
        self.serial += 1
        return Variable(f"v{self.serial}")

    def instance(self, name, args):
        # Keep finite literal tags and booleans. Generalize other runtime shapes.
        signature = tuple(
            value if type(value) in (bool, type(None))
            or (type(value) is str and len(value) <= 16000) else DYNAMIC for value in args
        )
        key = name, tuple((type(x), x) for x in signature)
        if key not in self.instances:
            if len(self.instances) >= 1800:
                raise SourceError("specialization exceeded 1800 residual functions")
            label = f"f{len(self.instances)}"
            self.instances[key] = label
            self.pending.append((name, signature, label))
        return self.instances[key], [v for v, s in zip(args, signature) if s is DYNAMIC]

    @staticmethod
    def expression(value):
        return str(value) if isinstance(value, Variable) else repr(value)

    def lower(self, term, env, output, prefix):
        def bind(expression):
            var = self.variable()
            output.append(f"{prefix}{var} = {expression}")
            return var

        if isinstance(term, ListExpr):
            items = [self.lower(item, env, output, prefix) for item in term]
            if all(not isinstance(x, Variable) for x in items):
                return items
            return bind("[" + ", ".join(self.expression(x) for x in items) + "]")
        if not isinstance(term, Call):
            return env[term] if is_variable(term) else literal(term)
        name, *args = term
        if name == "let":
            value = self.lower(args[1], env, output, prefix)
            return self.lower(args[2], {**env, args[0]: value}, output, prefix)
        if name == "do":
            self.lower(args[0], env, output, prefix)
            return self.lower(args[1], env, output, prefix)
        if name in ("if", "and", "or"):
            if name == "and":
                args = [args[0], Call((Symbol("truth"), args[1])), False]
            elif name == "or":
                args = [args[0], True, Call((Symbol("truth"), args[1]))]
            test = self.lower(args[0], env, output, prefix)
            if not isinstance(test, Variable):
                return self.lower(args[1 if test else 2], env, output, prefix)
            result = self.variable()
            for head, branch in zip((f"if {test}:", "else:"), args[1:]):
                output.append(prefix + head)
                value = self.lower(branch, env, output, prefix + "    ")
                output.append(f"{prefix}    {result} = {self.expression(value)}")
            return result
        values = [self.lower(arg, env, output, prefix) for arg in args]
        if name in PURE:
            if name != "failure" and all(not isinstance(x, Variable) for x in values):
                try:
                    value = PURE[name][1](*values)
                    if type(value) in (str, bool, type(None), int, list, dict):
                        return value
                except (Problem, ValueError, TypeError, KeyError, ArithmeticError):
                    pass
            arguments = ", ".join(self.expression(x) for x in values)
            return bind(f"pure[{str(name)!r}]({arguments})")
        if name in self.e.capabilities:
            if self.pure_only:
                raise SourceError(f"isolated entry reaches effect {name}")
            arguments = "".join(", " + self.expression(x) for x in values)
            return bind(f"ctx.effect({str(name)!r}{arguments})")
        label, dynamic = self.instance(name, values)
        arguments = "".join(", " + self.expression(x) for x in dynamic)
        return bind(f"{label}(ctx{arguments})")

    def generate(self, entries):
        lines = ["# Generated. Edit the application specification.", "from values import functions as pure, same"]
        wrappers = {}
        for index, (name, entry) in enumerate(entries.items()):
            label, _ = self.instance(entry["function"], [*entry.get("static", []), DYNAMIC])
            wrapper = f"entry{index}"
            wrappers[name] = wrapper
            lines.extend([f"def {wrapper}(ctx, arg):", f"    return {label}(ctx, arg)"])
        mapping = {name: [] for name in self.e.definitions}
        for name, signature, label in self.pending:
            params = [Variable(f"p{i}") if value is DYNAMIC else value for i, value in enumerate(signature)]
            formals = "".join(f", object {p}" for p in params if isinstance(p, Variable))
            lines.append(f"cdef object {label}(object ctx{formals}):")
            mapping[name].append({"function": label, "line": len(lines), "static": [
                None if value is DYNAMIC else {"value": value} for value in signature
            ]})
            for patterns, body in self.e.definitions[name]:
                scope = {}
                guards = []
                for pattern, value in zip(patterns, params):
                    if is_variable(pattern):
                        scope[pattern] = value
                    elif pattern == "_":
                        continue
                    elif isinstance(value, Variable):
                        guards.append(f"same({value}, {literal(pattern)!r})")
                    elif not same(literal(pattern), value):
                        break
                else:
                    indent = "    "
                    if guards:
                        lines.append(indent + "if " + " and ".join(guards) + ":")
                        indent += "    "
                    result = self.lower(body, scope, lines, indent)
                    lines.append(indent + f"return {self.expression(result)}")
                    if not guards:
                        break
            else:
                lines.append(f"    raise ValueError({('no matching clause for ' + name)!r})")
        lines.append("entries = {" + ", ".join(f"{n!r}: {v}" for n, v in wrappers.items()) + "}")
        output = "\n".join(lines) + "\n"
        if len(output.encode()) > 5_000_000:
            raise SourceError("residual source exceeds 5 MB")
        return output, mapping


def load(manifest):
    release = json.loads(manifest.read_text())
    if release.get("language") != LANGUAGE:
        raise SourceError(f"{manifest}: expected language {LANGUAGE!r}")
    definitions, locations = {}, {}
    for name in release["sources"]:
        defs, locs = read_program(manifest.parent / name)
        if defs.keys() & definitions.keys():
            raise SourceError(f"{name}: duplicate function")
        definitions.update(defs)
        locations.update(locs)
    evaluator = Evaluator(definitions, locations, release["capabilities"])
    evaluator.validate()
    for name, entry in release["entries"].items():
        if entry["function"] not in definitions:
            raise SourceError(f"{name}: missing definition {entry['function']}")
        expected = len(definitions[entry["function"]][0][0])
        if 1 + len(entry.get("static", [])) != expected:
            raise SourceError(f"{name}: entry argument count differs from {expected}")
        if any(type(x) not in (str, bool, type(None)) for x in entry.get("static", [])):
            raise SourceError(f"{name}: unsupported static entry argument")
    return release, evaluator


def reference_entries(release, evaluator):
    def bind(entry):
        def invoke(ctx, arg):
            return evaluator.run(entry["function"], [*entry.get("static", []), arg], ctx)
        return invoke
    return {name: bind(entry) for name, entry in release["entries"].items()}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_native(source, output):
    source = source.resolve()
    output = output.resolve()
    c_file = source.with_suffix(".c")
    subprocess.run([sys.executable, "-m", "cython", "-3", source.name],
                   cwd=source.parent, check=True, timeout=90)
    linker = []
    if sys.platform == "darwin":
        linker = ["-undefined", "dynamic_lookup", f"-Wl,-install_name,@rpath/{output.name}"]
    subprocess.run([
        "cc", "-O2", "-fPIC", "-shared", *linker, "-I" + sysconfig.get_path("include"),
        c_file.name, "-o", str(output)
    ], cwd=source.parent, check=True, timeout=120)


def build_policy(out, here, name, source, native_name):
    policy_dir = out / "policy-lib"
    policy_dir.mkdir()
    native = policy_dir / native_name
    residual = policy_dir / "_application.pyx"
    residual.write_text(source)
    compile_native(residual, native)
    for driver in ("values.py", "policy_host.py"):
        shutil.copyfile(here / driver, policy_dir / driver)
    (policy_dir / "entry.json").write_text(json.dumps(name) + "\n")
    policy_launcher = out / "policy"
    policy_launcher.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "sys.dont_write_bytecode = True\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'policy-lib'))\n"
        "from policy_host import main\n"
        "from _application import entries\n"
        "main(entries)\n"
    )
    policy_launcher.chmod(0o755)
    return native


def build(manifest, out):
    release, evaluator = load(manifest)
    processes = release.get("processes", {})
    policy_entry = processes.get("policy")
    server_entries = {name: entry for name, entry in release["entries"].items() if name != policy_entry}
    source, mapping = Specializer(evaluator).generate(server_entries)
    policy_source = None
    policy_mapping = {}
    if policy_entry is not None:
        policy = {policy_entry: release["entries"][policy_entry]}
        policy_source, policy_mapping = Specializer(evaluator, pure_only=True).generate(policy)
    out.mkdir(parents=True, exist_ok=False)
    here = Path(__file__).resolve().parent
    driver_dir = out / "lib"
    driver_dir.mkdir()
    native = driver_dir / ("_application" + sysconfig.get_config_var("EXT_SUFFIX"))
    residual = out / "_application.pyx"
    residual.write_text(source)
    compile_native(residual, native)
    driver_names = ["values.py", "host.py", "drivers.py"] if "routes" in release else ["values.py"]
    digest_names = list(driver_names)
    if policy_source is not None:
        policy_native = build_policy(out, here, policy_entry, policy_source, native.name)
        digest_names.append("policy_host.py")
    for name in driver_names:
        shutil.copyfile(here / name, driver_dir / name)
    for name in release.get("assets", []):
        shutil.copyfile(manifest.parent / name, driver_dir / name)
    (driver_dir / "manifest.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    if "routes" in release:
        for command in ("serve", "worker", "admin"):
            if command != "serve" and command not in processes:
                continue
            launcher = out / command
            launcher.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                "sys.dont_write_bytecode = True\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))\n"
                "from host import native_main\n"
                f"native_main({command!r})\n"
            )
            launcher.chmod(0o755)
    for name, asset in release.get("launchers", {}).items():
        launcher = out / name
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) or launcher.exists():
            raise SourceError(f"invalid or duplicate launcher name {name}")
        if asset not in release.get("assets", []) or Path(asset).name != asset or not asset.endswith(".py"):
            raise SourceError(f"launcher {name} requires a declared Python asset basename")
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import runpy, sys\n"
            "sys.dont_write_bytecode = True\n"
            "root = Path(__file__).resolve().parent / 'lib'\n"
            "sys.path.insert(0, str(root))\n"
            f"runpy.run_path(str(root / {asset!r}), run_name='__main__')\n"
        )
        launcher.chmod(0o755)
    digests = {
        "manifest": digest(manifest),
        "source": {name: digest(manifest.parent / name) for name in release["sources"]},
        "compiler": digest(Path(__file__)),
        "drivers": {name: digest(here / name) for name in digest_names},
        "assets": {name: digest(manifest.parent / name) for name in release.get("assets", [])},
        "native": digest(native),
        "compiler_dependencies": digest(here / "requirements-build.txt"),
    }
    if policy_source is not None:
        digests["policy_native"] = digest(policy_native)
    (out / "ownership.json").write_text(json.dumps({
        "entries": release["entries"],
        "language": LANGUAGE,
        "toolchain": {
            "python": platform.python_version(), "cython": version("Cython"),
            "cc": subprocess.check_output(["cc", "--version"], text=True, timeout=10).splitlines()[0],
        },
        "definitions": {
            name: {"source": evaluator.locations[name], "residual": instances, "policy": policy_mapping.get(name, [])}
            for name, instances in mapping.items()
        },
        "capabilities": release["capabilities"],
        "digests": digests,
        "abi": {
            "version": 1, "python": sysconfig.get_config_var("SOABI"),
            "platform": platform.system(), "machine": platform.machine(),
        }
    }, indent=2, sort_keys=True) + "\n")


def package_files(release):
    owned = {}
    for item in sorted(release.rglob("*")):
        path = item.relative_to(release).as_posix()
        if item.is_symlink():
            raise SourceError(f"symbolic link in release: {path}")
        if item.is_file() and path not in {"package.json", "package.sig"}:
            owned[path] = digest(item)
    if "ownership.json" not in owned:
        raise SourceError("missing ownership receipt")
    return owned


def seal(release, key):
    inventory, signature = release / "package.json", release / "package.sig"
    if signature.exists() or signature.is_symlink():
        raise SourceError("signature already exists")
    data = json.dumps({"sha256": package_files(release)}, sort_keys=True, indent=2) + "\n"
    with inventory.open("x") as stream:
        stream.write(data)
    try:
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key),
                        "-out", str(signature), str(inventory)], check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        inventory.unlink()
        signature.unlink(missing_ok=True)
        raise


def verify(release, key):
    inventory, signature = release / "package.json", release / "package.sig"
    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key),
                    "-signature", str(signature), str(inventory)], check=True, timeout=30)
    if json.loads(inventory.read_text()) != {"sha256": package_files(release)}:
        raise SourceError("release inventory does not match the signed receipt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    compile_parser = sub.add_parser("build")
    compile_parser.add_argument("manifest", type=Path)
    compile_parser.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("manifest", type=Path)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("release", type=Path)
    signing = sub.add_parser("seal")
    signing.add_argument("release", type=Path)
    signing.add_argument("--private-key", type=Path, required=True)
    verification = sub.add_parser("verify")
    verification.add_argument("release", type=Path)
    verification.add_argument("--public-key", type=Path, required=True)
    interpret = sub.add_parser("interpret")
    interpret.add_argument("manifest", type=Path)
    interpret.add_argument("service", choices=("serve", "worker", "admin", "policy"))
    interpret.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "build":
        build(args.manifest, args.out)
    elif args.action == "inspect":
        print((args.release / "ownership.json").read_text(), end="")
    elif args.action == "check":
        load(args.manifest)
    elif args.action == "seal":
        seal(args.release, args.private_key)
    elif args.action == "verify":
        verify(args.release, args.public_key)
        print("Release inventory verified.")
    else:
        manifest, evaluator = load(args.manifest)
        entries = reference_entries(manifest, evaluator)
        if args.service == "policy":
            from policy_host import run
            if args.args:
                parser.error("policy takes its input on standard input")
            run(entries[manifest["processes"]["policy"]])
        else:
            import host
            host.main(args.service, manifest, entries, args.args, args.manifest.resolve().parent)
