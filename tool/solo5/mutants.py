"""Deliberately wrong drivers and specifications, for the teeth of tool/solo5/check.py.

A driver mutant is one textual edit of driver.c. Each breaks one thing a specification
relies on from its effect shim (tool/README.md, "Environment rules") or one piece of the
marshalling, and the comparison with the simulator has to reject a run of it.

A specification mutant is one edit of a node's Shen source; the image built from it has
to be rejected against the simulator running the unedited node.
"""

DRIVERS = {
    # The acknowledgement is queued but the row never reaches the device.
    "ack-without-write": {
        "old": "    if (solo5_block_write(disk, offset, block, disk_info.block_size) != SOLO5_R_OK)\n"
               "        fail(\"block-write\");\n",
        "new": "    (void)offset;\n"},
    # A row is not written whole: its last integer stays zero.
    "torn-row": {
        "old": "for (int i = 2; i < width; ++i) pack(block + 8 * (i - 1), fields[i]);",
        "new": "for (int i = 2; i < width - 1; ++i) pack(block + 8 * (i - 1), fields[i]);"},
    # A read does not see what is on the device.
    "stale-read": {
        "old": "int64_t present = unpack(block);",
        "new": "int64_t present = 0 * unpack(block);"},
    # The node dies one effect later than the point it reports.
    "late-cut": {
        "old": "        if (events == hold_event && k == hold_count) hold(k);\n"
               "        const struct kind *kind = find(EFFECTS, NEFFECTS, output[at]);\n"
               "        if (kind == NULL) fail(\"effect-tag\");\n"
               "        perform(kind, output + at + 1);\n",
        "new": "        const struct kind *kind = find(EFFECTS, NEFFECTS, output[at]);\n"
               "        if (kind == NULL) fail(\"effect-tag\");\n"
               "        perform(kind, output + at + 1);\n"
               "        if (events == hold_event && k == hold_count) hold(k);\n"},
    # The step's new state is thrown away.
    "keeps-old-state": {
        "old": "    for (int i = 0; i < STATE_WIDTH; ++i) state[i] = output[i];\n",
        "new": ""},
    # An output is reported as performed and never sent.
    "swallows-output": {
        "old": "    (void)solo5_net_write(net, frame, size < FRAME_MIN ? FRAME_MIN : size);",
        "new": "    (void)size;"},
    # A frame with a field outside the declared kind reaches the step.
    "accepts-any-field": {
        "old": "                        : leaf == 'b' ? v != 0 && v != 1 : v < 1 || v > SYMBOLS) valid = 0;",
        "new": "                        : leaf == 'b' ? v != 0 && v != 1 : v < 1 || v > SYMBOLS) valid = 1;"},
    # A timer fires as soon as the node is idle, whatever its delay.
    "early-timer": {
        "old": "timers[i].due = now + (delay > 0 ? delay : 0);",
        "new": "timers[i].due = now;"},
    # A cancelled timer still fires.
    "ignores-cancel": {
        "old": "    case R_TIMER_CANCEL: timer_clear(fields[0]); break;",
        "new": "    case R_TIMER_CANCEL: break;"},
    # The clock a step is given is not the clock.
    "stale-clock": {
        "old": "        int64_t event[3] = {EV_CLOCK, fields[0], now};",
        "new": "        int64_t event[3] = {EV_CLOCK, fields[0], now - 1};"},
    # A unit of specification time is half a millisecond: every timer fires after half
    # its delay, and the driver's own log is consistent with itself.
    "half-tick": {
        "old": "#define TICK_NS UINT64_C(1000000) ",
        "new": "#define TICK_NS UINT64_C(500000) "},
    # ... or two milliseconds: the clock a step reads runs at half speed.
    "slow-tick": {
        "old": "#define TICK_NS UINT64_C(1000000) ",
        "new": "#define TICK_NS UINT64_C(2000000) "},
    # The deadline of a wait is computed in nanoseconds, where a far due time wraps to a
    # past instant: the wait returns at once, for ever. (The driver as it was.)
    "wrapping-deadline": {
        "old": "        if (timer >= 0 && (uint64_t)timers[timer].due < deadline / TICK_NS)\n",
        "new": "        if (timer >= 0 && (uint64_t)timers[timer].due * TICK_NS < deadline)\n"},
    # A block write that failed is acknowledged all the same.
    "acks-failed-write": {
        "old": "    if (solo5_block_write(disk, offset, block, disk_info.block_size) != SOLO5_R_OK)\n"
               "        fail(\"block-write\");\n",
        "new": "    (void)solo5_block_write(disk, offset, block, disk_info.block_size);\n"},
    # A block read that failed yields whatever the buffer held.
    "ignores-failed-read": {
        "old": "    if (solo5_block_read(disk, block_offset(fields[1]), block, disk_info.block_size) != SOLO5_R_OK)\n"
               "        fail(\"block-read\");\n",
        "new": "    (void)solo5_block_read(disk, block_offset(fields[1]), block, disk_info.block_size);\n"},
    # A read of a row the same step wrote is answered by the device, with the new row.
    # (The driver as it was: no simulated schedule gives that answer.)
    "reads-own-write": {
        "old": "        if (written[i] == fields[1]) fail(\"read-after-write\");\n",
        "new": "        (void)i;\n"},
    # Semantics in the shim, 1: the loop remembers the last request and drops a repeat.
    "dedupes-requests": {
        "old": "                if (valid) {\n"
               "                    handle(event, 1 + kind->width);\n",
        "new": "                static int64_t last[1 + EVENT_WIDTH];\n"
               "                static int have;\n"
               "                int repeat = have;\n"
               "                for (int i = 0; i < 1 + kind->width; ++i)\n"
               "                    if (last[i] != event[i]) repeat = 0;\n"
               "                if (valid && repeat) {\n"
               "                    text(\"@skip duplicate\");\n"
               "                    number((int64_t)size);\n"
               "                    end();\n"
               "                    return;\n"
               "                }\n"
               "                if (valid) {\n"
               "                    for (int i = 0; i < 1 + kind->width; ++i) last[i] = event[i];\n"
               "                    have = 1;\n"
               "                    handle(event, 1 + kind->width);\n"},
    # Semantics in the shim, 2: the loop sends every output twice, in case one is lost.
    "repeats-output": {
        "old": "    (void)solo5_net_write(net, frame, size < FRAME_MIN ? FRAME_MIN : size);",
        "new": "    (void)solo5_net_write(net, frame, size < FRAME_MIN ? FRAME_MIN : size);\n"
               "    (void)solo5_net_write(net, frame, size < FRAME_MIN ? FRAME_MIN : size);"},
}


def specs():
    """Image name -> (old, new) edit of the node's Shen source."""
    from ..examples.accepted_job.check import MUTANTS
    from ..sim_check import TRAPPING
    reply = MUTANTS["reply-before-ack"]
    return {
        # AcceptedJob answering in the step that issues the write, after the write. In
        # the simulator the row is still pending when the reply is made. On this shim it
        # is not: the block write has returned before the next effect starts.
        "accepted_job~reply-before-ack": (reply["old"], reply["new"]),
        # AcceptedJob answering first and writing second. Killed between the two, the
        # client holds `ok` and the block image has no row, on any shim.
        "accepted_job~reply-then-write": (reply["old"],
                                          "[[reply Req ok Slot] [write 0 Slot Key Digest]]"),
        # The simulator's probe without a clause for an acknowledgement: the first one
        # is a Shen error.
        "probe~no-ack-clause": (TRAPPING, ")"),
    }
