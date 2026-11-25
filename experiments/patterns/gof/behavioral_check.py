"""Independent behavioral assertions from a C consumer of two profiles."""

from .build import HERE, read_profile


CLIENT = r"""
#include "gof.h"
#include <assert.h>
#include <stdlib.h>

typedef struct {
    uint32_t balance;
    uint32_t audit;
    uint32_t seen;
    unsigned order;
    gof_bh_publish_registry *registry;
} Context;

void bh_add(void *p, uint32_t n) { ((Context *)p)->balance += n; }
void bh_subtract(void *p, uint32_t n) { ((Context *)p)->balance -= n; }
void bh_audit(void *p, uint32_t n) { ((Context *)p)->audit = n; }

uint32_t bh_hold(void *p, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 1;
    return n + 9;
}
uint32_t bh_confirm(void *p, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 2;
    return n * 3;
}
uint32_t bh_release(void *p, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 3;
    return n ^ 5;
}

void bh_account(void *p, uint32_t tag, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 4;
    context->balance = tag + n;
    gof_bh_publish_subscribe_audit(context->registry, false);
}
void bh_log(void *p, uint32_t tag, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 5;
    context->audit = tag * n;
}
void bh_admit(void *p, uint32_t n) { ((Context *)p)->seen = n; }

uint32_t bh_prepare(void *p, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 6;
    return n + 2;
}
uint32_t bh_compute(void *p, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 7;
    return n * 4;
}
void bh_finish(void *p, bool ran, uint32_t n) {
    Context *context = p;
    context->order = context->order * 10 + 8;
    context->seen = n;
    context->audit = ran;
}

uint32_t bh_number(void *p, uint32_t n) {
    ((Context *)p)->seen = 1;
    return n + 1;
}
uint32_t bh_number_alt(void *p, uint32_t n) {
    ((Context *)p)->seen = 3;
    return n - 1;
}
uint32_t bh_pair(void *p, uint32_t x, uint32_t y) {
    ((Context *)p)->seen = 2;
    return x + y;
}

static int compare_words(const void *left, const void *right) {
    uint32_t x = *(const uint32_t *)left;
    uint32_t y = *(const uint32_t *)right;
    return (x > y) - (x < y);
}

int main(void) {
    for (uint32_t n = 0; n < 200; ++n) {
        const int64_t expected = ALT ? n >= 10 : n >= 100 ? 2 : n >= 10;
        assert(gof_bh_dispatch(&n, 1) == expected);
    }
    assert(gof_bh_dispatch(NULL, 0) == -1);
    assert(gof_bh_dispatch(NULL, 1) == -1);

    Context first = {0}, second = {.balance = 100};
    gof_command queue[] = {gof_bh_credit(&first, 13), gof_bh_audit(&first, 88), gof_bh_credit(&second, 55)};
    assert(first.balance == 0 && second.balance == 100 && first.audit == 0);
    for (size_t i = 0; i < sizeof queue / sizeof *queue; ++i) gof_command_apply(queue[i]);
    assert(first.balance == (ALT ? UINT32_MAX - 12 : 13));
    assert(second.balance == (ALT ? 45 : 155));
    assert(first.audit == 88);
    assert(gof_command_undo(queue[0]) && gof_command_undo(queue[2]) && !gof_command_undo(queue[1]));
    assert(first.balance == 0 && second.balance == 100 && first.audit == 88);

    uint32_t result = 88;
    assert(!gof_bh_invoice(NULL, 2, &result) && result == 88);
    assert(gof_bh_constant(NULL, 0, &result) && result == UINT32_MAX - 2);
    const uint32_t operands[] = {0, 1, 1234, UINT32_MAX - 1, UINT32_MAX};
    for (size_t i = 0; i < sizeof operands / sizeof *operands; ++i) {
        for (size_t j = 0; j < sizeof operands / sizeof *operands; ++j) {
            const uint32_t input[] = {operands[i], operands[j]};
            const uint32_t expected = ALT ? (input[1] - input[0]) ^ 7u : input[0] * 5u + input[1];
            assert(gof_bh_invoice(input, 2, &result) && result == expected);
            assert(!gof_bh_invoice(input, 1, &result) && result == expected);
        }
    }

    const uint32_t records[] = {1, 2, 3, 4, 5, 6, 7};
    gof_bh_reading_cursor iterator = gof_bh_reading_begin(records, 7);
    gof_bh_reading_cursor another = gof_bh_reading_begin(records + 1, 6);
    assert(gof_bh_reading_next(&iterator, &result) && result == (ALT ? 1u : 2u));
    assert(gof_bh_reading_next(&another, &result) && result == (ALT ? 2u : 3u));
    assert(!gof_bh_reading_next(&iterator, NULL));
    assert(gof_bh_reading_next(&iterator, &result) && result == (ALT ? 3u : 5u));
    if (ALT) assert(gof_bh_reading_next(&iterator, &result) && result == 5u);
    assert(!gof_bh_reading_next(&iterator, &result) && result == 5u);
    iterator = gof_bh_reading_begin(NULL, 10);
    assert(!gof_bh_reading_next(&iterator, &result));

    first.order = 0;
    assert(gof_bh_checkout_open(&first, 4) == (ALT ? 21u : 39u));
    assert(first.order == (ALT ? 21u : 12u));
    assert(gof_bh_checkout_cancel(&first, 6) == (ALT ? 6u : 3u));
    assert(first.order == (ALT ? 21u : 123u));

    gof_originator originator = {.balance = 55, .revision = 6, .lease = 29, .dirty = 1};
    gof_bh_checkpoint_snapshot *earlier = gof_bh_checkpoint_capture(&originator);
    originator = (gof_originator){.balance = 70, .revision = 7, .lease = 34, .dirty = 2};
    gof_bh_checkpoint_snapshot *later = gof_bh_checkpoint_capture(&originator);
    assert(earlier && later && earlier != later);
    originator = (gof_originator){.balance = 88, .revision = 8, .lease = 39, .dirty = 3};
    assert(gof_bh_checkpoint_restore(&originator, earlier));
    assert(originator.balance == (ALT ? 88u : 55u) && originator.revision == (ALT ? 8u : 6u));
    assert(originator.lease == (ALT ? 29u : 39u) && originator.dirty == (ALT ? 1u : 3u));
    assert(gof_bh_checkpoint_restore(&originator, later));
    assert(originator.balance == (ALT ? 88u : 70u) && originator.revision == (ALT ? 8u : 7u));
    assert(originator.lease == (ALT ? 34u : 39u) && originator.dirty == (ALT ? 2u : 3u));
    assert(gof_bh_checkpoint_capture(NULL) == NULL && !gof_bh_checkpoint_restore(&originator, NULL));
    gof_bh_checkpoint_destroy(earlier);
    gof_bh_checkpoint_destroy(later);

    gof_bh_publish_registry registry = {0};
    first = (Context){.registry = &registry};
    gof_bh_publish_notify(&registry, &first, 7, 95);
    assert(first.order == 0);
    gof_bh_publish_subscribe_account(&registry, true);
    gof_bh_publish_subscribe_audit(&registry, true);
    gof_bh_publish_subscribe_audit(&registry, true);
    gof_bh_publish_notify(&registry, &first, 7, 6);
    assert(first.order == (ALT ? 5u : 45u) && first.audit == 42u);
    gof_bh_publish_notify(&registry, &first, 9, 10);
    assert(first.order == (ALT ? 554u : 45u));
    assert(first.balance == (ALT ? 19u : 13u));
    gof_bh_publish_notify(&registry, &first, 9, 11);
    assert(first.order == (ALT ? 5544u : 45u));
    gof_bh_publish_notify(&registry, &first, 32, 0);
    assert(first.order == (ALT ? 5544u : 45u));

    first.seen = 0;
    gof_bh_door_state state = gof_bh_door_state_sealed;
    assert(!gof_bh_door_on_revoke(&state, &first, 55));
    assert(gof_bh_door_on_release(&state, &first, 27));
    assert(state == (ALT ? gof_bh_door_state_revoked : gof_bh_door_state_released));
    assert(first.seen == (ALT ? 0u : 27u));
    assert(!gof_bh_door_on_release(&state, &first, 55));
    assert(gof_bh_door_on_revoke(&state, &first, 6) == !ALT);
    assert(state == gof_bh_door_state_revoked);
    state = (gof_bh_door_state)99;
    assert(!gof_bh_door_on_release(&state, &first, 55));

    for (uint32_t rotation = 0; rotation < 29; ++rotation) {
        uint32_t data[13], ordered[13];
        for (size_t i = 0; i < 13; ++i) data[i] = ordered[i] = (rotation * i + 10 * i * i) % 29;
        qsort(ordered, 13, sizeof *ordered, compare_words);
        gof_bh_sort(data, 13);
        for (size_t i = 0; i < 13; ++i) assert(data[i] == ordered[ALT ? 12 - i : i]);
    }
    gof_bh_sort(NULL, 0);

    first.order = 0;
    result = 88;
    assert(gof_bh_job(&first, 3, &result) == ALT);
    assert(first.order == (ALT ? 678u : 68u) && first.audit == ALT);
    assert(first.seen == (ALT ? 20u : 5u) && result == (ALT ? 20u : 88u));
    first.order = 0;
    assert(gof_bh_job(&first, 15, &result) && first.order == 678u && result == 68u);

    const gof_element number = {.kind = GOF_NUMBER, .value.number = 55};
    const gof_element pair = {.kind = GOF_PAIR, .value.pair = {123, 61}};
    assert(gof_bh_visit(&number, &first, &result) && result == (ALT ? 54u : 56u));
    assert(first.seen == (ALT ? 3u : 1u));
    assert(gof_bh_visit(&pair, &first, &result) && result == 184u && first.seen == 2u);
    assert(!gof_bh_visit(NULL, &first, &result) && result == 184u);
    return 0;
}
"""


