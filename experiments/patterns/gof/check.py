"""Verify GoF specializations through their generated public C interfaces."""

import os
from pathlib import Path
import subprocess
import tempfile

from .build import CFLAGS, HERE, compile_library, read_profile, specialize_profile


def run_case(work, label, profile, consumer, *, must_contain=(), must_absent=()):
    archive = work / label
    compile_library(profile, archive)
    generated = (archive / "gof.c").read_text()
    for fragment in must_contain:
        assert fragment in generated, (label, "missing residual fragment", fragment)
    for fragment in must_absent:
        assert fragment not in generated, (label, "unexpected residual fragment", fragment)
    client = work / f"{label}.c"
    client.write_text(consumer)
    binary = work / f"{label}-consumer"
    subprocess.run([os.environ.get("CLANG", "clang"), *CFLAGS, "-I", archive, client,
                    archive / "libgof.a", "-o", binary], check=True, timeout=60)
    subprocess.run([binary], check=True, timeout=20)


def check_invalid_profiles():
    invalid = [[], {}, {"wrong-name": {"pattern": "iterator"}},
               {"one": {"pattern": "missing"}},
               {"hook_scope": {"pattern": "iterator", "stride": 1, "offset": 0}},
               {"scope": {"pattern": "mediator", "routes": {"open": []}},
                "scope_open": {"pattern": "iterator", "stride": 1, "offset": 0},
                "scope_open_next": {"pattern": "strategy", "algorithm": "insertion", "order": "ascending"}},
               {"gate": {"pattern": "proxy", "roles": [32], "target": "target"}},
               {"notice": {"pattern": "observer", "subscribers": {"notify": {"notify": "target", "tags": [32]}}}},
               {"method": {"pattern": "command", "apply": "same", "undo": None},
                "visit": {"pattern": "visitor", "number": "same", "pair": "pair"}}]
    for profile in invalid:
        try:
            specialize_profile(profile)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid example profile {profile!r}")


def check_mixed_profile(work):
    profile = {
        "publish": {"pattern": "proxy", "target": "route", "roles": [2]},
        "publish_disabled": {"pattern": "proxy", "target": "unused_target", "roles": []},
        "dispatch": {"pattern": "mediator", "routes": {"publish": ["route"]}},
        "record": {"pattern": "builder", "representation": "sequence", "capacity": 2},
    }
    run_case(work, "mixed", profile, """
#include "gof.h"
#include <assert.h>

uint32_t route(void *context, uint32_t input) {
    unsigned *calls = context;
    return input + ++*calls;
}

int main(void) {
    unsigned calls = 0;
    uint32_t records[] = {4, 5, 6};
    uint32_t result = 81;
    gof_record_product product = gof_record_build(records, 3);
    assert(product.count == 2);
    assert(!gof_publish_disabled(&calls, 2, product.values[0], &result));
    assert(calls == 0 && result == 81);
    assert(gof_publish(&calls, 2, product.values[0], &result) && result == 5);
    assert(gof_dispatch_publish(&calls, product.values[1]) == 7);
    assert(calls == 2);
    return 0;
}
""", must_contain=("gof_hook_route(context, request)", "gof_hook_route(context, event)"),
             must_absent=("unused_target",))


def main():
    from . import behavioral, behavioral_check, creational, creational_check, structural, structural_check

    modules = (behavioral, creational, structural)
    assert sum(len(module.GENERATORS) for module in modules) == 23
    assert len({key for module in modules for key in module.GENERATORS}) == 23
    for module in modules:
        family = module.__name__.split(".")[-1]
        profile = read_profile(HERE / f"{family}_demo.json")
        assert {entry["pattern"] for entry in profile.values()} == set(module.GENERATORS)
    check_invalid_profiles()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        for checker in (creational_check, structural_check, behavioral_check):
            checker.check(work)
        check_mixed_profile(work)
    print("GoF catalog verified through seven linked C11 consumers: 23 patterns across three families.")


if __name__ == "__main__":
    main()
