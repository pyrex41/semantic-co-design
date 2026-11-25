"""The same rules in Maude.

Two parts, with different standing:

* `step_module` translates the Shen source of a node clause by clause into a
  functional module. Clause order is kept by a chain of `owise` equations. It is
  a mechanical translation of the text the compiler read, and `reduce` checks it
  against shen-jit on concrete states and events.
* `environment_module` is a hand-written model of the simulator's untimed
  environment (delivery, disk sync, crash with a persisted prefix of pending
  writes, restart, drop, duplicate), instantiated with the manifest's roles.
  It mirrors tool/sim by construction only; `search` and `tool.sim.explore`
  are compared on their reachable observation sets.
"""
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from .pe import PRELUDE_SOURCE, Call, Cons, Text, Unsupported, is_variable, parse

VALUES = """fmod SHEN-VALUE is
  protecting INT .
  protecting QID .
  protecting EXT-BOOL .
  sort Val .
  op i : Int -> Val [ctor] .
  op q : Qid -> Val [ctor] .
  op b : Bool -> Val [ctor] .
  op nil : -> Val [ctor] .
  op cons : Val Val -> Val [ctor] .
  op int : Val ~> Int .
  op bool : Val ~> Bool .
  op shen.error : ~> Val .
  eq int(i(N:Int)) = N:Int .
  eq bool(b(B:Bool)) = B:Bool .
  --- A Shen error is a term with no normal form of sort Val, and every operator below
  --- is strict, so an argument that is an error leaves the whole term stuck.
  --- Integers are exact from -2^61 to 2^61 - 1 and arithmetic outside that is an error.
  op num : Int ~> Val .
  ceq num(N:Int) = i(N:Int)
   if N:Int >= -2305843009213693952 /\\ N:Int <= 2305843009213693951 .
  --- shen.mod as the kernel defines it. A when B > A. Otherwise B must be positive and
  --- the first B * 2^k above A must be exact, and the answer is the remainder.
  op top : Int Int -> Int .
  eq top(A:Int, M:Int) = if M:Int > A:Int then M:Int else top(A:Int, 2 * M:Int) fi .
  op smod : Int Int ~> Int .
  ceq smod(A:Int, B:Int) = A:Int if B:Int > A:Int .
  ceq smod(A:Int, B:Int) = A:Int rem B:Int
   if B:Int > 0 /\\ B:Int <= A:Int /\\ top(A:Int, B:Int) <= 2305843009213693951 .
  --- Division where the quotient is an integer. Shen answers an inexact one with a float.
  op sdiv : Int Int ~> Int .
  ceq sdiv(A:Int, B:Int) = A:Int quo B:Int if B:Int =/= 0 /\\ A:Int rem B:Int == 0 .
  op same : Val Val -> Val .
  eq same(X:Val, Y:Val) = b(X:Val == Y:Val) .
endfm
"""
ARITHMETIC = {"+": "int({}) + int({})", "-": "int({}) - int({})", "*": "int({}) * int({})",
              "/": "sdiv(int({}), int({}))", "shen.mod": "smod(int({}), int({}))"}
COMPARE = {"<": "<", ">": ">", "<=": "<=", ">=": ">="}


def literal(value):
    if value is None:
        return "nil"
    if type(value) is bool:
        return f"b({'true' if value else 'false'})"
    if type(value) is int:
        return f"i({value})"
    if type(value) is str:
        return f"q('{value})"
    raise Unsupported(f"no Maude term for {value!r}")


def term(value):
    """A simulator value (nested tuples) as a Maude term."""
    if isinstance(value, (list, tuple)):
        out = "nil"
        for item in reversed(value):
            out = f"cons({term(item)}, {out})"
        return out
    return literal(value)


