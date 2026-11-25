"""Small compilation boundary shared by the PAIP experiments."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Residual:
    source: str
    run: Callable


def compile_residual(name, source, bindings=None):
    """Compile generated Python source with explicit runtime helpers."""
    namespace = dict(bindings or {})
    code = compile(source, f"<paip:{name}>", "exec", dont_inherit=True)
    exec(code, namespace)
    return Residual(source, namespace["run"])
