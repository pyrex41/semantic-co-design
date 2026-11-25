"""Exercise the generated creational APIs from C11."""

import json
from pathlib import Path


def check(work):
    from .check import run_case

    demo = json.loads(Path(__file__).with_name("creational_demo.json").read_text())
    run_case(work, "creational-demo", demo, """#include <assert.h>
#include "gof.h"

static const gof_cr_limits_configuration *other_caller(void) {
    return gof_cr_limits_get();
}

int main(void) {
    gof_cr_offset_reader reader = gof_cr_offset_make_reader();
    gof_cr_offset_writer writer = gof_cr_offset_make_writer();
    assert(reader.family == 1u && writer.family == reader.family);
    assert(gof_cr_offset_write(writer, 31u) == 38u);
    assert(gof_cr_offset_read(reader, 38u) == 31u);
    assert(gof_cr_offset_read(reader, gof_cr_offset_write(writer, UINT32_MAX)) == UINT32_MAX);

    const uint32_t input[] = {8u, 2u, 17u, 11u, 3u};
    gof_cr_sequence_product sequence = gof_cr_sequence_build(input, 5u);
    assert(sequence.count == 4u);
    assert(sequence.values[0] == 8u && sequence.values[1] == 2u);
    assert(sequence.values[2] == 17u && sequence.values[3] == 11u);
    sequence = gof_cr_sequence_build(input, 1u);
    assert(sequence.count == 1u && sequence.values[0] == 8u && sequence.values[1] == 0u);
    sequence = gof_cr_sequence_build(0, 0u);
    assert(sequence.count == 0u && sequence.values[0] == 0u);

    assert(gof_cr_adjust_create(21u).value == 36u);
    assert(gof_cr_adjust_create(UINT32_MAX).value == 14u);

    gof_cr_prototype_product left = gof_cr_prototype_clone();
    gof_cr_prototype_product right = gof_cr_prototype_clone();
    assert(left.kind == 73u && left.values[0] == 11u);
    assert(left.values[1] == 17u && left.values[2] == 23u);
    left.kind = 0u;
    left.values[1] = 499u;
    assert(right.kind == 73u && right.values[1] == 17u);
    assert(gof_cr_prototype_clone().values[1] == 17u);

    const gof_cr_limits_configuration *config = gof_cr_limits_get();
    assert(config == other_caller());
    assert(gof_cr_limits_revision(config) == 5u && gof_cr_limits_limit(config) == 256u);
    return 0;
}
""", must_contain=(
        "return (gof_cr_offset_reader){1u, 7u};",
        "return (gof_cr_offset_writer){1u, 7u};",
        "gof_cr_sequence_add(&product, values[i]);",
        "gof_cr_adjust_make_offset(value + 4u)",
        "static const gof_cr_prototype_product gof_cr_prototype_exemplar",
        "static const gof_cr_limits_configuration gof_cr_limits_instance",
        "return &gof_cr_limits_instance;",
    ), must_absent=("gof_cr_adjust_make_scaled",))

    alternate = {
        "cr_mask": {"pattern": "abstract_factory", "family": "mask", "key": 165},
        "cr_summary": {"pattern": "builder", "representation": "summary", "capacity": 3},
        "cr_multiply": {"pattern": "factory_method", "product": "scaled", "prepare": 2, "parameter": 7, "finish": 6},
        "cr_other": {"pattern": "prototype", "kind": 0, "values": [4294967295]},
        "cr_config": {"pattern": "singleton", "revision": 0, "limit": 1024},
        "cr_secondary": {"pattern": "singleton", "revision": 0, "limit": 1024},
    }
    run_case(work, "creational-alternate", alternate, """#include <assert.h>
#include "gof.h"

int main(void) {
    gof_cr_mask_reader reader = gof_cr_mask_make_reader();
    gof_cr_mask_writer writer = gof_cr_mask_make_writer();
    assert(reader.family == 2u && writer.family == reader.family);
    assert(gof_cr_mask_write(writer, 115u) == 214u);
    assert(gof_cr_mask_read(reader, 214u) == 115u);

    const uint32_t input[] = {10u, 3u, 37u, 900u};
    gof_cr_summary_product summary = gof_cr_summary_build(input, 4u);
    assert(summary.count == 3u && summary.sum == 50u && summary.max == 37u && summary.mean == 16u);
    const uint32_t wide[] = {UINT32_MAX, UINT32_MAX, UINT32_MAX};
    summary = gof_cr_summary_build(wide, 3u);
    assert(summary.sum == UINT64_C(12884901885) && summary.mean == UINT32_MAX);
    summary = gof_cr_summary_build(0, 0u);
    assert(summary.count == 0u && summary.sum == 0u && summary.max == 0u && summary.mean == 0u);

    assert(gof_cr_multiply_create(9u).value == 83u);
    assert(gof_cr_multiply_create(UINT32_MAX).value == 13u);

    gof_cr_other_product clone = gof_cr_other_clone();
    assert(clone.kind == 0u && clone.values[0] == UINT32_MAX);
    clone.values[0] = 99u;
    assert(clone.values[0] == 99u && gof_cr_other_clone().values[0] == UINT32_MAX);

    const gof_cr_config_configuration *config = gof_cr_config_get();
    assert(config == gof_cr_config_get());
    assert(gof_cr_config_revision(config) == 0u && gof_cr_config_limit(config) == 1024u);
    assert((const void *)config != (const void *)gof_cr_secondary_get());
    return 0;
}
""", must_contain=(
        "gof_cr_summary_add(&product, values[i]);",
        "product->mean = (uint32_t)(product->sum / product->count);",
        "gof_cr_multiply_make_scaled(value + 2u)",
        "return gof_cr_other_exemplar;",
    ), must_absent=("gof_cr_multiply_make_offset", "product->values[product->count]"))