class Translator:
    def __init__(self):
        self.ops, self.equations, self.lets = [], [], 0

    def pattern(self, pattern, fresh):
        if pattern == "_" and type(pattern) is str:
            # A Shen variable starts with an upper-case letter, so this name is free.
            fresh.append(f"w{len(fresh)}:Val")
            return fresh[-1]
        if is_variable(pattern):
            return f"{pattern}:Val"
        if isinstance(pattern, Cons):
            return f"cons({self.pattern(pattern.head, fresh)}, {self.pattern(pattern.tail, fresh)})"
        return literal(pattern)

    def variables(self, pattern, out):
        if isinstance(pattern, Cons):
            self.variables(pattern.head, out)
            self.variables(pattern.tail, out)
        elif is_variable(pattern) and pattern not in out:
            out.append(pattern)
        return out

    def expr(self, tree, scope):
        if isinstance(tree, Cons):
            return f"cons({self.expr(tree.head, scope)}, {self.expr(tree.tail, scope)})"
        if is_variable(tree):
            return f"{tree}:Val"
        if isinstance(tree, Text):
            raise Unsupported("strings")
        if not isinstance(tree, Call):
            return literal(tree)
        name, *args = tree.terms
        if name in ("simple-error", "error"):
            return "shen.error"
        if name == "let":
            self.lets += 1
            op = f"let.{self.lets}"
            rest = [v for v in scope if v != args[0]]
            self.ops.append(f"  op {op} : {'Val ' * (len(rest) + 1)}-> Val .")
            self.equations.append(
                f"  eq {op}({', '.join(f'{v}:Val' for v in [args[0], *rest])}) = "
                f"{self.expr(args[2], [args[0], *rest])} .")
            return f"{op}({', '.join([self.expr(args[1], scope), *(f'{v}:Val' for v in rest)])})"
        parts = [self.expr(arg, scope) for arg in args]
        if name == "if":
            return f"(if bool({parts[0]}) then {parts[1]} else {parts[2]} fi)"
        if name in ("and", "or"):
            return f"b(bool({parts[0]}) {name}-{'then' if name == 'and' else 'else'} bool({parts[1]}))"
        if name == "not":
            return f"b(not bool({parts[0]}))"
        if name == "=":
            return f"same({parts[0]}, {parts[1]})"
        if name in ARITHMETIC:
            return f"num({ARITHMETIC[name].format(*parts)})"
        if name in COMPARE:
            return f"b(int({parts[0]}) {COMPARE[name]} int({parts[1]}))"
        return f"f.{name}({', '.join(parts)})" if parts else f"f.{name}"

    def define(self, name, clauses):
        arity = len(clauses[0][0])
        sort = f"{'Val ' * arity}~> Val ."
        generic = ", ".join(f"A{n}:Val" for n in range(arity))
        call = (lambda op, inside: f"{op}({inside})") if arity else (lambda op, inside: op)
        self.ops.append(f"  op f.{name} : {sort}")
        self.equations.append(f"  eq {call(f'f.{name}', generic)} = {call(f'f.{name}.0', generic)} .")
        for n, (patterns, guard, body) in enumerate(clauses):
            self.ops.append(f"  op f.{name}.{n} : {sort}")
            wild, scope = [], []
            for pattern in patterns:
                self.variables(pattern, scope)
            matched = ", ".join(self.pattern(p, wild) for p in patterns)
            left = call(f"f.{name}.{n}", matched)
            right = self.expr(body, scope)
            following = call(f"f.{name}.{n + 1}", matched) if n + 1 < len(clauses) else "shen.error"
            if guard is None:
                self.equations.append(f"  eq {left} = {right} .")
            else:
                # Once the patterns match, the guard decides: true takes this clause, false
                # goes on to the next, and a guard that is an error is an error (stuck).
                bound = ", ".join([f"{v}:Val" for v in scope] + wild)
                test = f"g.{name}.{n}"
                self.ops.append(f"  op {test} : {'Val ' * (len(scope) + len(wild) + 1)}~> Val .")
                with_guard = lambda value: f"{test}({', '.join(filter(None, [value, bound]))})"
                self.equations += [
                    f"  eq {left} = {with_guard(self.expr(guard, scope))} .",
                    f"  eq {with_guard('b(true)')} = {right} .",
                    f"  eq {with_guard('b(false)')} = {following} ."]
            if n + 1 < len(clauses):    # the next clause is tried when the patterns fail
                self.equations.append(f"  eq {call(f'f.{name}.{n}', generic)} = "
                                      f"{call(f'f.{name}.{n + 1}', generic)} [owise] .")


def step_module(source):
    """Shen definitions (plus the system functions they may call) as fmod STEP."""
    translator = Translator()
    for name, clauses in {**parse(PRELUDE_SOURCE), **parse(source)}.items():
        translator.define(name, clauses)
    return (VALUES + "fmod STEP is\n  protecting SHEN-VALUE .\n" + "\n".join(translator.ops)
            + "\n" + "\n".join(translator.equations) + "\nendfm\n")


