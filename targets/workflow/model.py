"""The workflow engine as a node of tool/: one manifest per workflow definition.

The engine (engine.shen) is the same text for every definition. A definition file adds a
constant, `<name>.definition`, and the two entry points that apply the engine to it. The
manifest differs between definitions only in what the definition itself fixes: the names
of its activities (symbols that travel in effects) and the number of its idempotency keys
(one mark each in the state). Both are read from the definition by shen-jit, not restated.
"""
from pathlib import Path

from tool import shen
from tool.schema import Schema

HERE = Path(__file__).parent
ENGINE = HERE / "engine.shen"
INVARIANTS = HERE / "invariants.shen"

# The words of the definition language. They are deliberately absent from every manifest:
# a symbol that is not in the table has no integer code, so the compiler refuses a program
# in which one of them would have to be compared, stored or emitted at run time.
SYNTAX = ("act", "each", "sleep", "branch", "end")

MODES = ["loading", "live"]
PHASES = ["ready", "waiting", "backoff", "unwinding", "undoing", "completed", "compensated",
          "stuck"]
ROWS = ["sched", "ok", "fail", "woke", "cancel", "undo", "undone", "completed", "compensated",
        "stuck"]
TERMINAL = ("completed", "compensated", "stuck")
ROW = ["sym", "num", "num"]


def source(name, engine=None):
    """The Shen text of the engine applied to one definition."""
    return ((ENGINE.read_text() if engine is None else engine) + "\n"
            + (HERE / "defs" / f"{name}.shen").read_text())


DESCRIBED = {}


def describe(name):
    """Keys and activity names of a definition, computed from it by shen-jit."""
    if name not in DESCRIBED:
        keys, names = shen.evaluate([ENGINE, HERE / "defs" / f"{name}.shen", INVARIANTS], [
            f"(wf.keys (wf.program ({name}.definition)))",
            f"(inv.names (wf.program ({name}.definition)))"])
        DESCRIBED[name] = list(keys), list(dict.fromkeys(names))
    return DESCRIBED[name]


EVENTS = {
    "restart": {"role": "restart"},
    "loaded": {"role": "disk-read-result", "fields": ["num", "num", "num", *ROW]},
    "written": {"role": "disk-ack", "fields": ["num"]},
    "done": {"role": "request", "fields": ["num", "num", "num"]},
    "reverted": {"role": "request", "fields": ["num"]},
    "timer": {"role": "timer", "fields": ["num"]},
    "cancel": {"role": "request"}}
EFFECTS = {
    "read": {"role": "disk-read", "fields": ["num", "num"]},
    "write": {"role": "disk-write", "fields": ["num", "num", *ROW]},
    "run": {"role": "output", "fields": ["sym", "num", "num"]},
    "revert": {"role": "output", "fields": ["sym", "num", "num"]},
    "report": {"role": "output", "fields": ["sym"]},
    "arm": {"role": "timer-set", "fields": ["num", "num"]}}


def manifest(name, keys, names, extra_symbols=()):
    """The node manifest of one definition. `extra_symbols` go last, so that adding them
    changes no other symbol's code."""
    symbols = list(dict.fromkeys(["none", *MODES, *PHASES, *ROWS, *names, *EVENTS, *EFFECTS,
                                  *extra_symbols]))
    return {
        "name": f"workflow-{name}",
        "spec": "spec.shen", "step": f"{name}.step", "init": f"{name}.init",
        "symbols": symbols,
        "state": ["sym", "num", ROW,
                  ["sym", "num", "num", "num", "num", {"capacity": len(keys), "of": "num"}]],
        "events": EVENTS, "effects": EFFECTS, "max_effects": 2,
    }


class Definition:
    """One workflow definition, ready to compile: its schema lives in `directory`."""

    def __init__(self, name, directory, *, engine=None, extra_symbols=()):
        self.name, self.directory = name, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.keys, self.names = describe(name)
        self.text = source(name, engine)
        (self.directory / "spec.shen").write_text(self.text)
        self.schema = Schema(manifest(name, self.keys, self.names, extra_symbols), self.directory)
        self.files = [self.directory / "spec.shen", INVARIANTS]

    def mutant(self, label, edits):
        """The same definition under an engine with the given (old, new) edits."""
        text = ENGINE.read_text()
        for old, new in edits:
            if text.count(old) != 1:
                raise AssertionError(f"{label}: mutation site occurs {text.count(old)} times")
            text = text.replace(old, new)
        return Definition(self.name, self.directory.parent / f"{self.name}-{label}", engine=text)
