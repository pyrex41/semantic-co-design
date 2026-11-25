"""Finite integer projections of PAIP grammar, logic, search and constraints.

These are deliberately bounded observations, not the full catalog compilers.
"""


def L(n):
    return ["lit", n]


def A(n):
    return ["arg", n]


def C(n):
    return ["cfg", n]


def V(n):
    return ["var", n]


def B(op, x, y):
    return [op, x, y]


def add(x, y):
    return B("add", x, y)


def mul(x, y):
    return B("mul", x, y)


def eq(x, y):
    return B("eqv", x, y)


def lt(x, y):
    return B("ltv", x, y)


def AND(x, y):
    return B("andv", x, y)


def OR(x, y):
    return B("orv", x, y)


def IF(t, x, y):
    return ["ifv", t, x, y]


def fold(n, initial, body):
    return ["foldv", L(n), initial, body]


def digit(n, place):
    return B("modv", B("divv", n, L(place)), L(10))


# S -> terminal | S S.  Each iteration expands every derivation one level;
# the changed grammar replaces S S with two distinct unary productions.
def generation(ch=False):
    previous = V(0)
    return ["foldv", A(0), C(0), add(C(0), mul(L(2), previous) if ch else mul(previous, previous))]


# A bounded ordered Horn relation path(X,Z) :- edge(X,Z); edge(X,Y),edge(Y,Z).
# Input edge bits encode the 3x3 adjacency matrix in row-major order.
def edge(mask, x, y):
    index = add(mul(x, L(3)), y)
    divisor = L(256)
    for k in range(7, -1, -1):
        divisor = IF(eq(index, L(k)), L(2**k), divisor)
    return B("modv", B("divv", mask, divisor), L(2))


def horn(ch=False):
    join = AND(edge(A(0), A(1), V(1)), edge(A(0), V(1), A(2)))
    if ch:
        join = AND(B("sub", L(1), eq(V(1), L(1))), join)
    return ["foldv", C(0), edge(A(0), A(1), A(2)), OR(V(0), join)]


# Follow parent links 2 -> 1 -> 0, accumulating inherited facts. The
# packed state stores current entity + 3*found. Changed skips entity 1.
def worlds(ch=False):
    state = V(0)
    current = B("modv", state, L(3))
    found = B("divv", state, L(3))
    divisor = power3(current)
    fact = B("modv", B("divv", A(0), divisor), L(2))
    parent = IF(
        eq(current, L(0)),
        L(0),
        IF(AND(L(1 if ch else 0), eq(current, L(2))), L(0), B("sub", current, L(1))),
    )
    result = ["foldv", C(0), A(1), add(parent, mul(L(3), OR(found, fact)))]
    return B("divv", result, L(3))


# Right-recursive S -> noun | noun conjunction S. Each noun has two
# semantic senses with configured scores. Accumulate all parse counts and
# the minimum total score, packed as 100*score+count; changed prefers maximum.
def preference(ch=False):
    i = V(1)
    acc = V(0)
    token = ["getv", i]
    noun = eq(B("modv", i, L(2)), L(0))
    count = B("modv", acc, L(100))
    score = B("divv", acc, L(100))
    choice = B("maxv" if ch else "minv", C(2), C(3))
    advance = add(mul(L(100), add(score, choice)), mul(L(2), count))
    step = IF(
        eq(count, L(0)),
        L(0),
        IF(noun, IF(eq(token, C(0)), advance, L(0)), IF(eq(token, C(1)), acc, L(0))),
    )
    return IF(
        AND(lt(L(0), ["lenv"]), eq(B("modv", ["lenv"], L(2)), L(1))),
        ["foldv", ["lenv"], L(1), step],
        L(0),
    )


# Fixed DCG for determiner noun verb object-pronoun. Number features are
# encoded in the noun and verb token IDs and must unify across constituents.
def dcg(ch=False):
    noun = B("sub", A(1), L(10))
    verb = B("sub", A(2), L(20))
    agreement = eq(noun, B("sub", L(1), verb) if ch else verb)
    return AND(
        AND(
            AND(eq(A(0), C(0)), AND(lt(L(-1), noun), lt(noun, L(2)))),
            AND(lt(L(-1), verb), lt(verb, L(2))),
        ),
        AND(agreement, eq(A(3), C(1))),
    )


# Each breadth-first round expands every edge from the current frontier.
# State is visited + 8*frontier; all sets are three-bit masks.
def bit(mask, n):
    return B("modv", B("divv", mask, L(2**n)), L(2))


