"""Compile a node specification to a native step function.

    step : State Event -> [State' Effects]        (ordinary Shen, see tool/README.md)

The state leaves and the event fields are runtime integers; the list structure
of the state, the event kind inside each entry branch and all of the program
text are static. The partial evaluator in tool/pe.py does the rest.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from specializer.backend import generate, normalize, LIMITS
from .pe import (Cons, Node, Specializer, Unsupported, assemble, items_of, kind_of, parse,
                 sequence)
from .schema import Schema

TEMPLATE = Path(__file__).with_name("abi.c.in")
DEPTH = 4000


def flatten(value, shape, path, table):
    """Leaves of a specialization-time value laid out by a declared shape."""
    if isinstance(shape, list):
        try:
            items = items_of(value)
        except Unsupported:
            items = None
        if items is None or len(items) != len(shape):
            raise Unsupported(f"{path}: the specification builds {show(value)} where the "
                              f"manifest declares a list of {len(shape)}")
        out = []
        for n, (item, part) in enumerate(zip(items, shape)):
            out += flatten(item, part, f"{path}[{n}]", table)
        return out
    if kind_of(value) != shape or (type(value) is str and value not in table):
        raise Unsupported(f"{path}: the specification builds {show(value)} where the manifest "
                          f"declares a {shape} (symbols must be in the symbol table)")
    return [value]


def show(value):
    if isinstance(value, Cons) or value is None:
        try:
            return "[" + " ".join(show(v) for v in items_of(value)) + "]"
        except Unsupported:
            return "<improper list>"
    return "<runtime %s>" % value.kind if isinstance(value, Node) else str(value)


def specialize(schema, source=None, *, share=True, specializer=Specializer, **limits):
    """Schema and Shen source to (IR tree, slot count, residual block count)."""
    def encoder(value):
        parts = items_of(value)
        if len(parts) != 2:
            raise Unsupported(f"step must return [State Effects], not {show(value)}")
        out = flatten(parts[0], schema.state, "state", schema.code)
        effects = items_of(parts[1])
        if len(effects) > schema.max_effects:
            raise Unsupported(f"a step emits {len(effects)} effects; max_effects is "
                              f"{schema.max_effects}")
        out.append(len(effects))
        for effect in effects:
            if not isinstance(effect, Cons):
                raise Unsupported(f"effect {show(effect)} is not a tagged list")
            tag = effect.head
            tags = [tag] if type(tag) is str else sorted(getattr(tag, "syms", None) or [])
            shapes = {json.dumps(schema.effects[t].fields) if t in schema.effects else None
                      for t in tags}
            if len(shapes) != 1 or None in shapes:
                raise Unsupported(f"effect tag {show(tag)} is not one declared effect layout")
            out.append(tag)
            out += flatten(effect.tail, schema.effects[tags[0]].fields, f"effect {tags[0]}",
                           schema.code)
        return out

    sp = specializer(parse(schema.source if source is None else source), symbols=schema.code,
                     encoder=encoder, strict=True, **limits)

    def inputs(shape, counter):
        if isinstance(shape, list):
            return sequence([inputs(part, counter) for part in shape])
        counter[0] += 1
        return sp.input(counter[0] - 1, shape)

    def entry():
        state = inputs(schema.state, [0])
        tag = sp.input(schema.state_width, "sym")

        def dispatch(kinds, dyn):
            if not kinds:
                sp.trap()
            event = Cons(kinds[0].tag, inputs(kinds[0].fields, [schema.state_width + 1]))
            sp.fork(sp.equal(tag, kinds[0].tag),
                    lambda d: sp.tail_call(schema.step, [state, event], d, frozenset()),
                    lambda d: dispatch(kinds[1:], d), dyn)

        dispatch(list(schema.events.values()), False)

    start = sp.block(entry)
    blocks = sp.blocks()
    tree, slots = assemble(start, blocks, legacy=False, share=share)
    return tree, slots, len(blocks)


def specialize_init(schema, source=None, *, specializer=Specializer, **limits):
    """(init Node Nodes) with both arguments runtime integers, to (IR tree, slot count).

    The residual program reads the node number and the node count as inputs 0 and 1 and
    emits the state leaves, so the initial state is in the native artifact, not beside it.
    """
    sp = specializer(parse(schema.source if source is None else source), symbols=schema.code,
                     encoder=lambda value: flatten(value, schema.state, "initial state",
                                                   schema.code), strict=True, **limits)
    start = sp.block(lambda: sp.tail_call(schema.init, [sp.input(0), sp.input(1)], False,
                                          frozenset()))
    tree, slots = assemble(start, sp.blocks(), legacy=False)
    return tree, slots


def lower(tree, slots):
    """IR to (normalized tree, the C statements of its body)."""
    saved = dict(LIMITS)
    LIMITS.update(slots=max(slots, 1), depth=DEPTH)
    try:
        tree = normalize(tree)
    except (ValueError, RecursionError) as error:
        if isinstance(error, Unsupported):
            raise
        raise Unsupported(f"the residual program cannot be lowered: {error} (nesting is "
                          f"limited to {DEPTH}, for example by a chain of that many clauses)"
                          ) from None
    finally:
        LIMITS.update(saved)
    try:
        body = generate(tree, template="/* PROGRAM */", slots=max(slots, 1), depth=DEPTH,
                        checked=True)
    except RecursionError:
        raise Unsupported("the residual program is nested too deeply to lower") from None
    return tree, body


def c_source(step_body, slots, init_body, init_slots):
    out = TEMPLATE.read_text()
    for marker, text in (("/* SLOTS */", str(max(slots, 1))), ("/* BANKS */", ""),
                         ("/* INIT SLOTS */", str(max(init_slots, 1))),
                         ("/* PROGRAM */", step_body), ("/* INIT PROGRAM */", init_body)):
        if out.count(marker) != 1:
            raise AssertionError(marker)
        out = out.replace(marker, text)
    return out


def size(tree):
    return 1 + sum(size(child) for child in tree[1:]) if isinstance(tree, list) else 1


class Trap(Exception):
    pass


class Native:
    """A compiled step loaded in-process."""

    def __init__(self, schema, library, executable=None, stats=None):
        self.schema, self.library, self.executable, self.stats = schema, library, executable, stats
        # The loader caches an image by path, so a library rebuilt at the same path would
        # silently be the old code. Load a copy named by its contents instead.
        library = Path(library)
        data = library.read_bytes()
        loaded = library.with_name(f"{library.stem}.{hashlib.sha256(data).hexdigest()[:16]}"
                                   f"{library.suffix}")
        if not loaded.exists():
            loaded.write_bytes(data)
        self.lib = ctypes.CDLL(str(loaded))
        pointer = ctypes.POINTER(ctypes.c_int64)
        self.lib.node_step.restype = ctypes.c_int64
        self.lib.node_step.argtypes = [pointer, ctypes.c_int64, pointer, ctypes.c_int64]
        self.lib.node_init.restype = ctypes.c_int64
        self.lib.node_init.argtypes = [ctypes.c_int64, ctypes.c_int64, pointer, ctypes.c_int64]
        self.input = (ctypes.c_int64 * schema.input_width)()
        self.output = (ctypes.c_int64 * schema.output_capacity)()

    def code(self, row):
        """The value node_step returns: the output length, or a negative code."""
        self.input[:] = row
        return self.lib.node_step(self.input, len(row), self.output, self.schema.output_capacity)

    def raw(self, row):
        """Integer vector in, integer vector out; None when the step has no result."""
        written = self.code(row)
        return None if written < 0 else self.output[:written]

    def init(self, node, nodes):
        """The state integers of (<init> node nodes), computed by the native library."""
        written = self.lib.node_init(node, nodes, self.output, self.schema.output_capacity)
        if written != self.schema.state_width:
            raise Trap(f"init has no result for node {node} of {nodes} (code {written})")
        state = tuple(self.output[:written])
        self.schema.decode_state(state)
        return state

    def step(self, state, event):
        out = self.raw(self.schema.encode_input(state, event))
        if out is None:
            raise Trap(f"step has no result for {self.schema.decode_state(state)} {event}")
        return self.schema.decode_output(out)


def build(schema, directory, source=None, *, name="node", executable=True, **limits):
    """Write IR, C, a shared library and a line-oriented executable into `directory`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tree, slots, blocks = specialize(schema, source, **limits)
    only = {key: limits[key] for key in ("specializer",) if key in limits}
    init_tree, init_slots = specialize_init(schema, source, **only)
    tree, body = lower(tree, slots)
    init_tree, init_body = lower(init_tree, init_slots)
    code = c_source(body, slots, init_body, init_slots)
    (directory / f"{name}.ir.json").write_text(json.dumps(tree) + "\n")
    (directory / f"{name}.init.ir.json").write_text(json.dumps(init_tree) + "\n")
    (directory / f"{name}.c").write_text(code)
    clang = [os.environ.get("CLANG", "clang"), "-std=c11", "-O2", "-Wno-tautological-compare",
             f"-fbracket-depth={2 * DEPTH}",
             str(directory / f"{name}.c")]
    library = directory / f"{name}.{'dylib' if sys.platform == 'darwin' else 'so'}"
    subprocess.run([*clang, "-shared", "-fPIC", "-o", str(library)], check=True, timeout=300)
    program = None
    if executable:
        program = directory / name
        subprocess.run([*clang, "-DNODE_MAIN", "-o", str(program)], check=True, timeout=300)
    stats = {"ir_nodes": size(tree), "slots": slots, "blocks": blocks,
             "c_lines": code.count("\n")}
    return Native(schema, library, program, stats)


def run_init(executable, node, nodes):
    """`node init <node> <nodes>`: the initial state as the executable prints it."""
    result = subprocess.run([str(executable), "init", str(node), str(nodes)], text=True,
                            capture_output=True, check=True, timeout=60)
    return tuple(int(x) for x in result.stdout.split())


def run_lines(executable, rows):
    """The line protocol: one input vector per line; `trap` lines come back as None."""
    payload = "".join(" ".join(map(str, row)) + "\n" for row in rows)
    result = subprocess.run([str(executable)], input=payload, text=True, capture_output=True,
                            check=True, timeout=120)
    return [None if line.startswith("trap") else [int(x) for x in line.split()]
            for line in result.stdout.splitlines()]
