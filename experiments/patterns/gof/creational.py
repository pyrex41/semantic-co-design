"""C11 specializations for the five GoF creational patterns."""

from .common import choice, fields, items, word


SHARED_NAMES = set()


def abstract_factory(unit, name, config):
    fields(config, "family", "key")
    family = choice(config["family"], "offset", "mask")
    key = word(config["key"])
    family_code = 1 if family == "offset" else 2
    for role in ("reader", "writer"):
        product = f"{name}_{role}"
        unit.declare(f"typedef struct {{ uint32_t family; uint32_t key; }} {product};")
        unit.define(
            f"{product} {name}_make_{role}(void)",
            [f"return ({product}){{{family_code}u, {key}u}};"],
        )

    read = "value - self.key" if family == "offset" else "value ^ self.key"
    write = "value + self.key" if family == "offset" else "value ^ self.key"
    unit.define(f"uint32_t {name}_read({name}_reader self, uint32_t value)", [f"return {read};"])
    unit.define(f"uint32_t {name}_write({name}_writer self, uint32_t value)", [f"return {write};"])


def builder(unit, name, config):
    fields(config, "representation", "capacity")
    representation = choice(config["representation"], "sequence", "summary")
    capacity = word(config["capacity"])
    if not 1 <= capacity <= 32:
        raise ValueError("builder capacity must be between 1 and 32")

    product = f"{name}_product"
    if representation == "sequence":
        unit.declare(f"typedef struct {{ uint32_t count; uint32_t values[{capacity}]; }} {product};")
        add = "product->values[product->count] = value;"
        finish = "return *product;"
    else:
        unit.declare(f"typedef struct {{ uint32_t count; uint32_t max; uint32_t mean; uint64_t sum; }} {product};")
        add = """product->sum += value;
    if (product->count == 0u || value > product->max) {
        product->max = value;
    }"""
        finish = """if (product->count != 0u) {
        product->mean = (uint32_t)(product->sum / product->count);
    }
    return *product;"""

    unit.source(f"""static {product} {name}_begin(void) {{
    return ({product}){{0}};
}}

static void {name}_add({product} *product, uint32_t value) {{
    {add}
    ++product->count;
}}

static {product} {name}_finish({product} *product) {{
    {finish}
}}""")
    unit.define(
        f"{product} {name}_build(const uint32_t *values, uint32_t count)",
        [
            f"{product} product = {name}_begin();",
            f"for (uint32_t i = 0; i < count && i < {capacity}u; ++i) {{",
            f"    {name}_add(&product, values[i]);",
            "}",
            f"return {name}_finish(&product);",
        ],
    )


def factory_method(unit, name, config):
    fields(config, "product", "prepare", "parameter", "finish")
    concrete = choice(config["product"], "offset", "scaled")
    prepare = word(config["prepare"])
    parameter = word(config["parameter"])
    finish = word(config["finish"])
    product = f"{name}_product"
    make = f"{name}_make_{concrete}"
    operator = "+" if concrete == "offset" else "*"
    unit.declare(f"typedef struct {{ uint32_t value; }} {product};")
    unit.source(f"""static {product} {make}(uint32_t prepared) {{
    return ({product}){{(uint32_t)(prepared {operator} {parameter}u)}};
}}""")
    unit.define(
        f"{product} {name}_create(uint32_t value)",
        [
            f"{product} product = {make}(value + {prepare}u);",
            f"product.value += {finish}u;",
            "return product;",
        ],
    )


def prototype(unit, name, config):
    fields(config, "kind", "values")
    kind = word(config["kind"])
    values = [word(value) for value in items(config["values"])]
    if not values:
        raise ValueError("prototype requires at least one value")

    product = f"{name}_product"
    literals = ", ".join(f"{value}u" for value in values)
    unit.declare(f"typedef struct {{ uint32_t kind; uint32_t values[{len(values)}]; }} {product};")
    unit.source(f"static const {product} {name}_exemplar = {{{kind}u, {{{literals}}}}};")
    unit.define(f"{product} {name}_clone(void)", [f"return {name}_exemplar;"])


def singleton(unit, name, config):
    fields(config, "revision", "limit")
    revision = word(config["revision"])
    limit = word(config["limit"])
    config_type = f"{name}_configuration"
    unit.declare(f"typedef struct {config_type} {config_type};")
    unit.source(f"""struct {config_type} {{ uint32_t revision; uint32_t limit; }};
static const {config_type} {name}_instance = {{{revision}u, {limit}u}};""")
    unit.define(f"const {config_type} *{name}_get(void)", [f"return &{name}_instance;"])
    for member in ("revision", "limit"):
        unit.define(
            f"uint32_t {name}_{member}(const {config_type} *configuration)",
            [f"return configuration->{member};"],
        )


GENERATORS = {
    "abstract_factory": abstract_factory,
    "builder": builder,
    "factory_method": factory_method,
    "prototype": prototype,
    "singleton": singleton,
}