def power3(n):
    return IF(eq(n, L(0)), L(1), IF(eq(n, L(1)), L(2), L(4)))


def graph(ch=False):
    state = V(0)
    visited = B("modv", state, L(8))
    frontier = B("divv", state, L(8))
    new = []
    for dst in range(3):
        reaches = L(0)
        for src in range(3):
            e = L(0) if ch and (src, dst) == (1, 2) else bit(A(0), src * 3 + dst)
            reaches = OR(reaches, AND(bit(frontier, src), e))
        new.append(mul(L(2**dst), AND(B("sub", L(1), bit(visited, dst)), reaches)))
    nextfront = add(add(new[0], new[1]), new[2])
    update = ["letv", nextfront, add(add(B("modv", V(1), L(8)), V(0)), mul(L(8), V(0)))]
    initial = mul(L(9), power3(A(1)))
    traversal = ["foldv", C(0), initial, update]
    return B("modv", B("divv", B("modv", traversal, L(8)), power3(A(2))), L(2))


# BFS over four propositional states. Pickup adds key; unlock requires key
# and adds open. Each round expands both operators on every reached state.
def planning(ch=False):
    visited = B("modv", V(0), L(16))
    distance = B("divv", V(0), L(16))
    goal = OR(bit(visited, 2), bit(visited, 3))
    newdistance = IF(AND(eq(distance, L(9)), goal), V(1), distance)
    pieces = []
    for dst in range(4):
        reaches = bit(visited, dst)
        for src in range(4):
            pickup = (src & 1) == 0 and dst == (src | 1) and not ch
            unlock = (src & 1) != 0 and dst == (src | 2)
            if pickup or unlock:
                reaches = OR(reaches, bit(visited, src))
        pieces.append(mul(L(2**dst), reaches))
    expanded = add(add(pieces[0], pieces[1]), add(pieces[2], pieces[3]))
    init = add(
        L(144), IF(eq(A(0), L(0)), L(1), IF(eq(A(0), L(1)), L(2), IF(eq(A(0), L(2)), L(4), L(8))))
    )
    return B("divv", ["foldv", C(0), init, add(mul(L(16), newdistance), expanded)], L(16))


# Count all 3-color assignments satisfying observed first color and triangle edges.
def coloring(ch=False):
    # Propagate the observed x color by fixing x, then enumerate the
    # remaining nine (y,z) assignments and enforce every binary edge.
    i = V(1)
    x = A(0)
    y = B("modv", i, L(3))
    z = B("divv", i, L(3))
    neq = lambda a, b: B("sub", L(1), eq(a, b))
    valid = AND(neq(x, y), AND(neq(y, z), L(1) if ch else neq(x, z)))
    return ["foldv", C(0), L(0), add(V(0), valid)]


# Two three-edge junctions share one physical edge. The legal junction
# catalog consists of binary triples with even parity. Enumerate all five
# distinct line labels and filter both junction catalogs and observations.
def waltz(ch=False):
    i = V(1)
    shared = B("modv", i, L(2))
    a = B("modv", B("divv", i, L(2)), L(2))
    b = B("modv", B("divv", i, L(4)), L(2))
    c = B("modv", B("divv", i, L(8)), L(2))
    d = B("divv", i, L(16))
    first = eq(B("modv", add(add(shared, a), b), L(2)), L(0))
    second = eq(B("modv", add(add(shared, c), d), L(2)), L(1 if ch else 0))
    obs = lambda arg, v: OR(eq(arg, L(-1)), eq(arg, v))
    valid = AND(
        AND(first, second), AND(obs(A(0), a), AND(obs(A(1), b), AND(obs(A(2), c), obs(A(3), d))))
    )
    return ["foldv", C(0), L(0), add(V(0), valid)]


# Two-ply game: root maximizes the minimum of each opponent reply pair.
def game(ch=False):
    left = B("minv", A(0), A(1))
    right = B("minv", A(2), A(3))
    return add(C(0), B("minv" if ch else "maxv", left, right))


