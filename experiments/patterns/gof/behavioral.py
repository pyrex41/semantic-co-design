"""Emit C for fixed behavioral configuration with live state and callbacks."""

import operator

from experiments.patterns import build as classic
from specializer import specialize
from specializer.backend import lower_c_body

from .common import choice, fields, identifier, items, word


def chain(unit, name, config):
    fields(config, "handlers", "default")
    for handler in items(config["handlers"]):
        if len(items(handler, 2)) != 2:
            raise ValueError("each handler needs a threshold and result")
    program = specialize("patterns", "shen", classic.encode(config), directory=classic.HERE)
    lines, banks = lower_c_body(program)
    if banks:
        raise ValueError("handler chain unexpectedly requires scratch banks")
    if any("read_at(" in line for line in lines):
        unit.source("""static inline int64_t gof_chain_read_at(const uint32_t *input, int64_t count, int64_t index) {
    return index >= 0 && index < count ? input[index] : 0;
}""")
    unit.define(f"int64_t {name}(const uint32_t *input, size_t length)", [
        "if (length > UINT32_MAX || (length && !input)) return -1;",
        "int64_t count = (int64_t)length;",
        *(line[4:].replace("emit(", "return (").replace("read_at(", "gof_chain_read_at(") for line in lines),
    ])


def command(unit, name, config):
    fields(config, "apply", "undo")
    apply = unit.hook(config["apply"], "void", "void *, uint32_t")
    undo = config["undo"]
    reverse = "NULL" if undo is None else unit.hook(undo, "void", "void *, uint32_t")
    unit.declare("""typedef struct {
    void (*apply)(void *, uint32_t);
    void (*undo)(void *, uint32_t);
    void *receiver;
    uint32_t argument;
} gof_command;

static inline void gof_command_apply(gof_command request) {
    request.apply(request.receiver, request.argument);
}

static inline bool gof_command_undo(gof_command request) {
    if (!request.undo) return false;
    request.undo(request.receiver, request.argument);
    return true;
}""")
    unit.define(f"gof_command {name}(void *receiver, uint32_t argument)", [
        f"return (gof_command){{{apply}, {reverse}, receiver, argument}};",
    ])


OPERATORS = {"+": operator.add, "-": operator.sub, "*": operator.mul, "^": operator.xor}


def interpreter(unit, name, config):
    fields(config, "expression")
    required = 0
    visited = 0

    def compile_node(node):
        nonlocal required, visited
        visited += 1
        if visited > 32:
            raise ValueError("the expression can contain at most 32 nodes")
        node = items(node, 3)
        if len(node) == 2 and node[0] == "const":
            return word(node[1])
        if len(node) == 2 and node[0] == "value":
            index = word(node[1])
            if index >= 32:
                raise ValueError("value index must be less than 32")
            required = max(required, index + 1)
            return f"values[{index}]"
        if len(node) != 3:
            raise ValueError("expected a constant, value index, or binary operation")
        op = choice(node[0], *OPERATORS)
        left, right = compile_node(node[1]), compile_node(node[2])
        if isinstance(left, int) and isinstance(right, int):
            return OPERATORS[op](left, right) % 2**32
        return f"(uint32_t)((uint64_t){literal(left)} {op} (uint64_t){literal(right)})"

    def literal(value):
        return f"{value}u" if isinstance(value, int) else value

    expr = literal(compile_node(config["expression"]))
    if required:
        guard = [f"if (!result || !values || count < {required}u) return false;"]
    else:
        guard = ["(void)values;", "(void)count;", "if (!result) return false;"]
    unit.define(f"bool {name}(const uint32_t *values, size_t count, uint32_t *result)", [
        *guard, f"*result = {expr};", "return true;",
    ])


def iterator(unit, name, config):
    fields(config, "stride", "offset")
    stride, offset = word(config["stride"]), word(config["offset"])
    if not offset < stride:
        raise ValueError("expected offset smaller than stride")
    cursor = f"{name}_cursor"
    unit.declare(f"typedef struct {{\n    const uint32_t *current;\n    size_t remaining;\n}} {cursor};")
    unit.define(f"{cursor} {name}_begin(const uint32_t *values, size_t length)", [
        f"return ({cursor}){{values, values ? length / {stride}u : 0}};",
    ])
    unit.define(f"bool {name}_next({cursor} *cursor, uint32_t *value)", [
        "if (!cursor || !value || !cursor->remaining) return false;",
        f"*value = cursor->current[{offset}];",
        f"if (--cursor->remaining) cursor->current += {stride}u;",
        "return true;",
    ])


