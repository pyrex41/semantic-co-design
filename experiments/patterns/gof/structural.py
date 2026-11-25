"""Structural specializations and their residual C11 interfaces."""

from .common import choice, fields, identifier, items, word


def adapter(unit, name, config):
    fields(config, "format", "target")
    shift = {"pair8": 8, "pair16": 16}[choice(config["format"], "pair8", "pair16")]
    target = identifier(config["target"])
    mask = (1 << shift) - 1
    target = unit.hook(target, "uint32_t", "void *, uint32_t, uint32_t")
    unit.define(
        f"uint32_t {name}(void *context, uint32_t packed)",
        f"return {target}(context, (packed >> {shift}u) & {mask}u, packed & {mask}u);",
    )


def bridge(unit, name, config):
    fields(config, "backend", "adjustment", "threshold")
    backend = config["backend"]
    fields(backend, "read", "channel")
    read = identifier(backend["read"])
    channel = word(backend["channel"])
    adjustment = word(config["adjustment"])
    threshold = word(config["threshold"])
    read = unit.hook(read, "uint32_t", "void *, uint32_t, uint32_t")
    call = f"{read}(context, {channel}u, request)"
    unit.define(
        f"uint32_t {name}_value(void *context, uint32_t request)",
        f"return {call} + {adjustment}u;",
    )
    unit.define(
        f"int {name}_ready(void *context, uint32_t request)",
        f"return {call} >= {threshold}u;",
    )


def composite(unit, name, config):
    fields(config, "read", "tree")
    read = unit.hook(identifier(config["read"]), "uint32_t", "void *, uint32_t")
    statements = []

    def emit(node, depth):
        if depth > 8 or len(statements) >= 128:
            raise ValueError("composite tree exceeds the depth or statement limit")
        if type(node) is int:
            result = f"node_{len(statements)}"
            statements.append(f"uint32_t {result} = {read}(context, {word(node)}u);")
            return result

        fields(node, "op", "children")
        op = choice(node["op"], "sum", "max")
        children = items(node["children"])
        if not children:
            raise ValueError("composite branch must have a child")
        result = emit(children[0], depth + 1)
        for child in children[1:]:
            other = emit(child, depth + 1)
            if op == "sum":
                statements.append(f"{result} += {other};")
            else:
                statements.append(f"if ({other} > {result}) {result} = {other};")
            if len(statements) > 128:
                raise ValueError("composite tree exceeds the statement limit")
        return result

    result = emit(config["tree"], 0)
    unit.define(f"uint32_t {name}(void *context)", [*statements, f"return {result};"])


def decorator(unit, name, config):
    fields(config, "target", "layers")
    target = identifier(config["target"])
    layers = []
    for layer in items(config["layers"]):
        fields(layer, "before", "after")
        steps = []
        for step in (layer["before"], layer["after"]):
            fields(step, "op", "value")
            operator = {"add": "+=", "multiply": "*="}[choice(step["op"], "add", "multiply")]
            steps.append(f"value {operator} {word(step['value'])}u;")
        layers.append(steps)
    target = unit.hook(target, "uint32_t", "void *, uint32_t")
    statements = [before for before, _ in layers]
    statements.append(f"value = {target}(context, value);")
    statements.extend(after for _, after in reversed(layers))
    unit.define(
        f"uint32_t {name}(void *context, uint32_t value)",
        [*statements, "return value;"],
    )


def facade(unit, name, config):
    fields(config, "validate", "encode", "send")
    validate = identifier(config["validate"])
    encode = identifier(config["encode"])
    send = identifier(config["send"])
    if len({validate, encode, send}) != 3:
        raise ValueError("facade stages require distinct subsystem symbols")
    validate = unit.hook(validate, "int", "void *, uint32_t")
    encode = unit.hook(encode, "uint32_t", "void *, uint32_t")
    send = unit.hook(send, "int", "void *, uint32_t")
    unit.define(
        f"int {name}(void *context, uint32_t request)",
        [f"if (!{validate}(context, request)) return 0;",
         f"uint32_t encoded = {encode}(context, request);",
         f"return {send}(context, encoded);"],
    )


def flyweight(unit, name, config):
    fields(config, "descriptors")
    descriptors = items(config["descriptors"])
    if not descriptors:
        raise ValueError("flyweight requires a descriptor")
    aliases = {}
    descriptors_by_value = {}
    for descriptor in descriptors:
        fields(descriptor, "scale", "offset", "aliases")
        value = (word(descriptor["scale"]), word(descriptor["offset"]))
        index = descriptors_by_value.setdefault(value, len(descriptors_by_value))
        names = items(descriptor["aliases"])
        if not names:
            raise ValueError("flyweight descriptor requires an alias")
        for alias in names:
            alias = identifier(alias)
            if alias in aliases:
                raise ValueError(f"duplicate flyweight alias {alias!r}")
            aliases[alias] = index

    intrinsic = f"{name}_intrinsic"
    unit.declare(f"typedef struct {intrinsic} {intrinsic};")
    unit.source(f"struct {intrinsic} {{ uint32_t scale; uint32_t offset; }};")
    for (scale, offset), index in descriptors_by_value.items():
        unit.source(f"static const {intrinsic} {name}_descriptor_{index} = "
                    f"{{ {scale}u, {offset}u }};")
    for alias, index in aliases.items():
        unit.define(
            f"const {intrinsic} *{name}_get_{alias}(void)",
            f"return &{name}_descriptor_{index};",
        )
    unit.define(
        f"uint32_t {name}_apply(const {intrinsic} *descriptor, uint32_t position)",
        "return descriptor->scale * position + descriptor->offset;",
    )


def proxy(unit, name, config):
    fields(config, "roles", "target")
    target = identifier(config["target"])
    mask = 0
    for role in items(config["roles"]):
        role = word(role)
        if role >= 32:
            raise ValueError("proxy role must be below 32")
        mask |= 1 << role
    signature = f"int {name}(void *context, uint32_t role, uint32_t request, uint32_t *result)"
    if not mask:
        unit.define(signature, ["(void)context;", "(void)role;", "(void)request;", "(void)result;", "return 0;"])
        return
    target = unit.hook(target, "uint32_t", "void *, uint32_t")
    unit.define(
        signature,
        [f"if (role >= 32u || !(({mask}u >> role) & 1u) || !result) return 0;",
         f"*result = {target}(context, request);",
         "return 1;"],
    )


SHARED_NAMES = set()


GENERATORS = {
    "adapter": adapter,
    "bridge": bridge,
    "composite": composite,
    "decorator": decorator,
    "facade": facade,
    "flyweight": flyweight,
    "proxy": proxy,
}