ENVIRONMENT = """mod ENV is
  protecting STEP .
  sorts Thing Soup World PSet .
  subsort Thing < Soup .
  op none : -> Soup [ctor] .
  op __ : Soup Soup -> Soup [ctor assoc comm id: none] .
  op {_} : Soup -> World [ctor] .
  op done : Soup -> World [ctor] .
  op trapped : Int Val Val -> World [ctor] .
  op node : Int Val -> Thing [ctor] .       --- a running node and its state
  op down : Int -> Thing [ctor] .
  op disk : Int Val Val -> Thing [ctor] .   --- durable rows by key, pending writes oldest first
  op ev : Int Val -> Thing [ctor] .         --- a request or message on its way to a node
  op lev : Int Val -> Thing [ctor] .        --- a local event; lost when the node crashes
  op out : Val -> Thing [ctor] .            --- an output on its way to the outside
  ops got dropped lostw : Val -> Thing [ctor] .
  ops rows state : Int Val -> Thing [ctor] .
  op budget : Qid Nat -> Thing [ctor] .
  op noP : -> PSet [ctor] .
  op px : Val -> PSet [ctor] .
  op __ : PSet PSet -> PSet [ctor assoc comm id: noP] .
  vars I J K K' : Int . var N : Nat . vars S S' E T D V W Es Pre Pre2 Rows PW Vs Vs' L Ws : Val .
  vars R R' : Soup . vars PS PS2 : PSet . var X : [Val] .
  eq got(V) got(V) = got(V) .
  eq dropped(V) dropped(V) = dropped(V) .
  eq lostw(V) lostw(V) = lostw(V) .

  op ok : [Val] -> Bool .
  eq ok(cons(S, cons(Es, nil))) = true .
  eq ok(X) = false [owise] .
  op append : Val Val -> Val .
  eq append(nil, L) = L .
  eq append(cons(E, Es), L) = cons(E, append(Es, L)) .
  op after : Val Val -> Val .               --- what is left of a list after a prefix of it
  eq after(nil, L) = L .
  eq after(cons(E, Pre), cons(V, L)) = after(Pre, L) .
  op prefixes : Val -> PSet .               --- proper prefixes
  op allPrefixes : Val -> PSet .
  op push : Val PSet -> PSet .
  eq prefixes(nil) = noP .
  eq prefixes(cons(E, L)) = px(nil) push(E, prefixes(L)) .
  eq push(E, noP) = noP .
  eq push(E, px(L) PS) = px(cons(E, L)) push(E, PS) .
  eq allPrefixes(L) = px(L) prefixes(L) .

  op put : Val Val -> Val .                 --- a row [Key | Value] into rows sorted by key
  eq put(cons(i(K), Vs), nil) = cons(cons(i(K), Vs), nil) .
  eq put(cons(i(K), Vs), cons(cons(i(K'), Vs'), Rows))
   = if K < K' then cons(cons(i(K), Vs), cons(cons(i(K'), Vs'), Rows))
     else if K == K' then cons(cons(i(K), Vs), Rows)
     else cons(cons(i(K'), Vs'), put(cons(i(K), Vs), Rows)) fi fi .
  op putAll : Val Val -> Val .              --- pending writes [Token Key | Value] in order
  eq putAll(nil, Rows) = Rows .
  eq putAll(cons(cons(T, W), Ws), Rows) = putAll(Ws, put(W, Rows)) .
  op lookup : Int Val -> Val .              --- [Found | Value]
  eq lookup(K, nil) = cons(i(0), %MISSING%) .
  eq lookup(K, cons(cons(i(K'), Vs), Rows)) = if K == K' then cons(i(1), Vs) else lookup(K, Rows) fi .
  op lostAll : Val -> Soup .
  eq lostAll(nil) = none .
  eq lostAll(cons(cons(T, W), Ws)) = lostw(W) lostAll(Ws) .
  op untimer : Int Val Soup -> Soup .
  op purge : Int Soup -> Soup .
  eq purge(I, lev(I, E) R) = purge(I, R) .
  eq purge(I, R) = R [owise] .

  op eff : Int Val Soup -> Soup .           --- one effect, by the role its tag has in the manifest
%EFFECTS%
  op app : Int Val Soup -> Soup .           --- effects in order
  eq app(I, nil, R) = R .
  eq app(I, cons(E, Es), R) = app(I, Es, eff(I, E, R)) .

  --- A crash loses memory and local events. A prefix of the pending writes reaches the disk.
  op crash : Int Val Soup -> Soup .
  eq crash(I, Pre, node(I, S) disk(I, Rows, PW) R)
   = down(I) disk(I, putAll(Pre, Rows), nil) lostAll(after(Pre, PW)) purge(I, R) .

%STEPS%
  rl [lost] : { ev(I, E) down(I) R } => { down(I) R } .
  rl [sync] : { node(I, S) disk(I, Rows, cons(cons(T, W), PW)) R }
           => { node(I, S) disk(I, put(W, Rows), PW) %ACK% R } .
  rl [output] : { out(V) R } => { got(V) R } .
  rl [drop-out] : { out(V) budget('drop, s(N)) R } => { dropped(V) budget('drop, N) R } .
  rl [drop-ev] : { ev(I, E) budget('drop, s(N)) R } => { dropped(E) budget('drop, N) R } .
  rl [dup-out] : { out(V) budget('dup, s(N)) R } => { out(V) out(V) budget('dup, N) R } .
  rl [dup-ev] : { ev(I, E) budget('dup, s(N)) R } => { ev(I, E) ev(I, E) budget('dup, N) R } .
  crl [crash] : { node(I, S) disk(I, Rows, PW) budget('crash, s(N)) R }
             => { crash(I, Pre, node(I, S) disk(I, Rows, PW) R) budget('crash, N) }
   if px(Pre) PS := allPrefixes(PW) .

  op obs : Soup -> Soup .                   --- what an outside observer can know
  eq obs(node(I, S) R) = %SEEN% obs(R) .
  eq obs(down(I) R) = %UNSEEN% obs(R) .
  eq obs(ev(I, E) R) = obs(R) .
  eq obs(lev(I, E) R) = obs(R) .
  eq obs(out(V) R) = obs(R) .
  eq obs(budget(Q:Qid, N) R) = obs(R) .
  eq obs(disk(I, Rows, PW) R) = rows(I, Rows) obs(R) .
  eq obs(R) = R [owise] .
  rl [observe] : { R } => done(obs(R)) .

  op boot : Int Soup -> Soup .
%BOOT%
%WORKLOAD%
  op init : -> World .
  eq init = { %INIT% } .
endm
"""


