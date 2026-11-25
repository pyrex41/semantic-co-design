"""Finite integer observations of the nine Chapter 3 specialization groups.

These projections encode one whole interaction in the live argument vector. They
are bounded models, not implementations of the underlying concurrent/lazy APIs.
"""

L = lambda x: ["lit", x]
A = lambda x: ["arg", x]
C = lambda x: ["cfg", x]
V = lambda x: ["var", x]
G = lambda x: ["getv", x]
B = lambda op, x, y: [op, x, y]
I = lambda test, yes, no: ["ifv", test, yes, no]
F = lambda n, initial, body: ["foldv", n, initial, body]

# Each fold reads the live request at its current index; var 0 is its state.
request = G(V(1))
account = lambda fee: F(
    ["lenv"],
    C(0),
    I(
        B("ltv", request, L(0)),
        I(
            B("ltv", V(0), B("add", B("sub", L(0), request), fee)),
            V(0),
            B("sub", B("add", V(0), request), fee),
        ),
        B("add", V(0), request),
    ),
)


def transfer(reserve):
    """Pack source and target into one state; rejected writes leave both intact."""
    source = B("divv", V(0), L(10000))
    target = B("modv", V(0), L(10000))
    accepted = B("add", B("mul", B("sub", source, request), L(10000)), B("add", target, request))
    initial = B("add", B("mul", C(0), L(10000)), C(2))
    return F(["lenv"], initial, I(B("ltv", B("sub", source, reserve), request), V(0), accepted))


# Alternating key/value pairs. Only writes to the selected aliased cell count.
table = lambda key: F(
    B("divv", ["lenv"], L(2)),
    C(0),
    I(
        B("eqv", B("modv", G(B("mul", V(1), L(2))), L(2)), key),
        G(B("add", B("mul", V(1), L(2)), L(1))),
        V(0),
    ),
)


def memoized(step):
    """Cache four recursive recurrence keys as decimal value-plus-one slots."""
    # In this let, var 0 is 10**index, var 1 the cache and var 2 the index.
    power = F(V(1), L(1), B("mul", V(0), L(10)))
    state, index, place = V(1), V(2), V(0)
    digit = B("modv", B("divv", state, place), L(10))
    previous = B("modv", B("divv", state, B("divv", place, L(10))), L(10))
    value = I(B("eqv", index, L(0)), B("add", C(0), L(1)), B("add", previous, step))
    insert = B("add", state, B("add", L(10000), B("mul", value, place)))
    body = ["letv", power, I(B("eqv", digit, L(0)), insert, state)]
    # Recursive dependency n-1 is computed before n by the inner traversal.
    return F(["lenv"], L(0), F(B("add", G(V(1)), L(1)), V(0), body))


def circuit(zero_delay, one_delay):
    """Drain an agenda in timestamp order, preserving insertion order on ties."""
    signal = G(B("add", B("mul", V(1), L(2)), L(1)))
    time = G(B("mul", V(1), L(2)))
    due = B("add", time, I(signal, one_delay, zero_delay))
    token = B("add", B("mul", V(3), L(2)), signal)
    delivery = I(B("eqv", due, V(3)), B("add", B("mul", V(0), L(100)), token), V(0))
    return F(L(10), L(0), F(B("divv", ["lenv"], L(2)), V(0), delivery))


def constraints(total_slot):
    """One sum connector, external ownership, retraction and rollback."""
    state = V(0)
    xd = B("modv", state, L(10))
    yd = B("modv", B("divv", state, L(10)), L(10))
    op = G(B("mul", V(1), L(2)))
    value = G(B("add", B("mul", V(1), L(2)), L(1)))
    clear_x = B("sub", state, xd)
    clear_y = B("sub", state, B("mul", yd, L(10)))
    # A known value may be read or confirmed by another source. Agreement
    # never transfers ownership. Contradiction leaves the previous state live.
    def assert_slot(owned, other, scale):
        known = I(owned, owned, I(other, B("sub", B("add", C(total_slot), L(2)), other), L(0)))
        proposed = B("add", value, L(1))
        return I(known,
                 I(B("eqv", proposed, known), state, B("add", state, L(100))),
                 B("add", state, B("mul", proposed, L(scale))))

    update = I(B("eqv", op, L(0)), clear_x,
               I(B("eqv", op, L(1)), assert_slot(xd, yd, 1),
                 I(B("eqv", op, L(2)), clear_y, assert_slot(yd, xd, 10))))
    result = F(B("divv", ["lenv"], L(2)), L(0), update)
    # Extract external slots, infer missing values, and retain assertion mask.
    x = B("modv", V(0), L(10))
    y = B("modv", B("divv", V(0), L(10)), L(10))
    ix = I(x, x, I(y, B("sub", B("add", C(total_slot), L(2)), y), L(0)))
    iy = I(y, y, I(x, B("sub", B("add", C(total_slot), L(2)), x), L(0)))
    mask = B("add", I(x, L(1), L(0)), I(y, L(2), L(0)))
    return [
        "letv",
        result,
        B(
            "add",
            B("add", B("mul", ix, L(1000)), B("mul", iy, L(100))),
            B("add", B("mul", mask, L(10)), B("divv", V(0), L(100))),
        ),
    ]


