"""The node manifest: shapes, the symbol table and the integer encoding.

The compiler and the simulator both read this one description, so a symbol,
an event kind or a state field has the same integer meaning on both sides.
"""
import json
from pathlib import Path

LEAVES = ("num", "sym", "bool")
EVENT_ROLES = ("request", "recv", "disk-ack", "disk-read-result", "restart", "timer", "clock")
EFFECT_ROLES = ("output", "send", "disk-write", "disk-read", "timer-set", "timer-cancel",
                "clock-read")


class SchemaError(ValueError):
    pass


def expand(shape):
    """A declared shape to nested lists of leaf kinds. A bounded collection is `capacity` slots."""
    if shape in LEAVES:
        return shape
    if isinstance(shape, list):
        return [expand(item) for item in shape]
    if isinstance(shape, dict) and set(shape) == {"capacity", "of"} and type(shape["capacity"]) is int:
        return [expand(shape["of"]) for _ in range(shape["capacity"])]
    raise SchemaError(f"invalid shape {shape!r}")


def width(shape):
    return 1 if isinstance(shape, str) else sum(map(width, shape))


class Kind:
    def __init__(self, tag, spec, roles):
        self.tag, self.role = tag, spec.get("role")
        if self.role not in roles:
            raise SchemaError(f"{tag}: role must be one of {roles}")
        self.fields = [expand(field) for field in spec.get("fields", [])]
        self.width = width(self.fields)
        self.deliver = spec.get("deliver")


class Schema:
    def __init__(self, manifest, directory=None):
        if not isinstance(manifest, dict):
            directory = Path(manifest).parent
            manifest = json.loads(Path(manifest).read_text())
        self.manifest, self.directory = manifest, Path(directory) if directory else None
        self.name = manifest["name"]
        self.step, self.init = manifest["step"], manifest["init"]
        self.state = expand(manifest["state"])
        self.events = {tag: Kind(tag, spec, EVENT_ROLES) for tag, spec in manifest["events"].items()}
        self.effects = {tag: Kind(tag, spec, EFFECT_ROLES)
                        for tag, spec in manifest["effects"].items()}
        self.max_effects = manifest["max_effects"]
        # One table for every symbol that can be a runtime value. Codes start at 1 so that
        # zero padding is never a symbol.
        self.symbols = list(manifest.get("symbols", []))
        for tag in [*self.events, *self.effects]:
            if tag not in self.symbols:
                self.symbols.append(tag)
        if len(set(self.symbols)) != len(self.symbols):
            raise SchemaError("duplicate symbol")
        self.code = {symbol: n + 1 for n, symbol in enumerate(self.symbols)}
        self.symbol = {n: symbol for symbol, n in self.code.items()}
        self.state_width = width(self.state)
        self.event_width = max((kind.width for kind in self.events.values()), default=0)
        self.effect_width = max((kind.width for kind in self.effects.values()), default=0)
        self.input_width = self.state_width + 1 + self.event_width
        self.output_capacity = self.state_width + 1 + self.max_effects * (1 + self.effect_width)
        self.roles = {}
        for kind in self.events.values():
            if kind.role in ("disk-ack", "disk-read-result", "restart", "timer", "clock"):
                if kind.role in self.roles:
                    raise SchemaError(f"two events with role {kind.role}")
                self.roles[kind.role] = kind
        for kind in self.effects.values():
            if kind.role == "send" and (kind.deliver not in self.events
                                         or self.events[kind.deliver].role != "recv"
                                         or self.events[kind.deliver].fields != kind.fields):
                raise SchemaError(f"{kind.tag}: deliver must name a recv event with the same fields")
        self.invariants = manifest.get("invariants", {})

    @property
    def source(self):
        return (self.directory / self.manifest["spec"]).read_text()

    # ---- values (nested tuples of int, str and bool) to and from integers

    def encode(self, value, shape, out=None, path="value"):
        out = [] if out is None else out
        if isinstance(shape, list):
            if not isinstance(value, (list, tuple)) or len(value) != len(shape):
                raise SchemaError(f"{path}: expected a list of {len(shape)}, got {value!r}")
            for n, (item, part) in enumerate(zip(value, shape)):
                self.encode(item, part, out, f"{path}[{n}]")
        elif shape == "num" and type(value) is int:
            out.append(value)
        elif shape == "bool" and type(value) is bool:
            out.append(int(value))
        elif shape == "sym" and value in self.code and type(value) is str:
            out.append(self.code[value])
        else:
            raise SchemaError(f"{path}: {value!r} is not a declared {shape}")
        return out

    def decode(self, ints, shape, path="value"):
        """Consume integers from the iterator `ints`."""
        if isinstance(shape, list):
            return tuple(self.decode(ints, part, path) for part in shape)
        n = next(ints)
        if shape == "num":
            return n
        if shape == "bool" and n in (0, 1):
            return bool(n)
        if shape == "sym" and n in self.symbol:
            return self.symbol[n]
        raise SchemaError(f"{path}: {n} is not a {shape}")

    def default(self, shape):
        if isinstance(shape, list):
            return tuple(self.default(part) for part in shape)
        return {"num": 0, "bool": False}.get(shape, self.symbols[0] if self.symbols else None)

    def encode_state(self, state):
        return tuple(self.encode(state, self.state, path="state"))

    def decode_state(self, ints):
        return self.decode(iter(ints), self.state, "state")

    def encode_input(self, state_ints, event):
        """The ABI input vector: state leaves, event tag, event fields, zero padding."""
        kind = self.events.get(event[0])
        if kind is None:
            raise SchemaError(f"undeclared event {event!r}")
        row = [*state_ints, self.code[kind.tag]]
        self.encode(tuple(event[1:]), kind.fields, row, f"event {kind.tag}")
        return row + [0] * (self.input_width - len(row))

    def decode_output(self, ints):
        """The ABI output vector: state leaves, effect count, then tag and fields per effect."""
        stream = iter(ints)
        state = tuple(next(stream) for _ in range(self.state_width))
        self.decode_state(state)
        effects = []
        for _ in range(next(stream)):
            tag = self.symbol.get(next(stream))
            if tag not in self.effects:
                raise SchemaError(f"undeclared effect code in output {list(ints)}")
            effects.append((tag, *self.decode(stream, self.effects[tag].fields, f"effect {tag}")))
        if next(stream, None) is not None:
            raise SchemaError("trailing output")
        return state, tuple(effects)