def environment_module(schema, prof, workload):
    """Instantiate the environment for a manifest, an untimed profile and workload rules.

    `workload` is Maude text: declarations and rules over `{ ... }`, plus the equation-free
    term for its initial things under the name given as `workload["init"]`.
    """
    if prof["timed"]:
        raise ValueError("the Maude environment models the untimed profile only")
    q = lambda tag: f"q('{tag})"
    list_of = lambda *items: term(()) if not items else f"cons({items[0]}, {list_of(*items[1:])})"
    roles = schema.roles
    effects = []
    for kind in schema.effects.values():
        tag = q(kind.tag)
        if kind.role == "disk-write":
            effects.append(f"  eq eff(I, cons({tag}, W), disk(I, Rows, PW) R) = "
                           f"disk(I, Rows, append(PW, cons(W, nil))) R .")
        elif kind.role == "disk-read":
            result = q(roles["disk-read-result"].tag)
            effects.append(
                f"  eq eff(I, cons({tag}, cons(T, cons(i(K), nil))), disk(I, Rows, PW) R) = "
                f"lev(I, cons({result}, cons(T, cons(i(K), lookup(K, Rows))))) disk(I, Rows, PW) R .")
        elif kind.role == "output":
            effects.append(f"  eq eff(I, cons({tag}, Vs), R) = out(cons({tag}, Vs)) R .")
        elif kind.role == "send":
            effects.append(f"  eq eff(I, cons({tag}, cons(i(J), Vs)), R) = "
                           f"ev(J, cons({q(kind.deliver)}, cons(i(I), Vs))) R .")
        elif kind.role in ("timer-set", "timer-cancel"):
            timer = f"lev(I, cons({q(roles['timer'].tag)}, cons(T, nil)))"
            if kind.role == "timer-set":
                effects.append(f"  eq eff(I, cons({tag}, cons(T, cons(D, nil))), R) = "
                               f"{timer} untimer(I, T, R) .")
            else:
                effects.append(f"  eq eff(I, cons({tag}, cons(T, nil)), R) = untimer(I, T, R) .")
        elif kind.role == "clock-read":
            effects.append(f"  eq eff(I, cons({tag}, cons(T, nil)), R) = lev(I, cons("
                           f"{q(roles['clock'].tag)}, cons(T, cons(i(0), nil)))) R .")
    if "timer" in roles:
        timer = f"lev(I, cons({q(roles['timer'].tag)}, cons(T, nil)))"
        effects += [f"  eq untimer(I, T, {timer} R) = untimer(I, T, R) .",
                    "  eq untimer(I, T, R) = R [owise] ."]

    step = lambda state, event: f"f.{schema.step}({state}, {event})"
    init = lambda node: f"f.{schema.init}(i({node}), i({prof['nodes']}))"
    restart = list_of(q(roles["restart"].tag)) if "restart" in roles else None
    steps = []

    def delivery(label, taken, state, event, rest, when=""):
        """A step from `taken`, with and without a crash between two of its effects."""
        result = f"cons(S', cons(Es, nil)) := {step(state, event)}{when}"
        steps.append(f"  crl [{label}] : {{ {taken} R }} => {{ app(I, Es, node(I, S') {rest}) }}\n"
                     f"   if {result} .")
        steps.append(
            f"  crl [{label}-crash] : {{ {taken} budget('crash, s(N)) R }}\n"
            f"   => {{ crash(I, Pre2, node(I, S) disk(I, Rows, PW) R') budget('crash, N) }}\n"
            f"   if {result} /\\ px(Pre) PS := prefixes(Es)\n"
            f"   /\\ node(I, S) disk(I, Rows, PW) R' := app(I, Pre, node(I, {state}) {rest})\n"
            f"   /\\ px(Pre2) PS2 := allPrefixes(PW) .")

    delivery("deliver", "ev(I, E) node(I, S)", "S", "E", "R")
    if "timer" in prof["budget"] and "timer" in roles:
        # A bound on timer events: a timer is delivered only while the budget lasts.
        effects += ["  op timer? : Val -> Bool .",
                    f"  eq timer?(cons({q(roles['timer'].tag)}, Vs)) = true .",
                    "  eq timer?(E) = false [owise] ."]
        delivery("local", "lev(I, E) node(I, S)", "S", "E", "R", " /\\ timer?(E) = false")
        delivery("timer", "lev(I, E) node(I, S) budget('timer, s(M:Nat))", "S", "E",
                 "budget('timer, M:Nat) R", " /\\ timer?(E) = true")
    else:
        delivery("local", "lev(I, E) node(I, S)", "S", "E", "R")
    for label, taken in (("trap", "ev(I, E) node(I, S)"), ("trap-local", "lev(I, E) node(I, S)")):
        steps.append(f"  crl [{label}] : {{ {taken} R }} => trapped(I, S, E) "
                     f"if not ok({step('S', 'E')}) .")
    if restart:
        steps.append(f"  crl [restart] : {{ down(I) R }} => {{ app(I, Es, node(I, S') R) }}\n"
                     f"   if cons(S', cons(Es, nil)) := {step('f.' + schema.init + '(i(I), i(' + str(prof['nodes']) + '))', restart)} .")
        steps.append(
            f"  crl [restart-crash] : {{ down(I) budget('crash, s(N)) R }}\n"
            f"   => {{ crash(I, Pre2, node(I, S) disk(I, Rows, PW) R') budget('crash, N) }}\n"
            f"   if cons(S', cons(Es, nil)) := {step('f.' + schema.init + '(i(I), i(' + str(prof['nodes']) + '))', restart)}\n"
            f"   /\\ px(Pre) PS := prefixes(Es)\n"
            f"   /\\ node(I, S) disk(I, Rows, PW) R' := app(I, Pre, node(I, f.{schema.init}(i(I), i({prof['nodes']}))) R)\n"
            f"   /\\ px(Pre2) PS2 := allPrefixes(PW) .")
        boot = (f"  ceq boot(I, node(I, S) R) = app(I, Es, node(I, S') R)\n"
                f"   if cons(S', cons(Es, nil)) := {step('S', restart)} .")
    else:
        steps.append(f"  rl [restart] : {{ down(I) R }} => {{ node(I, f.{schema.init}(i(I), "
                     f"i({prof['nodes']}))) R }} .")
        boot = "  eq boot(I, R) = R ."
    ack = (f"lev(I, cons({q(roles['disk-ack'].tag)}, cons(T, nil)))" if "disk-ack" in roles
           else "none")
    missing = (term(schema.default(roles["disk-read-result"].fields[3:]))
               if "disk-read-result" in roles else "nil")
    soup = " ".join([*(f"node({n}, {init(n)}) disk({n}, nil, nil)" for n in range(prof["nodes"])),
                     *(f"budget('{name}, {prof['budget'][name]})" for name in ("crash", "drop", "dup")),
                     *([f"budget('timer, {prof['budget']['timer']})"] if "timer" in prof["budget"]
                       else []),
                     workload["init"]])
    for node in range(prof["nodes"]):
        soup = f"boot({node}, {soup})"
    out = ENVIRONMENT
    for marker, text in (("EFFECTS", "\n".join(effects)), ("STEPS", "\n".join(steps)),
                         ("ACK", ack), ("MISSING", missing), ("BOOT", boot),
                         ("SEEN", "state(I, S)" if prof["observe_states"] else "none"),
                         ("UNSEEN", "state(I, q('down))" if prof["observe_states"] else "none"),
                         ("WORKLOAD", workload["rules"]), ("INIT", soup)):
        if out.count(f"%{marker}%") != 1:
            raise AssertionError(marker)
        out = out.replace(f"%{marker}%", text)
    return out