# Demand a prefix of a live source, retaining only values >= threshold.
def pipeline(offset):
    """Pack sum, output count and source forces; stop forcing on demand."""
    state = V(0)
    source = G(B("add", V(1), L(1)))
    total = B("divv", state, L(10000))
    produced = B("modv", B("divv", state, L(100)), L(100))
    forced = B("modv", state, L(100))
    accepted = B(
        "add",
        B("mul", B("add", total, B("add", source, offset)), L(10000)),
        B("add", B("mul", B("add", produced, L(1)), L(100)), B("add", forced, L(1))),
    )
    rejected = B("add", state, L(1))
    updated = I(B("ltv", source, C(0)), rejected, accepted)
    folded = F(B("sub", ["lenv"], L(1)), L(0), I(B("ltv", produced, A(0)), updated, state))
    return [
        "letv",
        folded,
        B("add", B("mul", B("divv", V(0), L(10000)), L(100)), B("modv", V(0), L(100))),
    ]


def pairs(stride):
    """Follow the interleaved row/recursive-tail pair stream by index."""
    # State holds remaining index in hundreds and diagonal depth in units.
    remainder = B("divv", V(0), L(100))
    depth = B("modv", V(0), L(100))
    descend = B(
        "add", B("mul", B("sub", B("divv", remainder, L(2)), L(1)), L(100)), B("add", depth, L(1))
    )
    even_positive = B("andv", B("ltv", L(0), remainder), B("eqv", B("modv", remainder, L(2)), L(0)))
    state = F(L(8), B("mul", A(0), L(100)), I(even_positive, descend, V(0)))
    depth = B("modv", V(0), L(100))
    remainder = B("divv", V(0), L(100))
    right = B("add", depth, B("mul", stride, B("divv", B("add", remainder, L(1)), L(2))))
    return ["letv", state, B("add", B("mul", depth, L(100)), right)]


def feedback(coefficient):
    """Multiple consumers share one cached prefix and its force counter."""
    requested = G(V(1))
    current_index = B("modv", B("divv", V(0), L(100)), L(100))
    count = B("maxv", L(0), B("sub", requested, current_index))
    value = B("divv", V(0), L(10000))
    index = B("modv", B("divv", V(0), L(100)), L(100))
    forces = B("modv", V(0), L(100))
    advance = B(
        "add",
        B("mul", B("mul", value, B("add", L(1), coefficient)), L(10000)),
        B("add", B("mul", B("add", index, L(1)), L(100)), B("add", forces, L(1))),
    )
    return F(["lenv"], B("mul", C(0), L(10000)), F(count, V(0), advance))


