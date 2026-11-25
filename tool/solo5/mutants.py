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
}


def specs():
    """Image name -> (old, new) edit of the node's Shen source."""
    from ..examples.accepted_job.check import MUTANTS
    from ..sim_check import TRAPPING
    reply = MUTANTS["reply-before-ack"]
    return {
        # AcceptedJob answering before its row is acknowledged.
        "accepted_job~reply-before-ack": (reply["old"], reply["new"]),
        # The simulator's probe without a clause for an acknowledgement: the first one
        # is a Shen error.
        "probe~no-ack-clause": (TRAPPING, ")"),
    }