def run(modules, commands, *, timeout=540):
    with TemporaryDirectory() as work:
        path = Path(work) / "model.maude"
        path.write_text(modules)
        proc = subprocess.run([os.environ.get("MAUDE", "maude"), "-no-banner", "-no-advise",
                               "-batch", str(path)], input="".join(commands) + "quit\n",
                              text=True, capture_output=True, timeout=timeout)
    if proc.returncode or "Warning" in proc.stderr or "Error" in proc.stderr:
        raise RuntimeError(f"maude failed:\n{proc.stderr[-3000:]}\n{proc.stdout[-1000:]}")
    return proc.stdout


TERM = re.compile(r"[(),]|[^\s(),]+")


def read_terms(text):
    """Maude constructor terms, juxtaposed, as nested tuples (name, args...) and atoms."""
    tokens = TERM.findall(text)
    pos = 0

    def one():
        nonlocal pos
        head = tokens[pos]
        pos += 1
        if pos < len(tokens) and tokens[pos] == "(":
            pos += 1
            args = []
            while tokens[pos] != ")":
                if tokens[pos] == ",":
                    pos += 1
                args.append(one())
            pos += 1
            return (head, *args)
        return head

    out = []
    while pos < len(tokens):
        out.append(one())
    return out


def value(tree):
    """A parsed Maude term as a simulator value. Raises ValueError if it is not a value."""
    if isinstance(tree, tuple):
        head, *args = tree
        if head == "i" and len(args) == 1 and re.fullmatch(r"-?\d+", str(args[0])):
            return int(args[0])
        if head == "q" and len(args) == 1 and str(args[0]).startswith("'"):
            return args[0][1:]
        if head == "b" and args in (["true"], ["false"]):
            return args == ["true"]
        if head == "cons" and len(args) == 2:
            rest = value(args[1])
            if isinstance(rest, tuple):
                return (value(args[0]), *rest)
    elif tree == "nil":
        return ()
    elif re.fullmatch(r"-?\d+", str(tree)):
        return int(tree)
    raise ValueError(f"not a value: {tree!r}")


