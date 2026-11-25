"""Observe seven behavioral projections through the generated GoF C API."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from ..gof.build import CFLAGS, compile_library
from .check import require, run


IDS = tuple(f"gof-{name}" for name in (
    "command", "interpreter", "mediator", "memento", "state", "strategy", "visitor",
))


HOOKS = r"""
#include "gof.h"
#include <inttypes.h>
#include <stdio.h>

typedef struct {
    unsigned id;
    uint32_t balance;
    unsigned *trace;
} Receiver;

void credit(void *context, uint32_t amount) {
    Receiver *receiver = context;
    receiver->balance += amount;
    *receiver->trace = *receiver->trace * 10 + 1 + receiver->id;
}

void compensate(void *context, uint32_t amount) {
    Receiver *receiver = context;
    receiver->balance -= amount;
    *receiver->trace = *receiver->trace * 10 + 5 + receiver->id;
}

void audit(void *context, uint32_t amount) {
    Receiver *receiver = context;
    (void)amount;
    *receiver->trace = *receiver->trace * 10 + 3 + receiver->id;
}

typedef struct {
    unsigned trace;
    uint32_t addend;
    uint32_t factor;
} Route;

uint32_t add_value(void *context, uint32_t value) {
    Route *route = context;
    route->trace = route->trace * 10 + 1;
    return value + route->addend;
}

uint32_t multiply_value(void *context, uint32_t value) {
    Route *route = context;
    route->trace = route->trace * 10 + 2;
    return value * route->factor;
}

typedef struct {
    gof_door_state *state;
    unsigned trace;
    unsigned bias;
} Door;

void record_transition(void *context, uint32_t payload) {
    Door *door = context;
    door->trace = door->trace * 100 + 10 * (*door->state + door->bias) + payload;
}

typedef struct {
    unsigned method;
    uint32_t number_factor;
    uint32_t pair_factor;
} Visit;

uint32_t visit_number(void *context, uint32_t number) {
    Visit *visit = context;
    visit->method = 1;
    return number * visit->number_factor;
}

uint32_t visit_pair_sum(void *context, uint32_t first, uint32_t second) {
    Visit *visit = context;
    visit->method = 2;
    return first + second * visit->pair_factor;
}

uint32_t visit_pair_max(void *context, uint32_t first, uint32_t second) {
    Visit *visit = context;
    visit->method = 2;
    uint32_t scaled = second * visit->pair_factor;
    return first > scaled ? first : scaled;
}
"""


def make_profile(cases):
    profile = {
        "audit": {"pattern": "command", "apply": "audit", "undo": None},
        "door": {
            "pattern": "state",
            "states": ["closed", "open"],
            "events": {
                "open": [["closed", "open", "record_transition"]],
                "close": [["open", "closed", "record_transition"]],
            },
        },
    }
    undo_enabled, = cases["gof-command"]["config"]
    left, right = cases["gof-interpreter"]["config"]
    captured, = cases["gof-memento"]["config"]
    descending, = cases["gof-strategy"]["config"]
    fields = ("balance", "revision")
    for suffix, edited in (("base", False), ("edit", True)):
        profile[f"credit_{suffix}"] = {
            "pattern": "command", "apply": "credit", "undo": "compensate" if undo_enabled and not edited else None,
        }
        profile[f"expression_{suffix}"] = {
            "pattern": "interpreter",
            "expression": [
                "-" if edited else "+",
                ["*", ["value", 0], ["+", ["const", left], ["const", right]]],
                ["value", 1],
            ],
        }
        colleagues = ["add_value", "multiply_value"]
        profile[f"route_{suffix}"] = {
            "pattern": "mediator", "routes": {"empty": [], "checkout": colleagues[::-1] if edited else colleagues},
        }
        profile[f"capture_{suffix}"] = {"pattern": "memento", "snapshot": [fields[captured + edited]]}
        profile[f"sort_{suffix}"] = {
            "pattern": "strategy", "algorithm": "selection" if edited else "insertion",
            "order": "descending" if bool(descending) != edited else "ascending",
        }
        profile[f"visitor_{suffix}"] = {
            "pattern": "visitor", "number": "visit_number",
            "pair": "visit_pair_max" if edited else "visit_pair_sum",
        }
    return profile


def vector(values):
    return ", ".join(map(str, values or [0]))


def command_client(args, config, suffix):
    first, second, *requests = args
    declare = []
    invoke = []
    for index, request in enumerate(requests):
        operation, receiver, amount = request // 100, request // 10 % 10, request % 10
        constructor = "audit" if operation == 1 else f"credit_{suffix}"
        declare.append(f"gof_command request_{index} = gof_{constructor}(&receivers[{receiver}], {amount});")
        method = "undo" if operation == 2 else "apply"
        invoke.append(f"gof_command_{method}(request_{index});")
    declarations = "\n    ".join(declare)
    invocations = "\n    ".join(invoke)
    return rf"""
    unsigned trace = 0;
    Receiver receivers[] = {{{{0, {first}, &trace}}, {{1, {second}, &trace}}}};
    (void)receivers;
    {declarations}
    {invocations}
    printf("%u\n", trace * 100 + receivers[1].balance * 10 + receivers[0].balance);
