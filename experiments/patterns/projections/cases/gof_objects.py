"""Bounded, executable encodings of the GoF creational and structural examples.

Results use decimal fields only within the domains stated by each contract.
They are observations, not implementations of general-purpose object systems.
"""


def L(x):
    return ["lit", x]


def A(x):
    return ["arg", x]


def C(x):
    return ["cfg", x]


def B(op, x, y):
    return [op, x, y]


def add(x, y):
    return B("add", x, y)


def sub(x, y):
    return B("sub", x, y)


def mul(x, y):
    return B("mul", x, y)


def eq(x, y):
    return B("eqv", x, y)


def lt(x, y):
    return B("ltv", x, y)


def iff(t, a, b):
    return ["ifv", t, a, b]


def pack(x, y):
    return add(mul(x, L(100)), y)


# The family selector chooses matching reversible product operations.
def abstract_factory(bad=False):
    family = A(0)
    value = A(1)
    writer = iff(eq(family, L(0)), add(value, C(0)), mul(value, C(1)))
    reader_family = sub(L(1), family) if bad else family
    return iff(eq(reader_family, L(0)), sub(writer, C(0)), B("divv", writer, C(1)))


# The director consumes every value after the representation selector.
def builder(changed=False):
    count = sub(["lenv"], L(1))
    count = sub(count, L(1)) if changed else count
    value = ["getv", add(["var", 1], L(1))]
    update = iff(eq(A(0), L(0)), add(["var", 0], value), B("maxv", ["var", 0], value))
    result = ["foldv", count, C(0), update]
    return pack(count, result)


# The creator's shared preparation and finish surround its product operation.
def factory_method(changed=False):
    prepared = add(A(0), C(0))
    product = add(prepared, C(1)) if not changed else mul(prepared, C(1))
    return add(product, L(1))


# Two-cell exemplar: clone the cells, write one, read either original or clone.
def prototype(changed=False):
    original = iff(eq(A(1), L(0)), C(0), C(1))
    clone_read = iff(eq(A(1), A(0)), add(original, A(2)), original)
    original_read = clone_read if changed else original
    return pack(original_read, clone_read)


# State packs first handle, mismatch count and accumulated reads. Each live
# getter either reuses the first allocation or allocates a fresh handle.
def singleton(changed=False):
    state = ["var", 0]
    index = ["var", 1]
    first = B("divv", state, L(10000))
    mismatches = B("modv", B("divv", state, L(100)), L(100))
    reads = B("modv", state, L(100))
    handle = add(index, L(1)) if changed else iff(eq(first, L(0)), add(index, L(1)), first)
    first_next = iff(eq(first, L(0)), handle, first)
    mismatch_next = add(mismatches, iff(eq(first, L(0)), L(0), iff(eq(first, handle), L(0), L(1))))
    read_next = add(reads, mul(["getv", index], C(0)))
    body = add(add(mul(first_next, L(10000)), mul(mismatch_next, L(100))), read_next)
    return ["foldv", ["lenv"], L(0), body]


# Packed word consists of tens and units, passed in order to the target.
def adapter(changed=False):
    hi = B("divv", A(0), L(10))
    lo = B("modv", A(0), L(10))
    return add(mul(lo if changed else hi, C(0)), hi if changed else lo)


# Both abstract operations consume readings from the same chosen backend;
# output carries adjusted first reading and readiness of the second.
def bridge(changed=False):
    first = add(A(0), C(0))
    second = add(A(1), C(0))
    adjusted = sub(first, C(1)) if changed else add(first, C(1))
    return pack(adjusted, lt(C(2), second))


# Ordered tree sum(a,max(b,a)); a appears twice as a leaf occurrence.
def composite(changed=False):
    branch = B("minv" if changed else "maxv", A(1), A(0))
    return add(add(A(0), branch), C(0))


# Outer add, inner multiply, one component add, then reverse postprocessing.
def decorator(changed=False):
    pre = mul(add(A(0), C(0)), C(1))
    component = add(pre, C(2))
    return mul(add(component, C(0)), C(1)) if changed else add(mul(component, C(1)), C(0))


# Carry subsystem effects as a state: count*100 + current value.
def facade(changed=False):
    valid = lt(A(0), C(0))
    initial = A(0)
    encoded = add(initial, C(1))
    after_encode = add(L(100), encoded)
    after_send = add(after_encode, L(100))
    return iff(valid, after_encode if changed else after_send, L(0))


# Three sequential intern requests; identities derive from descriptor equality,
# while positions only participate in application, never in the intern key.
def flyweight(changed=False):
    d1, p1, d2, p2, d3, p3 = (A(i) for i in range(6))
    id2 = iff(eq(d2, d1), L(2) if changed else L(1), L(2))
    id3 = iff(
        eq(d3, d1), L(3) if changed else L(1), iff(eq(d3, d2), id2, iff(eq(id2, L(1)), L(2), L(3)))
    )
    identities = add(add(L(100), mul(id2, L(10))), id3)
    applied = add(add(add(mul(d1, p1), mul(d2, p2)), mul(d3, p3)), C(0))
    return pack(identities, applied)


# A target invocation updates the call count and transforms the live value.
def proxy(changed=False):
    allowed = eq(A(0), C(0))
    target = add(L(100), add(A(1), C(1)))
    return iff(allowed, target, target if changed else L(0))


