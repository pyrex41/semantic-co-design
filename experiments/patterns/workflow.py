"""Consume the three book libraries in a single invoice routing example."""
import operator
import os
from pathlib import Path
import subprocess
import tempfile

from .gof.build import compile_library
from .paip.rules import compile_translator
from .sicp.evaluator import compile_evaluator


# The native step uses the archive exactly as another application's C client would.
CLIENT = r"""
#include "gof.h"
#include <inttypes.h>
#include <stdio.h>
int main(void) {
    uint32_t amount;
    while (scanf("%" SCNu32, &amount) == 1) {
        printf("%" PRId64 "\n", gof_review_queue(&amount, 1));
    }
    return ferror(stdin) || !feof(stdin);
}
"""


def build_workflow(work, unit_price, handlers):
    """Fix the message layout, pricing expression and review policy once."""
    if type(unit_price) is not int or not 0 <= unit_price <= 0xffffffff:
        raise ValueError("unit_price must be a uint32")
    translate = compile_translator([
        (("invoice", "?id", "?units", "confirm", "?id"), ("?id", "?units")),
    ]).run
    price = compile_evaluator(("call", "multiply", "units", unit_price),
                              ("units",), primitives={"multiply": operator.mul})
    work.mkdir(parents=True, exist_ok=True)
    lib = work / "lib"
    compile_library({"review_queue": {"pattern": "chain_of_responsibility",
                                      "handlers": handlers, "default": 0}}, lib)
    client = work / "route.c"
    client.write_text(CLIENT)
    binary = work / "route"
    subprocess.run([os.environ.get("CLANG", "clang"), "-std=c11", "-I", lib,
                    client, lib / "libgof.a", "-o", binary], check=True, timeout=60)

    def route(messages):
        pending, results = [], []
        for message in messages:
            match = translate(message)
            if match is None:
                results.append(None)
                continue
            invoice_id, units = match
            if type(units) is not int or units < 0:
                raise ValueError("units must be a nonnegative integer")
            amount = price(units)
            if amount > 0xffffffff:
                raise ValueError("invoice amount exceeds the native word width")
            pending.append((len(results), invoice_id, amount))
            results.append(None)
        reply = subprocess.run([binary], input="".join(f"{amount}\n" for _, _, amount in pending),
                               text=True, capture_output=True, check=True, timeout=10)
        queues = reply.stdout.splitlines()
        if len(queues) != len(pending):
            raise RuntimeError("native router returned an incomplete batch")
        for (index, invoice_id, amount), queue in zip(pending, queues):
            results[index] = (invoice_id, amount, int(queue))
        return results

    return route


def main():
    messages = [("invoice", 71, 12, "confirm", 71),
                ("invoice", 72, 1, "confirm", 72),
                ("invoice", 73, 12, "confirm", 99)]
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        route = build_workflow(work, unit_price=10, handlers=[[100, 2], [10, 1]])
        result = route(messages)
        assert result == [(71, 120, 2), (72, 10, 1), None]
        assert route([]) == []
        revised = build_workflow(work / "revised", unit_price=5, handlers=[[10, 7]])
        assert revised(messages) == [(71, 60, 7), (72, 5, 0), None]
        for units in (-1, True, 0x100000000):
            try:
                route([("invoice", 71, units, "confirm", 71)])
            except ValueError:
                continue
            raise AssertionError(f"accepted invalid units {units!r}")
        print(result)


if __name__ == "__main__":
    main()
