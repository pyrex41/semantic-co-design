"""Independent scalar expectations, actual Shen overlap, reference and packaged App Shen."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "webapp"))
import appc
from values import Problem


def definition(file, name):
    # Preserve source text for the external engine: no App Shen AST translation.
    lines = (HERE / file).read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.split()[:2] == ["(define", name])
    stop = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("(define ")), len(lines))
    return "\n".join(lines[start:stop]) + "\n"


def property_cases():
    yield "observe-recovery", "recovery-law", []
    for count in range(13):
        for quorum in range(1, 10):
            yield "observe-votes", "vote-law", [count, quorum]
    for price in range(129):
        for tick in range(1, 17):
            yield "observe-rounding", "rounding-law", [price, tick]
    for idle in range(1, 10):
        for reserve in [None, *range(12)]:
            flow = {"until": idle, "port": 1001}
            if reserve is not None:
                flow["reserved-until"] = reserve
            yield "observe-recovery", "recovery-law", [flow, {"until": 3, "port": 1002}]


def check_properties(entries):
    count = 0
    for observer, law, arg in property_cases():
        observed = entries[observer](None, arg)
        assert entries[law](None, [arg, observed]) is True, (law, arg, observed)
        count += 1
    # Each intentionally wrong observation must provide a counterexample.
    for law, arg, observed in [
        ("vote-law", [2, 3], ["pending", "approved", "rejected"]),
        ("rounding-law", [17, 5], [15, 25]),
        ("recovery-law", [{"until": 5, "reserved-until": 9}],
         [[{"until": 5, "reserved-until": 9}]] * 2),
    ]:
        assert entries[law](None, [arg, observed]) is False, (law, arg, observed)
    return count


def check(work):
    overlap = definition("webapp/review.shen", "vote-status") + definition(
        "trading-engine/strategy.shen", "tx.inventory-direction")
    # Shen reads the bare symbol null; App Shen uses JSON null at its API.
    probes = [('(vote-status "approve" 1 2)', "pending"),
              ('(vote-status "approve" 2 2)', "approved"),
              ('(vote-status "reject" 4 2)', "rejected"),
              ('(tx.inventory-direction 3 5 9)', "buy"),
              ('(tx.inventory-direction 7 5 9)', None),
              ('(tx.inventory-direction 10 5 9)', "sell")]
    shared = work / "shared.shen"
    shared.write_text(overlap)
    cmd = [os.environ.get("SHEN_JIT", "shen-jit"), "eval", "-l", str(shared)]
    for query, _ in probes:
        cmd.extend(["-e", query])
    cmd.extend(["-e", "(/ 7 2)"])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)
    outputs = result.stdout.strip().splitlines()[-len(probes)-1:]
    assert outputs == [want if want is not None else "null" for _, want in probes] + ["3.5"], outputs

    wrappers = "".join(f"(define probe{i} _ -> {expr})\n" for i, (expr, _) in enumerate(probes))
    (work / "dialect.shen").write_text(wrappers + """
(define quotient Pair -> (floor-div (at Pair 0) (at Pair 1)))
(define remainder Pair -> (floor-mod (at Pair 0) (at Pair 1)))
(define addition Pair -> (+ (at Pair 0) (at Pair 1)))
(define equal Pair -> (= (at Pair 0) (at Pair 1)))
(define conditional Use -> (if Use (floor-div 7 0) "untouched"))
""")
    cases = [(f"probe{i}", None, want) for i, (_, want) in enumerate(probes)] + [
        ("quotient", [7, 2], 3), ("quotient", [-7, 2], -4), ("quotient", [7,-2], -4),
        ("remainder", [-7, 2], 1), ("remainder", [7,-2], -1),
        ("addition", [2**63-2,1], 2**63-1), ("conditional", False, "untouched"),
        ("equal", [[True],[1]], False), ("equal", [{"a": 0},{"a": False}], False)]
    errors = [("quotient", [-2**63,-1], "integer-range"), ("conditional", True, "zero-divisor"),
              ("addition", [2**63-1,1], "integer-range"), ("addition", [True,1], "integer-range")]
    property_source = (HERE / "properties.shen").read_text()
    for filename, names in [
        ("trading-engine/strategy.shen", ["tx.ceil-step", "tx.floor-step"]),
        ("router-nlb/appliance.shen", ["rt.restore-reservations", "rt.get-or", "rt.put"]),
    ]:
        for name in names:
            property_source += "\n" + definition(filename, name)
    (work / "properties.shen").write_text(property_source)
    manifest = work / "manifest.json"
    release = {"language": "app-shen/1", "sources": ["shared.shen", "dialect.shen", "properties.shen"],
               "capabilities": {}, "entries": {name: {"function": name} for name, *_ in cases}}
    property_entries = {name for observer, law, _ in property_cases() for name in (observer, law)}
    release["entries"].update({name: {"function": name} for name in sorted(property_entries)})
    for version in (None, "app-shen/2"):
        manifest.write_text(json.dumps({**release, "language": version}))
        try:
            appc.load(manifest)
        except appc.SourceError:
            continue
        raise AssertionError(f"unsupported dialect accepted: {version}")
    for foreign_name in ("/", "shen.mod"):
        invalid = work / "invalid.shen"
        invalid.write_text(f"(define illegal X -> ({foreign_name} X 2))")
        manifest.write_text(json.dumps({**release, "sources": [*release["sources"], invalid.name]}))
        try:
            appc.load(manifest)
        except appc.SourceError as exc:
            assert f"undefined function {foreign_name}" in str(exc)
        else:
            raise AssertionError(f"ordinary Shen operation {foreign_name} accepted silently")
    manifest.write_text(json.dumps(release))
    release, evaluator = appc.load(manifest)
    dest = work / "compiled"
    appc.build(manifest, dest)
    so, = (dest / "lib").glob("_application*.so")
    spec = importlib.util.spec_from_file_location("_application", so)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for entries in (appc.reference_entries(release, evaluator), module.entries):
        property_count = check_properties(entries)
        for name, arg, expected in cases:
            result = entries[name](None, arg)
            assert type(result) is type(expected) and result == expected, (name, arg, result)
        for name, arg, code in errors:
            try:
                entries[name](None, arg)
            except Problem as exc:
                assert exc.code == code, (name, arg, exc.code)
            else:
                raise AssertionError((name, arg, "expected error", code))
    print(f"Shen JIT checked {len(probes)} application cases; App Shen reference/native passed "
          f"{len(cases) + len(errors)} value and failure cases, {property_count} generated property cases and 3 negative controls per mode.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        check(Path(tmp))
