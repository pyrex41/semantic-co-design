"""Specialize first-order Shen folds with a known argument and a dynamic input list.

Supported fold entries have an empty-list clause and a cons clause with a
direct tail call. Helpers can recurse over finite, statically shaped lists.
"""
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Call:
    terms: tuple


@dataclass(frozen=True)
class Cons:
    head: object
    tail: object


@dataclass(frozen=True)
class Expr:
    ir: object


def sequence(items, tail=None):
    for item in reversed(items):
        tail = Cons(item, tail)
    return tail


def parse(source):
    tokens = re.findall(r"[()\[\]|]|[^\s()\[\]|]+", source)
    pos = 0

    def read():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token not in ("(", "["):
            return int(token) if token.lstrip("-").isdigit() else token
        end = ")" if token == "(" else "]"
        items = []
        tail = None
        while tokens[pos] != end:
            if tokens[pos] == "|":
                pos += 1
                tail = read()
                if tokens[pos] != "]" or token != "[":
                    raise ValueError("invalid list tail")
                break
            items.append(read())
        pos += 1
        return Call(tuple(items)) if token == "(" else sequence(items, tail)

    definitions = {}
    while pos < len(tokens):
        form = read().terms
        if form[0] != "define":
            raise ValueError("expected a first-order definition")
        clauses = []
        start = 2
        while start < len(form):
            arrow = form.index("->", start)
            clauses.append((form[start:arrow], form[arrow+1]))
            start = arrow + 2
        definitions[form[1]] = clauses
    return definitions


def bind(pattern, value, env):
    if pattern == "_":
        return True
    if isinstance(pattern, str) and pattern[0].isupper():
        if pattern in env:
            raise ValueError("repeated-variable patterns are unsupported")
        env[pattern] = value
        return True
    if isinstance(value, Expr):
        raise ValueError("helper pattern requires a runtime branch")
    if isinstance(pattern, Cons):
        return isinstance(value, Cons) and bind(pattern.head, value.head, env) and bind(pattern.tail, value.tail, env)
    return pattern == value


def ir(value):
    if isinstance(value, Expr):
        return value.ir
    if type(value) is int:
        return value
    raise ValueError("expected a scalar")


def evaluate(term, env, definitions):
    if isinstance(term, Cons):
        return Cons(evaluate(term.head, env, definitions), evaluate(term.tail, env, definitions))
    if not isinstance(term, Call):
        return env[term] if isinstance(term, str) else term
    name, *args = term.terms
    values = [evaluate(arg, env, definitions) for arg in args]
    primitives = {"+": "add", "-": "sub", "*": "mul", "shen.mod": "mod"}
    if name in primitives:
        return Expr([primitives[name], *(ir(v) for v in values)])
    for patterns, body in definitions[name]:
        local = {}
        if len(patterns) == len(values) and all(bind(p,v,local) for p,v in zip(patterns,values)):
            return evaluate(body, local, definitions)
    raise ValueError(f"no matching clause for {name}")


INPUT = object()


def derive(source, entry, arguments):
    definitions = parse(Path(source).read_text())
    base, step = definitions[entry]
    empty_params, answer = base
    params, recur = step
    index = arguments.index(INPUT)
    if (len(params) != len(arguments) or len(empty_params) != len(params)
            or empty_params[index] is not None or not isinstance(params[index],Cons)
            or not isinstance(recur,Call) or recur.terms[0] != entry
            or len(recur.terms) != len(params)+1 or recur.terms[index+1] != params[index].tail):
        raise ValueError("expected structural tail recursion on the dynamic list")
    if not isinstance(params[index].head, str) or not isinstance(params[index].tail, str):
        raise ValueError("the dynamic list needs simple head and tail bindings")
    env, final_env = {}, {}
    initial, updates = [], []
    cursor = len(params)
    for n,(param,value) in enumerate(zip(params,arguments)):
        if n == index:
            bind(param.head, Expr(["at",["var",cursor]]), env)
            continue
        if recur.terms[n+1] == param:
            bound = value
        else:
            bound = Expr(["var",n])
            initial.append(["set",n,ir(value)])
            updates.append(n)
        if not bind(param,bound,env) or not bind(empty_params[n],bound,final_env):
            raise ValueError("unsupported scalar argument pattern")
    # Evaluate every next argument against the old frame (simultaneous tail call).
    body = [["set",cursor+1+n,ir(evaluate(recur.terms[n+1],env,definitions))] for n in updates]
    body.extend(["set",n,["var",cursor+1+n]] for n in updates)
    body.append(["set",cursor,["add",["var",cursor],1]])
    return ["seq", *initial, ["while",["lt",["var",cursor],["len"]],["seq",*body]],
            ["emit",ir(evaluate(answer,final_env,definitions))]]


def static(value):
    return sequence([static(item) for item in value]) if isinstance(value,list) else value


if __name__ == "__main__":
    import argparse
    import json
    from specializer.backend import generate, normalize

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("entry")
    parser.add_argument("arguments", help='JSON array; null marks the dynamic input list')
    parser.add_argument("--emit", choices=("ir", "c"), default="ir")
    args = parser.parse_args()
    values = [INPUT if x is None else static(x) for x in json.loads(args.arguments)]
    if values.count(INPUT) != 1:
        parser.error("expected exactly one dynamic input list")
    program = normalize(derive(args.source, args.entry, values))
    print(generate(program) if args.emit == "c" else json.dumps(program, indent=2))
