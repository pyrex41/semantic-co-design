"""Lower an equality-only unary digest language using two representative values."""
import re
from pathlib import Path

from finite import expand


def prepare(spec):
    if len(spec["types"]) != 1 or list(spec["types"].values()) != ["sha256"]:
        raise ValueError("digest profile requires a single sha256 type")
    if spec["initial"] not in spec["states"] or spec["states"][spec["initial"]] is not None:
        raise ValueError("digest profile requires a fieldless initial state")
    typ, = spec["types"]
    symbolic = expand({**spec, "types": {typ: ["a", "b"]}})
    symbolic["runtime_digest"] = True
    return symbolic


def step(spec, rules, state, event):
    def split(term):
        name, sep, value = term.partition("-")
        if sep and not re.fullmatch("[0-9a-f]{64}", value):
            raise ValueError("expected canonical sha256")
        return name, value
    sc, stored = split(state)
    ec, incoming = split(event)
    s = sc + "-a" if stored else sc
    # 'a' refers to the stored field, 'b' to a distinct input field.
    e = ec + ("-a" if incoming == stored else "-b") if incoming else ec
    if s not in spec["states"] or e not in spec["events"]:
        raise ValueError("invalid state or event")
    target, output = rules.get((s, e), (s, "ignored"))

    def resolve(term):
        name, sep, value = term.partition("-")
        return name + "-" + (stored if value == "a" else incoming) if sep else name
    return resolve(target), resolve(output)


def wrapper(spec):
    # Names validated by finite.expand; longest tagged result includes '-' and 64 hex digits.
    width = 66 + max(len(x) for x in (*spec["states"], *spec["events"], *spec["outputs"], "ignored"))
    return [f"#define TERM_SIZE {width}", Path(__file__).with_name("digest.c").read_text()]