def reduce(modules, terms, *, timeout=300):
    """Normal forms of terms in STEP as simulator values; None where the term is stuck."""
    output = run(modules, [f"reduce in STEP : {t} .\n" for t in terms], timeout=timeout)
    results = re.findall(r"^result (\S+): (.*?)(?=^=====|^Bye\.|\Z)", output,
                         re.MULTILINE | re.DOTALL)
    if len(results) != len(terms):
        raise RuntimeError(f"maude: {len(results)} of {len(terms)} results\n{output[-2000:]}")
    out = []
    for sort, text in results:
        try:
            parsed, = read_terms(text)
            out.append(value(parsed) if sort == "Val" else None)
        except ValueError:
            out.append(None)
    return out


def search(modules, *, timeout=540):
    """Reachable observations of ENV, and whether any reachable delivery traps.

    Returns (set of frozensets of things, number of states, trap solution text or None).
    """
    output = run(modules, ["search in ENV : init =>* done(O:Soup) .\n",
                           "search [1] in ENV : init =>* trapped(I:Int, S:Val, E:Val) .\n"],
                 timeout=timeout)
    first, second = output.split("search [1] in ENV", 1)
    if "No more solutions." not in first:
        raise RuntimeError(f"maude search did not finish:\n{first[-1500:]}")
    states = int(re.findall(r"states: (\d+)", first)[-1])
    observations = set()
    for text in re.findall(r"^O:Soup --> (.*?)(?=^\s*$|^Solution|^No more)", first,
                           re.MULTILINE | re.DOTALL):
        things = set()
        for thing in read_terms(text):
            if thing == "none":
                continue
            head, *args = thing
            things.add((head, *(value(arg) for arg in args)))
        observations.add(frozenset(things))
    return observations, states, None if "No solution." in second else second.strip()