def check(work):
    from .check import run_case

    profile = read_profile(HERE / "behavioral_demo.json")
    run_case(work, "behavioral", profile, "#define ALT 0\n" + CLIENT,
             must_contain=("INT64_C(100) <= gof_chain_read_at", "(uint64_t)5u", "*result = 4294967293u;",
                           "cursor->current[1]", "*state = gof_bh_door_state_released;",
                           "snapshot->balance = originator->balance;", "while (j && value < values[j - 1])",
                           "event = gof_hook_bh_hold(context, event);", "bool ran = prepared >= 10u;"),
             must_absent=("snapshot->lease = originator->lease;", "gof_hook_bh_number_alt", "strcmp("))

    profile["bh_dispatch"]["handlers"].reverse()
    profile["bh_credit"].update(apply="bh_subtract", undo="bh_add")
    profile["bh_invoice"]["expression"] = ["^", ["-", ["value", 1], ["value", 0]], ["const", 7]]
    profile["bh_reading"].update(stride=2, offset=0)
    profile["bh_checkout"]["routes"] = {"open": ["bh_confirm", "bh_hold"], "cancel": []}
    profile["bh_checkpoint"]["snapshot"] = ["lease", "dirty"]
    subscribers = profile["bh_publish"]["subscribers"]
    subscribers["account"]["tags"] = [9]
    profile["bh_publish"]["subscribers"] = dict(reversed(list(subscribers.items())))
    profile["bh_door"]["events"]["release"] = [["sealed", "revoked", None]]
    profile["bh_sort"].update(algorithm="selection", order="descending")
    profile["bh_job"]["minimum"] = 0
    profile["bh_visit"]["number"] = "bh_number_alt"
    run_case(work, "behavioral-alternate", profile, "#define ALT 1\n" + CLIENT,
             must_contain=("INT64_C(10) <= gof_chain_read_at", "cursor->current[0]", "bool ran = true;",
                           "snapshot->lease = originator->lease;", "if (values[j] > values[best])",
                           "*state = gof_bh_door_state_revoked;", "gof_hook_bh_number_alt(context,"),
             must_absent=("snapshot->balance = originator->balance;", "gof_hook_bh_release(context, event);",
                          "gof_hook_bh_admit(context, payload);", "while (j && value", "strcmp("))
