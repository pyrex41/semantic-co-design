"""Compare static libraries with independent interpretations of three pattern profiles."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .build import HERE, UINT32_MAX, compile_library


def expected(profile, row):
    chain = profile["review_queue"]
    route = -1 if len(row) != 1 else next(
        (answer for threshold, answer in chain["handlers"] if row[0] >= threshold), chain["default"])
    items = list(row)
    for op, n in profile["billable_total"]["sum"]:
        items = [x for x in items if x >= n] if op == "keep_ge" else [(x + n) & UINT32_MAX for x in items]
    match = profile["transfer_id"]
    tokens = match["match"]
    captured = {}
    matches = len(tokens) == len(row)
    for token, value in zip(tokens, row):
        if isinstance(token, int):
            matches &= token == value
        elif token != "_":
            matches &= captured.setdefault(token, value) == value
    result = captured.get(match.get("capture"), 1) if matches else -1
    return route, sum(items) & UINT32_MAX, result


# The consumer sees only the public header and archive. Exercise NULL/empty too.
CLIENT = r"""
#include "patterns.h"
#include <stdio.h>
#include <inttypes.h>
int main(void) {
    if (pat_review_queue(NULL, 1) != -1 || pat_billable_total(NULL, 1) != -1 ||
        pat_transfer_id(NULL, 1) != -1) return 2;
    size_t n;
    while (scanf("%zu", &n) == 1) {
        uint32_t row[8];
        if (n > 8) return 2;
        for (size_t i = 0; i < n; ++i) if (scanf("%" SCNu32, &row[i]) != 1) return 2;
        const uint32_t *data = n ? row : NULL;
        printf("%" PRId64 " %" PRId64 " %" PRId64 "\n", pat_review_queue(data, n),
               pat_billable_total(data, n), pat_transfer_id(data, n));
    }
}
"""


def check(work):
    profiles = [json.loads((HERE / "profile.json").read_text()),
                {"review_queue": {"handlers": [[10, 1], [100, 2]], "default": 19},
                 "billable_total": {"sum": [["add", 2], ["keep_ge", 10]]},
                 "transfer_id": {"match": ["?a", "?b", "?a", "?b"], "capture": "?b"}},
                {"review_queue": {"handlers": [], "default": 7},
                 "billable_total": {"sum": []}, "transfer_id": {"match": []}}]
    rows = [row for length in range(5) for row in itertools.product((0, 1, 7, UINT32_MAX), repeat=length)]
    rows += [(n,) for n in (8, 9, 10, 11, 99, 100, 101)] + [(4, 10, 20), (7, 42, 9, 42)]
    wire = "".join(" ".join(map(str, [len(row), *row])) + "\n" for row in rows)
    client = work / "check.c"
    client.write_text(CLIENT)
    for i, profile in enumerate(profiles):
        lib = work / f"lib{i}"
        compile_library(profile, lib)
        code = (lib / "patterns.c").read_text()
        assert code.count("while (") == 1  # only the traversal of the input sequence
        binary = work / "consumer"
        subprocess.run([os.getenv("CLANG", "clang"), "-std=c11", "-I", lib, client, lib / "libpatterns.a",
                        "-o", binary], check=True, timeout=60)
        observed = subprocess.run([binary], input=wire, text=True, capture_output=True, check=True, timeout=20)
        results = [tuple(map(int, line.split())) for line in observed.stdout.splitlines()]
        assert len(results) == len(rows)
        for row, result in zip(rows, results):
            assert result == expected(profile, row), (i, row, result, expected(profile, row))
    print(f"{len(profiles)} linked C profiles, {len(rows)} input rows per profile, {3*len(rows)*len(profiles)} results verified.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp:
        check(Path(temp))