CASES = [
    dict(
        id="paip-recursive-phrase-generation",
        title="Recursive phrase generation",
        contract="Depth 0..3. Count derivation trees of S -> terminal | S S by iterated bounded expansion, with cfg[0] terminal alternatives. Changed replaces the binary rule with two unary rules.",
        program=generation(),
        changed=generation(True),
        config=[1],
        samples=[[[0], 1, 1], [[2], 5, 7], [[3], 26, 15]],
    ),
    dict(
        id="paip-horn-clause-compilation",
        title="Horn clause compilation",
        contract="Args: 9-bit adjacency matrix, source and target in 0..2. Return reachability by one or two directed edges; changed excludes entity 1 as an intermediate binding.",
        program=horn(),
        changed=horn(True),
        config=[3],
        samples=[[[34, 0, 2], 1, 0], [[4, 0, 2], 1, 1], [[0, 0, 1], 0, 0]],
    ),
    dict(
        id="paip-indexed-reasoning-in-worlds",
        title="Indexed reasoning in worlds",
        contract="Args: three-bit fact mask and entity 0..2. The fixed inheritance chain is 2 -> 1 -> 0. Return whether entity has a direct or inherited fact; changed omits inheritance from entity 1 to entity 2.",
        program=worlds(),
        changed=worlds(True),
        config=[3],
        samples=[[[2, 2], 1, 0], [[1, 2], 1, 1], [[4, 1], 0, 0]],
    ),
    dict(
        id="paip-ambiguous-parsing-with-preferences",
        title="Ambiguous parsing with preferences",
        contract="Up to five token IDs; grammar S -> noun | noun conjunction S, with token IDs and two sense costs in cfg. Return 100*best total cost + number of complete parses. Changed ranks higher cost first.",
        program=preference(),
        changed=preference(True),
        config=[7, 9, 2, 5],
        samples=[
            [[7, 9, 7], 404, 1004],
            [[7, 9, 7, 9, 7], 608, 1508],
            [[7, 7, 7], 0, 0],
            [[], 0, 0],
        ],
    ),
    dict(
        id="paip-deductive-grammars-and-english-feature-profiles",
        title="Deductive grammar and English features",
        contract="Four token IDs: determiner, noun 10/11, verb 20/21, object pronoun. The fixed DCG accepts cfg[0] and cfg[1] as determiner and object and unifies noun and verb number. Changed requires opposite numbers.",
        program=dcg(),
        changed=dcg(True),
        config=[8, 30],
        samples=[
            [[8, 10, 20, 30], 1, 0],
            [[8, 11, 20, 30], 0, 1],
            [[7, 10, 20, 30], 0, 0],
            [[8, 10, 20, 31], 0, 0],
        ],
    ),
    dict(
        id="paip-ordered-graph-and-heuristic-search",
        title="Ordered graph search",
        contract="Args: 9-bit directed adjacency matrix, source and target 0..2. Breadth-first traversal expands a frontier for three rounds and returns reachability. Changed removes edge 1 -> 2.",
        program=graph(),
        changed=graph(True),
        config=[3],
        samples=[[[34, 0, 2], 1, 0], [[4, 0, 2], 1, 1], [[0, 0, 2], 0, 0]],
    ),
    dict(
        id="paip-means-ends-and-forward-state-space-planning",
        title="Means-ends and forward planning",
        contract="Arg: 2-bit initial state (key, open door). Expand pickup and unlock operators across reachable states breadth-first for up to two actions; return shortest plan length, or 9 if unreachable. Changed removes pickup.",
        program=planning(),
        changed=planning(True),
        config=[3],
        samples=[[[0], 2, 9], [[1], 1, 1], [[2], 0, 0]],
    ),
    dict(
        id="paip-constraint-propagation-and-complete-bounded-search",
        title="Observed-color propagation and bounded search",
        contract="Arg: observed first vertex color 0..2. Propagate the observed first color, enumerate the remaining nine pairs, and count assignments satisfying all three inequality constraints. Changed drops the last edge constraint.",
        program=coloring(),
        changed=coloring(True),
        config=[9],
        samples=[[[0], 2, 4], [[2], 2, 4]],
    ),
    dict(
        id="paip-waltz-line-labeling",
        title="Waltz line labeling",
        contract="Four outer-edge observations, each -1 unknown or 0..1. Enumerate the shared edge and four outer labels; two junctions each require a triple of even parity and share one line. Return number of compatible assignments. Changed uses odd parity for the second junction.",
        program=waltz(),
        changed=waltz(True),
        config=[32],
        samples=[[[-1, -1, -1, -1], 8, 8], [[0, 0, 0, 0], 1, 0], [[0, 0, 0, 1], 0, 1]],
    ),
    dict(
        id="paip-adversarial-search",
        title="Adversarial search",
        contract="Four integer leaf utilities are two opponent replies under each of two root moves. Opponent minimizes and root maximizes; cfg[0] is added as a fixed position bonus. Changed root minimizes.",
        program=game(),
        changed=game(True),
        config=[1],
        samples=[[[-6, -1, -20, -2], -5, -19], [[15, 16, 16, 25], 17, 16]],
    ),
]
