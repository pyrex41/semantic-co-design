"""Exercise the structural residuals through their linked public C11 ABI."""

import json
from pathlib import Path


CONSUMER = r'''
#include "gof.h"
#include <assert.h>

struct calls {
    uint32_t trace[8];
    unsigned count;
    uint32_t leaves[4];
};

static void record(struct calls *calls, uint32_t value) {
    assert(calls->count < 8);
    calls->trace[calls->count++] = value;
}

static void expect(struct calls *calls, const uint32_t *trace, unsigned count) {
    assert(calls->count == count);
    for (unsigned i = 0; i < count; i++)
        assert(calls->trace[i] == trace[i]);
    calls->count = 0;
}

#define EXPECT(calls, ...) \
    expect(calls, (const uint32_t[]){__VA_ARGS__}, \
           sizeof((const uint32_t[]){__VA_ARGS__}) / sizeof(uint32_t))

uint32_t st_accept_pair(void *context, uint32_t left, uint32_t right) {
    record(context, left);
    record(context, right);
    return left * 2u + right;
}

uint32_t st_channel_read(void *context, uint32_t channel, uint32_t request) {
    record(context, channel);
    record(context, request);
    return channel * 10u + request;
}

uint32_t st_other_channel(void *context, uint32_t channel, uint32_t request) {
    record(context, channel);
    record(context, request);
    return channel * 20u + request;
}

uint32_t st_read_leaf(void *context, uint32_t leaf) {
    struct calls *calls = context;
    record(calls, leaf);
    return calls->leaves[leaf];
}

uint32_t node_0(void *context, uint32_t leaf) {
    return st_read_leaf(context, leaf);
}

uint32_t st_component(void *context, uint32_t value) {
    record(context, value);
    return value + 1u;
}

int st_validate(void *context, uint32_t request) {
    record(context, 100u + request);
    return request > 2u;
}

int st_other_validate(void *context, uint32_t request) {
    record(context, 200u + request);
    return request > 4u;
}

uint32_t st_encode(void *context, uint32_t request) {
    record(context, 10u + request);
    return 2u * request;
}

int st_send(void *context, uint32_t request) {
    record(context, request);
    return request == 10u ? 7 : -1;
}

uint32_t st_authorized(void *context, uint32_t request) {
    record(context, request);
    return request + 4u;
}

int main(void) {
    struct calls calls = {{0}, 0, {0, 5, 8, 7}};
    uint32_t result = 51;

#if ST_VARIANT
    assert(gof_st_adapter(&calls, 0x12345678u) == 0x7ae0u);
    EXPECT(&calls, 0x1234u, 0x5678u);
    assert(gof_st_adapter(&calls, 0xffff0001u) == 131071u);
    EXPECT(&calls, 65535u, 1u);

    assert(gof_st_bridge_value(&calls, 8u) == 31u);
    assert(!gof_st_bridge_ready(&calls, 8u));
    assert(gof_st_bridge_ready(&calls, 10u));
    EXPECT(&calls, 1u, 8u, 1u, 8u, 1u, 10u);

    assert(gof_st_tree(&calls) == 13u);
    EXPECT(&calls, 1u, 2u, 3u);
    calls.leaves[3] = 20u;
    assert(gof_st_tree(&calls) == 20u);
    EXPECT(&calls, 1u, 2u, 3u);

    assert(gof_st_decorated(&calls, 4u) == 110u);
    EXPECT(&calls, 14u);
    assert(gof_st_decorated(&calls, 0u) == 50u);
    EXPECT(&calls, 2u);

    assert(gof_st_submit(&calls, 4u) == 0);
    EXPECT(&calls, 204u);
    assert(gof_st_submit(&calls, 5u) == 7);
    EXPECT(&calls, 205u, 15u, 10u);

    assert(gof_st_marks_get_primary() == gof_st_marks_get_main());
    assert(gof_st_marks_get_primary() != gof_st_marks_get_same());
    assert(gof_st_marks_get_same() == gof_st_marks_get_secondary());
    assert(gof_st_marks_apply(gof_st_marks_get_same(), 3u) == 15u);
    assert(gof_st_marks_apply(gof_st_marks_get_same(), 5u) == 19u);

    assert(!gof_st_guard(&calls, 31u, 9u, &result));
    assert(calls.count == 0 && result == 51u);
    assert(gof_st_guard(&calls, 0u, 9u, &result) && result == 13u);
    assert(gof_st_guard(&calls, 0u, 7u, &result) && result == 11u);
    EXPECT(&calls, 9u, 7u);
#else
    assert(gof_st_adapter(&calls, 0x12345678u) == 0x124u);
    EXPECT(&calls, 0x56u, 0x78u);
    assert(gof_st_adapter(&calls, 0x0000ff01u) == 511u);
    EXPECT(&calls, 255u, 1u);

    assert(gof_st_bridge_value(&calls, 8u) == 55u);
    assert(!gof_st_bridge_ready(&calls, 19u));
    assert(gof_st_bridge_ready(&calls, 20u));
    EXPECT(&calls, 4u, 8u, 4u, 19u, 4u, 20u);

    assert(gof_st_tree(&calls) == 18u);
    EXPECT(&calls, 1u, 2u, 3u, 1u);
    calls.leaves[3] = 20u;
    assert(gof_st_tree(&calls) == 30u);
    EXPECT(&calls, 1u, 2u, 3u, 1u);

    assert(gof_st_decorated(&calls, 4u) == 102u);
    EXPECT(&calls, 18u);
    assert(gof_st_decorated(&calls, 0u) == 42u);
    EXPECT(&calls, 6u);

    assert(gof_st_submit(&calls, 2u) == 0);
    EXPECT(&calls, 102u);
    assert(gof_st_submit(&calls, 5u) == 7);
    EXPECT(&calls, 105u, 15u, 10u);
    assert(gof_st_submit(&calls, 6u) == -1);
    EXPECT(&calls, 106u, 16u, 12u);

    assert(gof_st_marks_get_primary() == gof_st_marks_get_main());
    assert(gof_st_marks_get_primary() == gof_st_marks_get_same());
    assert(gof_st_marks_get_same() != gof_st_marks_get_secondary());
    assert(gof_st_marks_apply(gof_st_marks_get_same(), 3u) == 15u);
    assert(gof_st_marks_apply(gof_st_marks_get_same(), 5u) == 23u);

    assert(!gof_st_guard(&calls, 0u, 9u, &result));
    assert(calls.count == 0 && result == 51u);
    assert(gof_st_guard(&calls, 31u, 9u, &result) && result == 13u);
    assert(gof_st_guard(&calls, 1u, 7u, &result) && result == 11u);
    EXPECT(&calls, 9u, 7u);
#endif

    assert(!gof_st_guard(&calls, 32u, 99u, &result));
    assert(!gof_st_guard(&calls, UINT32_MAX, 99u, &result));
    assert(!gof_st_guard(&calls, 0u, 99u, 0));
    assert(calls.count == 0 && result == 11u);
    return 0;
}
'''