"""


def interpreter_client(args, config, suffix):
    return rf"""
    const uint32_t values[] = {{{vector(args)}}};
    uint32_t result;
    bool ok = gof_expression_{suffix}(values, {len(args)}, &result);
    int64_t signed_result = -1;
    if (ok) signed_result = result <= INT32_MAX ? result : (int64_t)result - INT64_C(4294967296);
    printf("%" PRId64 "\n", signed_result);
"""


def mediator_client(args, config, suffix):
    route, value = args
    addend, factor = config
    event = "checkout" if route else "empty"
    return rf"""
    Route route = {{0, {addend}, {factor}}};
    uint32_t value = gof_route_{suffix}_{event}(&route, {value});
    printf("%" PRIu32 "\n", route.trace * 1000 + value);
"""


def memento_client(args, config, suffix):
    a, b, later_a, later_b, live_a, live_b, live_c, handle = args
    snapshot = f"gof_capture_{suffix}_snapshot"
    return rf"""
    gof_originator originator = {{{a}, {b}, 0, 0}};
    {snapshot} *first = gof_capture_{suffix}_capture(&originator);
    originator = (gof_originator){{{later_a}, {later_b}, 0, 0}};
    {snapshot} *second = gof_capture_{suffix}_capture(&originator);
    originator = (gof_originator){{{live_a}, {live_b}, {live_c}, 0}};
    if (!first || !second || !gof_capture_{suffix}_restore(&originator, {"second" if handle else "first"}))
        return 1;
    gof_capture_{suffix}_destroy(first);
    gof_capture_{suffix}_destroy(second);
    printf("%" PRIu32 "\n", originator.balance * 100 + originator.revision * 10 + originator.lease);
"""


def state_client(args, config, suffix):
    initial, *requests = args
    bias, = config
    calls = []
    for event, payload in zip(requests[::2], requests[1::2], strict=True):
        if event in (0, 1):
            name = "close" if event else "open"
            calls.append(f"gof_door_on_{name}(&state, &door, {payload});")
    event_calls = "\n    ".join(calls)
    return rf"""
    gof_door_state state = {initial};
    Door door = {{&state, 0, {bias}}};
    {event_calls}
    printf("%u\n", door.trace * 10 + state);
"""


def strategy_client(args, config, suffix):
    return rf"""
    uint32_t values[] = {{{vector(args)}}};
    size_t length = {len(args)};
    gof_sort_{suffix}(values, length);
    uint32_t packed = 0;
    for (size_t i = 0; i < length; ++i) packed = packed * 10 + values[i];
    printf("%" PRIu32 "\n", packed);
"""


def visitor_client(args, config, suffix):
    tag, first, second = args
    number_factor, pair_factor = config
    return rf"""
    gof_element element = {{.kind = {tag}}};
    if (element.kind == GOF_NUMBER) element.value.number = {first};
    if (element.kind == GOF_PAIR) {{
        element.value.pair.first = {first};
        element.value.pair.second = {second};
    }}
    Visit visit = {{0, {number_factor}, {pair_factor}}};
    uint32_t value;
    bool ok = gof_visitor_{suffix}(&element, &visit, &value);
    printf("%" PRId64 "\n", ok ? (int64_t)visit.method * 1000 + value : INT64_C(-1));
"""


CLIENTS = dict(zip(IDS, (
    command_client, interpreter_client, mediator_client, memento_client,
    state_client, strategy_client, visitor_client,
), strict=True))


def check(cases):
    """Verify original and edited outputs, with original-only state callbacks."""
    clients = []
    expected = []
    for slug, client in CLIENTS.items():
        case = cases[slug]
        for args, original, edited in case["samples"]:
            variants = [("base", original)]
            # The API always updates state before reaching the configured hook.
            if slug != "gof-state":
                variants.append(("edit", edited))
            for suffix, result in variants:
                clients.append("{" + client(args, case["config"], suffix) + "}")
                expected.append((slug, suffix, args, result))
    with TemporaryDirectory(prefix="projection-behavior-") as directory:
        work = Path(directory)
        release = work / "library"
        compile_library(make_profile(cases), release)
        source = work / "client.c"
        source.write_text(HOOKS + "\nint main(void) {\n" + "\n".join(clients) + "\nreturn 0;\n}\n")
        executable = work / "client"
        run([os.environ.get("CLANG", "clang"), *CFLAGS, "-I", release,
             source, release / "libgof.a", "-o", executable])
        observed = list(map(int, run([executable]).splitlines()))
    require(len(observed) == len(expected), "behavioral GoF C client returned the wrong number of outputs")
    for (slug, suffix, args, result), actual in zip(expected, observed, strict=True):
        require(actual == result, f"{slug}/{suffix}: GoF C API {args} returned {actual}, expected {result}")
    return len(expected)