CASES = [
    dict(
        id="gof-abstract-factory",
        title="Abstract Factory",
        contract="Args: family selector 0 or 1 and value 1..20. The selected family supplies a writer and matching inverse reader; changed selects the other reader. Keys and scale are cfg[0:2].",
        program=abstract_factory(),
        changed=abstract_factory(True),
        config=[3, 2],
        samples=[[[0, 5], 5, 4], [[1, 7], 7, 11]],
    ),
    dict(
        id="gof-builder",
        title="Builder",
        contract="Args: representation selector 0 for sum, 1 for maximum, followed by one to five values 0..9. The director visits the stream and packs count and result; cfg[0] seeds the builder. Changed omits the last element.",
        program=builder(),
        changed=builder(True),
        config=[0],
        samples=[[[0, 2, 3, 4], 309, 205], [[1, 2, 7, 4], 307, 207], [[1, 8], 108, 0]],
    ),
    dict(
        id="gof-factory-method",
        title="Factory Method",
        contract="Args: integer value. Creator prepares by adding cfg[0], invokes the product operation using cfg[1], then finalizes by adding one. Changed factory selects multiplication.",
        program=factory_method(),
        changed=factory_method(True),
        config=[2, 3],
        samples=[[[4], 10, 19], [[1], 7, 10]],
    ),
    dict(
        id="gof-prototype",
        title="Prototype",
        contract="Args: write index 0 or 1, read index 0 or 1, nonnegative delta. Clone two configured cells, mutate the selected clone cell, then pack the selected original and clone reads. Values stay below 100. Changed mutates the exemplar too.",
        program=prototype(),
        changed=prototype(True),
        config=[12, 20],
        samples=[[[0, 0, 3], 1215, 1515], [[0, 1, 5], 2020, 2020], [[1, 1, 4], 2024, 2424]],
    ),
    dict(
        id="gof-singleton",
        title="Singleton",
        contract="Args: one to five read multipliers 0..3. Each getter reuses the first allocated handle and reads cfg[0] times the multiplier. Packed state is first handle * 10000 + unequal-handle count * 100 + total reads; totals stay below 100. Changed allocates on every getter.",
        program=singleton(),
        changed=singleton(True),
        config=[4],
        samples=[[[1], 10004, 10004], [[1, 2, 1], 10016, 10216], [[0, 3], 10012, 10112]],
    ),
    dict(
        id="gof-adapter",
        title="Adapter",
        contract="Args: a two-digit nonnegative packed word. Adapter extracts tens and units and invokes target cfg[0]*first + second. Changed swaps the arguments.",
        program=adapter(),
        changed=adapter(True),
        config=[4],
        samples=[[[23], 11, 14], [[71], 29, 11]],
    ),
    dict(
        id="gof-bridge",
        title="Bridge",
        contract="Args: two readings 0..20 from the same backend; cfg[0] is backend offset, cfg[1] abstraction adjustment, cfg[2] readiness threshold. Result packs adjusted first reading and second-reading readiness. Changed adjustment subtracts.",
        program=bridge(),
        changed=bridge(True),
        config=[2, 3, 10],
        samples=[[[4, 9], 901, 301], [[8, 4], 1300, 700]],
    ),
    dict(
        id="gof-composite",
        title="Composite",
        contract="Args: two leaf values. Tree computes first + max(second, first) + cfg[0], retaining the repeated first leaf occurrence. Changed branch uses min.",
        program=composite(),
        changed=composite(True),
        config=[1],
        samples=[[[3, 7], 11, 7], [[8, 2], 17, 11]],
    ),
    dict(
        id="gof-decorator",
        title="Decorator",
        contract="Args: component input. Outer preprocessing adds cfg[0], inner multiplies cfg[1], component adds cfg[2], and reverse postprocessing multiplies cfg[1], then adds cfg[0]. Changed reverses the postprocessing order.",
        program=decorator(),
        changed=decorator(True),
        config=[2, 3, 4],
        samples=[[[5], 77, 81], [[1], 41, 45]],
    ),
    dict(
        id="gof-facade",
        title="Facade",
        contract="Args: value 0..20. Validate value < cfg[0]; on success encode by adding cfg[1], then send. Each reached effect increments the packed count; failure returns zero. Changed omits send.",
        program=facade(),
        changed=facade(True),
        config=[10, 5],
        samples=[[[4], 209, 109], [[12], 0, 0], [[0], 205, 105]],
    ),
    dict(
        id="gof-flyweight",
        title="Flyweight",
        contract="Args: three descriptor and position pairs, each 0..4. Requests intern equal descriptors, assigning sequential handles; result packs the three handle digits and the sum of descriptor*position plus cfg[0]. Changed fails to reuse the first descriptor.",
        program=flyweight(),
        changed=flyweight(True),
        config=[2],
        samples=[
            [[3, 1, 3, 2, 4, 1], 11215, 12315],
            [[2, 1, 3, 1, 2, 2], 12111, 12311],
            [[1, 1, 2, 1, 3, 1], 12308, 12308],
        ],
    ),
    dict(
        id="gof-proxy",
        title="Proxy",
        contract="Args: role and value 0..20. Only cfg[0] role invokes target, which increments call count and adds cfg[1]; packed result is count*100 plus value. Denial has no effect and returns zero. Changed invokes target on denial.",
        program=proxy(),
        changed=proxy(True),
        config=[2, 4],
        samples=[[[2, 6], 110, 110], [[1, 6], 0, 110], [[2, 0], 104, 104]],
    ),
]