def check(work):
    from .check import run_case

    profile = json.loads(Path(__file__).with_name("structural_demo.json").read_text())
    run_case(
        work, "structural", profile, "#define ST_VARIANT 0\n" + CONSUMER,
        must_contain=(
            "return st_accept_pair(gof_arg_0, gof_arg_1, gof_arg_2);",
            "gof_hook_st_accept_pair(context, (packed >> 8u) & 255u, packed & 255u)",
            "gof_hook_st_channel_read(context, 4u, request) + 7u",
            "gof_hook_st_channel_read(context, 4u, request) >= 60u",
            "if (node_2 > node_1) node_1 = node_2;",
            "value = gof_hook_st_component(context, value);",
            "if (!gof_hook_st_validate(context, request)) return 0;",
            "return gof_hook_st_send(context, encoded);",
            "return &gof_st_marks_descriptor_0;",
            "2147483650u >> role",
        ),
        must_absent=("for (", "while (", "gof_st_marks_descriptor_2", "strcmp("),
    )

    profile["st_adapter"]["format"] = "pair16"
    profile["st_bridge"].update(
        backend={"read": "st_other_channel", "channel": 1}, adjustment=3, threshold=30,
    )
    profile["st_tree"]["read"] = "node_0"
    profile["st_tree"]["tree"] = {
        "op": "max", "children": [{"op": "sum", "children": [1, 2]}, 3],
    }
    profile["st_decorated"]["layers"].reverse()
    profile["st_submit"]["validate"] = "st_other_validate"
    profile["st_marks"]["descriptors"][1].update(scale=2, offset=9)
    profile["st_guard"]["roles"] = [0]
    run_case(
        work, "structural_variant", profile, "#define ST_VARIANT 1\n" + CONSUMER,
        must_contain=(
            "gof_hook_st_accept_pair(context, (packed >> 16u) & 65535u, packed & 65535u)",
            "gof_hook_st_other_channel(context, 1u, request) + 3u",
            "return node_0(gof_arg_0, gof_arg_1);",
            "uint32_t node_0 = gof_hook_node_0(context, 1u);",
            "node_0 += node_1;",
            "if (!gof_hook_st_other_validate(context, request)) return 0;",
            "return &gof_st_marks_descriptor_1;",
            "1u >> role",
        ),
        must_absent=("for (", "while (", "gof_hook_st_validate(context", "gof_hook_st_channel_read(context"),
    )