CASES = [
    dict(
        id="sicp-account",
        title="Local account state",
        contract="Initial balance config[0]; arguments are deposits >= 0 or withdrawals < 0. Withdrawals including the fee fail unchanged if funds are insufficient. At most 32 requests and values 0..1000.",
        program=account(C(1)),
        changed=account(L(0)),
        config=[10, 2],
        samples=[[[5, -4, -12], 9, 11], [[-9, 3, -4], 7, 0], [[], 10, 10]],
    ),
    dict(
        id="sicp-transfer",
        title="Serialized transfer",
        contract="Source and target start at config[0] and config[2]. Each argument is a nonnegative transfer, rejected atomically if it would cross reserve config[1]. Return source*10000+target; target remains below 10000. At most 32 requests of 0..1000.",
        program=transfer(C(1)),
        changed=transfer(L(0)),
        config=[20, 5, 3],
        samples=[[[7, 9, 3], 100013, 10022], [[16, 4], 160007, 23], [[], 200003, 200003]],
    ),
    dict(
        id="sicp-table",
        title="Aliased mutable table",
        contract="Arguments are at most 16 key,value write pairs with keys 0..3 and values 0..1000. Keys 0/2 alias one cell and keys 1/3 alias the other; return the selected cell after ordered writes, initial value config[0].",
        program=table(L(0)),
        changed=table(L(1)),
        config=[7],
        samples=[[[0, 3, 1, 8, 2, 4], 4, 8], [[1, 6], 7, 6], [[], 7, 7]],
    ),
    dict(
        id="sicp-memoized",
        title="Recursive keyed memoization",
        contract="Each live argument is a key 0..3. Recursive f(0)=config[0], f(n)=f(n-1)+step fills four decimal cache slots, each value stored plus one. Return 10000*misses plus packed slots; hits do not add misses. At most 16 requests; slot values must be 0..8.",
        program=memoized(C(1)),
        changed=memoized(C(2)),
        config=[1, 1, 2],
        samples=[[[2, 1, 3], 45432, 48642], [[1, 1], 20032, 20042], [[], 0, 0]],
    ),
    dict(
        id="sicp-circuit",
        title="Transport delay agenda",
        contract="Up to three (time,bit) input events in insertion order, time 0..5. Every event schedules a captured signal at time plus bit-specific config delay; drain through time 9 in timestamp order and FIFO order at equal times. Trace is base-100 tokens 2*delivery_time+bit. Changed swaps bit delays.",
        program=circuit(C(0), C(1)),
        changed=circuit(C(1), C(0)),
        config=[1, 2],
        samples=[[[0, 1, 1, 0], 504, 306], [[0, 0, 0, 1], 205, 304], [[], 0, 0]],
    ),
    dict(
        id="sicp-constraints",
        title="Connector propagation and retraction",
        contract="At most 8 (operation,value) pairs: 0 retract x, 1 assert x, 2 retract y, 3 assert y. Values 0..config[0], total config[0] <= 8. Equation x+y=total infers either missing value. Confirming an inferred value does not acquire ownership. A contradictory assertion is rolled back and increments conflicts. Return 1000*(x+1)+100*(y+1)+10*external-mask+conflicts, with zero digit for unknown; x/y own mask bits 1/2. Changed total is config[1].",
        program=constraints(0),
        changed=constraints(1),
        config=[6, 7],
        samples=[
            [[1, 2], 3510, 3610],
            [[1, 2, 3, 4], 3510, 3611],
            [[1, 2, 3, 5], 3511, 3610],
            [[1, 2, 3, 4, 0, 0], 0, 1],
            [[1, 2, 1, 3], 3511, 3611],
            [[3, 2, 0, 0], 5320, 6320],
        ],
    ),
    dict(
        id="sicp-pipeline",
        title="Demand-driven stream pipeline",
        contract="Argument 0 is requested output count 0..16; remaining arguments are at most 16 source values 0..1000. Force source until enough values >= config[0] are mapped or the source ends; return mapped sum*100 plus number of source cells forced.",
        program=pipeline(L(2)),
        changed=pipeline(L(1)),
        config=[5],
        samples=[[[2, 2, 5, 8, 99], 1703, 1503], [[1, 4, 10], 1202, 1102], [[0, 8], 0, 0]],
    ),
    dict(
        id="sicp-pairs",
        title="Fair stream pairs",
        contract="Argument n in 0..20 requests a zero-based pair in fair interleaved row order (0,0),(0,1),(1,1),(0,2),...; return i*100+j. The changed configured row stride 2 skips odd row offsets. Both coordinates stay below 100.",
        program=pairs(C(0)),
        changed=pairs(C(1)),
        config=[1, 2],
        samples=[[[0], 0, 0], [[2], 101, 101], [[4], 102, 103]],
    ),
    dict(
        id="sicp-feedback",
        title="Shared delayed feedback",
        contract="Each argument is a requested stream index 0..8. Starting from config[0], feedback multiplies each next value by 2; all requests share one memoized prefix. Return last forced value*10000 plus maximum forced index*100 plus number of forces. Changed multiplier is 3.",
        program=feedback(L(1)),
        changed=feedback(L(2)),
        config=[2],
        samples=[[[2, 1, 3], 160303, 540303], [[0, 0], 20000, 20000], [[1, 1], 40101, 60101]],
    ),
]
