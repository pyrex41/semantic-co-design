"""Elaborate unary enum constructors and equality guards to finite transitions."""
from itertools import product
import re


def expand(spec):
    types = spec["types"]
    for values in types.values():
        if not isinstance(values, list) or not values or len(set(values)) != len(values):
            raise ValueError("enum requires distinct members")
        if any(not re.fullmatch("[a-z][a-z0-9]*", v) for v in values):
            raise ValueError("invalid enum member")

    def terms(declarations):
        result = {}
        for constructor, typ in declarations.items():
            if not re.fullmatch("[a-z][a-z0-9]*", constructor):
                raise ValueError("invalid constructor")
            if typ is not None and typ not in types:
                raise ValueError("unknown enum")
            for value in types[typ] if typ is not None else [None]:
                name = constructor if value is None else f"{constructor}-{value}"
                result[name] = constructor, typ, value
        return result

    states, events, outputs = [terms(spec[k]) for k in ("states", "events", "outputs")]
    rules = []
    for state, event, guard, target, output in spec["rules"]:
        matches = 0
        for (s, (sc, st, sv)), (e, (ec, et, ev)) in product(states.items(), events.items()):
            if (sc, ec) != (state, event):
                continue
            if guard not in ("always", "equal", "different"):
                raise ValueError("unknown guard")
            if guard != "always":
                if st is None or st != et:
                    raise ValueError("guard needs two fields of the same enum")
                if (guard == "equal") != (sv == ev):
                    continue

            def instantiate(expr, declarations):
                if isinstance(expr, str):
                    if expr in declarations and declarations[expr][1] is None:
                        return expr
                elif isinstance(expr, list) and len(expr) == 2:
                    constructor, field = expr
                    typ, value = {"stored": (st, sv), "input": (et, ev)}[field]
                    name = f"{constructor}-{value}"
                    if typ is not None and name in declarations and declarations[name][1] == typ:
                        return name
                raise ValueError(f"ill-typed result {expr!r}")

            rules.append([s, e, instantiate(target, states), instantiate(output, outputs)])
            matches += 1
        if not matches:
            raise ValueError("rule matches no state/event pair")
    return {**spec, "states": list(states), "events": list(events), "rules": rules}
