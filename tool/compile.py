"""Compile a node specification to a native step function.

    step : State Event -> [State' Effects]        (ordinary Shen, see tool/README.md)

The state leaves and the event fields are runtime integers; the list structure
of the state, the event kind inside each entry branch and all of the program
text are static. The partial evaluator in tool/pe.py does the rest.
"""
import ctypes
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


def plain(value):
    """A fully static specialization-time value as nested tuples."""
    if isinstance(value, Cons) or value is None:
        return tuple(plain(v) for v in items_of(value))
    if isinstance(value, Node):
        raise Unsupported("expected a value known at specialization time")
    return value


def specialize(schema, source=None, *, share=True, **limits):
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

    sp = Specializer(parse(schema.source if source is None else source), symbols=schema.code,
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


def initial_state(schema, node, nodes, source=None):
    """(init Node Nodes) evaluated at specialization time, as state integers."""
    sp = Specializer(parse(schema.source if source is None else source), symbols=schema.code)
    result = []
    sp.block(lambda: result.append(sp.call(schema.init, [node, nodes])))
    if not result:
        raise Unsupported("init has no value")
    return schema.encode_state(plain(result[0]))


def c_source(tree, slots):
    return generate(tree, template=TEMPLATE.read_text(), slots=max(slots, 1), depth=DEPTH)


def size(tree):
    return 1 + sum(size(child) for child in tree[1:]) if isinstance(tree, list) else 1


class Trap(Exception):
    pass


class Native:
    """A compiled step loaded in-process."""

    def __init__(self, schema, library, executable=None, stats=None):
        self.schema, self.library, self.executable, self.stats = schema, library, executable, stats
        self.lib = ctypes.CDLL(str(library))
        pointer = ctypes.POINTER(ctypes.c_int64)
        self.lib.node_step.restype = ctypes.c_int64
        self.lib.node_step.argtypes = [pointer, ctypes.c_int64, pointer, ctypes.c_int64]
        self.input = (ctypes.c_int64 * schema.input_width)()
        self.output = (ctypes.c_int64 * schema.output_capacity)()

    def raw(self, row):
        """Integer vector in, integer vector out; None when the step traps."""
        self.input[:] = row
        written = self.lib.node_step(self.input, len(row), self.output,
                                     self.schema.output_capacity)
        return None if written < 0 else self.output[:written]

    def step(self, state, event):
        out = self.raw(self.schema.encode_input(state, event))
        if out is None:
            raise Trap(f"step has no result for {self.schema.decode_state(state)} {event}")
        return self.schema.decode_output(out)


def build(schema, directory, source=None, *, name="node", **limits):
    """Write IR, C, a shared library and a line-oriented executable into `directory`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tree, slots, blocks = specialize(schema, source, **limits)
    saved = dict(LIMITS)
    LIMITS.update(slots=max(slots, 1), depth=DEPTH)
    try:
        tree = normalize(tree)
    finally:
        LIMITS.update(saved)
    code = c_source(tree, slots)
    (directory / f"{name}.ir.json").write_text(json.dumps(tree) + "\n")
    (directory / f"{name}.c").write_text(code)
    clang = [os.environ.get("CLANG", "clang"), "-std=c11", "-O2", f"-fbracket-depth={2 * DEPTH}",
             str(directory / f"{name}.c")]
    library = directory / f"{name}.{'dylib' if sys.platform == 'darwin' else 'so'}"
    executable = directory / name
    subprocess.run([*clang, "-shared", "-fPIC", "-o", str(library)], check=True, timeout=300)
    subprocess.run([*clang, "-DNODE_MAIN", "-o", str(executable)], check=True, timeout=300)
    stats = {"ir_nodes": size(tree), "slots": slots, "blocks": blocks,
             "c_lines": code.count("\n")}
    return Native(schema, library, executable, stats)


def run_lines(executable, rows):
    """The line protocol: one input vector per line; `trap` lines come back as None."""
    payload = "".join(" ".join(map(str, row)) + "\n" for row in rows)
    result = subprocess.run([str(executable)], input=payload, text=True, capture_output=True,
                            check=True, timeout=120)
    return [None if line.startswith("trap") else [int(x) for x in line.split()]
            for line in result.stdout.splitlines()]
