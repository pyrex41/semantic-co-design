"""The same residual C compiled to a freestanding WebAssembly module.

    NODE_TARGET=wasm python3 -m tool check accepted_job

The module imports nothing: no clock, no entropy, no I/O and no host functions,
so every result is a function of the integers the embedder wrote into its
memory. The embedder here is wasmtime; the simulator drives it exactly as it
drives the shared library.

Exports: memory, node_in() and node_out() (addresses of two int64 buffers),
node_call(count) and node_failure(). A failure of the step is a wasm trap; the
embedder reads the code and replaces the instance.
"""
import os
from pathlib import Path
import struct
import subprocess

from .compile import Trap

EXPORTS = ("memory", "node_in", "node_out", "node_call", "node_failure")


def link(source, module, bracket_depth):
    subprocess.run([os.environ.get("WASM_CLANG", "clang"), "--target=wasm32-unknown-unknown",
                    "-std=c11", "-O2", "-ffreestanding", "-nostdlib",
                    f"-fbracket-depth={bracket_depth}", "-Wl,--no-entry", "-Wl,--strip-all",
                    "-o", str(module), str(source)], check=True, timeout=300)
    return Path(module)


class Wasm:
    """A compiled step in a wasm instance, with the interface of compile.Native."""

    def __init__(self, schema, module, executable=None, stats=None):
        import wasmtime
        self.schema, self.library, self.executable, self.stats = schema, module, executable, stats
        self.wasmtime = wasmtime
        self.engine = wasmtime.Engine()
        self.module = wasmtime.Module.from_file(self.engine, str(module))
        if self.module.imports:
            raise ValueError(f"{module} imports {[i.name for i in self.module.imports]}")
        self.instantiate()

    def instantiate(self):
        self.store = self.wasmtime.Store(self.engine)
        exports = self.wasmtime.Instance(self.store, self.module, []).exports(self.store)
        self.memory, node_in, node_out, self.call, self.failure = (exports[n] for n in EXPORTS)
        self.at_in, self.at_out = node_in(self.store), node_out(self.store)

    def raw(self, row):
        """Integer vector in, integer vector out; None when the step traps."""
        self.memory.write(self.store, struct.pack(f"<{len(row)}q", *row), self.at_in)
        try:
            written = self.call(self.store, len(row))
        except self.wasmtime.Trap:
            code = self.failure(self.store)
            self.instantiate()
            if code == 0:
                raise
            return None
        if written < 0:
            return None
        data = self.memory.read(self.store, self.at_out, self.at_out + 8 * written)
        return list(struct.unpack(f"<{written}q", data))

    def step(self, state, event):
        out = self.raw(self.schema.encode_input(state, event))
        if out is None:
            raise Trap(f"step has no result for {self.schema.decode_state(state)} {event}")
        return self.schema.decode_output(out)