def mapping(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise ValueError("expected a mapping of 1 to 32 names")
    return [(identifier(key), entry) for key, entry in value.items()]


def mediator(unit, name, config):
    fields(config, "routes")
    for event, participants in mapping(config["routes"]):
        steps = [unit.hook(participant, "uint32_t", "void *, uint32_t")
                 for participant in items(participants)]
        unit.define(f"uint32_t {name}_{event}(void *context, uint32_t event)", [
            *([f"event = {step}(context, event);" for step in steps] or ["(void)context;"]),
            "return event;",
        ])


ORIGINATOR_FIELDS = ("balance", "revision", "lease", "dirty")


def memento(unit, name, config):
    fields(config, "snapshot")
    columns = [choice(column, *ORIGINATOR_FIELDS) for column in items(config["snapshot"], 4)]
    if not columns or len(set(columns)) != len(columns):
        raise ValueError("select at least one field without duplicates")
    snapshot = f"{name}_snapshot"
    unit.declare("typedef struct {\n" + "\n".join(f"    uint32_t {col};" for col in ORIGINATOR_FIELDS)
                 + "\n} gof_originator;")
    unit.declare(f"typedef struct {snapshot} {snapshot};")
    unit.source(f"struct {snapshot} {{\n" + "\n".join(f"    uint32_t {col};" for col in columns) + "\n};")
    unit.define(f"{snapshot} *{name}_capture(const gof_originator *originator)", [
        "if (!originator) return NULL;", f"{snapshot} *snapshot = malloc(sizeof *snapshot);",
        "if (!snapshot) return NULL;",
        *(f"snapshot->{col} = originator->{col};" for col in columns), "return snapshot;",
    ])
    unit.define(f"bool {name}_restore(gof_originator *originator, const {snapshot} *snapshot)", [
        "if (!originator || !snapshot) return false;",
        *(f"originator->{col} = snapshot->{col};" for col in columns), "return true;",
    ])
    unit.define(f"void {name}_destroy({snapshot} *snapshot)", ["free(snapshot);"])


def observer(unit, name, config):
    fields(config, "subscribers")
    subscribers = mapping(config["subscribers"])
    registry = f"{name}_registry"
    unit.declare(f"typedef struct {{ uint32_t active; }} {registry};")
    notices = []
    for position, (key, subscriber) in enumerate(subscribers):
        fields(subscriber, "notify", "tags")
        mask = 0
        for tag in items(subscriber["tags"]):
            tag = word(tag)
            if tag >= 32:
                raise ValueError("notification tags must be less than 32")
            mask |= 1 << tag
        if not mask:
            raise ValueError("each subscriber must accept at least one event tag")
        hook = unit.hook(subscriber["notify"], "void", "void *, uint32_t, uint32_t")
        bit = 1 << position
        unit.define(f"void {name}_subscribe_{key}({registry} *registry, bool active)", [
            "if (!registry) return;",
            f"if (active) registry->active |= {bit}u;",
            f"else registry->active &= ~{bit}u;",
        ])
        notices.append(f"if ((active & {bit}u) && (event & {mask}u)) {hook}(context, tag, payload);")
    unit.define(f"void {name}_notify(const {registry} *registry, void *context, uint32_t tag, uint32_t payload)", [
        "if (!registry || tag >= 32u) return;",
        "uint32_t active = registry->active;",
        "uint32_t event = UINT32_C(1) << tag;",
        *notices,
    ])


def state(unit, name, config):
    fields(config, "states", "events")
    states = [identifier(state) for state in items(config["states"])]
    if not states or len(set(states)) != len(states):
        raise ValueError("states must be nonempty and unique")
    kind = f"{name}_state"
    unit.declare("typedef enum {\n" + ",\n".join(f"    {kind}_{state} = {i}" for i, state in enumerate(states))
                 + f"\n}} {kind};")
    for event, transitions in mapping(config["events"]):
        branches = []
        seen = set()
        for row in items(transitions):
            if len(items(row, 3)) != 3:
                raise ValueError("each transition needs a source, destination and hook or null")
            old, new, action = row
            choice(old, *states)
            choice(new, *states)
            if old in seen:
                raise ValueError(f"duplicate transition for state {old!r} in event {event!r}")
            seen.add(old)
            branches.extend((f"case {kind}_{old}:", f"    *state = {kind}_{new};"))
            if action is not None:
                hook = unit.hook(action, "void", "void *, uint32_t")
                branches.append(f"    {hook}(context, payload);")
            branches.append("    return true;")
        unit.define(f"bool {name}_on_{event}({kind} *state, void *context, uint32_t payload)", [
            "(void)context;", "(void)payload;", "if (!state) return false;", "switch (*state) {",
            *branches, "default:", "    return false;", "}",
        ])


def strategy(unit, name, config):
    fields(config, "algorithm", "order")
    algorithm = choice(config["algorithm"], "insertion", "selection")
    ahead = "<" if choice(config["order"], "ascending", "descending") == "ascending" else ">"
    if algorithm == "insertion":
        body = [
            "for (size_t i = 1; i < count; ++i) {", "    uint32_t value = values[i];", "    size_t j = i;",
            f"    while (j && value {ahead} values[j - 1]) {{", "        values[j] = values[j - 1];",
            "        --j;", "    }", "    values[j] = value;", "}",
        ]
    else:
        body = [
            "for (size_t i = 0; i < count; ++i) {", "    size_t best = i;",
            "    for (size_t j = i + 1; j < count; ++j) {",
            f"        if (values[j] {ahead} values[best]) best = j;", "    }",
            "    uint32_t value = values[i];", "    values[i] = values[best];", "    values[best] = value;", "}",
        ]
    unit.define(f"void {name}(uint32_t *values, size_t count)", ["if (!values) return;", *body])


def template_method(unit, name, config):
    fields(config, "prepare", "compute", "finish", "minimum")
    prepare = unit.hook(config["prepare"], "uint32_t", "void *, uint32_t")
    compute = unit.hook(config["compute"], "uint32_t", "void *, uint32_t")
    finish = unit.hook(config["finish"], "void", "void *, bool, uint32_t")
    minimum = word(config["minimum"])
    admit = f"prepared >= {minimum}u" if minimum else "true"
    unit.define(f"bool {name}(void *context, uint32_t input, uint32_t *result)", [
        "if (!result) return false;", f"uint32_t prepared = {prepare}(context, input);", f"bool ran = {admit};",
        f"uint32_t value = ran ? {compute}(context, prepared) : prepared;",
        f"{finish}(context, ran, value);", "if (ran) *result = value;", "return ran;",
    ])


def visitor(unit, name, config):
    fields(config, "number", "pair")
    number = unit.hook(config["number"], "uint32_t", "void *, uint32_t")
    pair = unit.hook(config["pair"], "uint32_t", "void *, uint32_t, uint32_t")
    unit.declare("""typedef struct {
    enum { GOF_NUMBER, GOF_PAIR } kind;
    union {
        uint32_t number;
        struct { uint32_t first; uint32_t second; } pair;
    } value;
} gof_element;""")
    unit.define(f"bool {name}(const gof_element *element, void *context, uint32_t *result)", [
        "if (!element || !result) return false;", "switch (element->kind) {", "case GOF_NUMBER:",
        f"    *result = {number}(context, element->value.number);", "    return true;", "case GOF_PAIR:",
        f"    *result = {pair}(context, element->value.pair.first, element->value.pair.second);",
        "    return true;", "default:", "    return false;", "}",
    ])


SHARED_NAMES = {"command", "command_apply", "command_undo", "element", "originator"}

GENERATORS = {
    "chain_of_responsibility": chain,
    "command": command,
    "interpreter": interpreter,
    "iterator": iterator,
    "mediator": mediator,
    "memento": memento,
    "observer": observer,
    "state": state,
    "strategy": strategy,
    "template_method": template_method,
    "visitor": visitor,
}
