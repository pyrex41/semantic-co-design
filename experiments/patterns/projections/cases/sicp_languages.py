"""Integer projections of the SICP chapter 4–5 mechanism groups."""


def lit(n):
    return ["lit", n]


def arg(n):
    return ["arg", n]


def cfg(n):
    return ["cfg", n]


def get(index):
    return ["getv", index]


def choose(test, yes, no):
    return ["ifv", test, yes, no]


def bound(value, body):
    local = object()
    return ["letv", value, local, body(local)]


def fold(count, initial, body):
    accumulator, index = object(), object()
    return ["foldv", count, initial, accumulator, index, body(accumulator, index)]


def operations(*names):
    def binary(name):
        return lambda left, right: [name, left, right]

    return tuple(binary(name) for name in names)


add, sub, mul, div, mod, eq, lt, both = operations(
    "add", "sub", "mul", "divv", "modv", "eqv", "ltv", "andv"
)


def lower(expr, scope=()):
    if not isinstance(expr, list):
        return ["var", scope.index(expr)]
    opcode, *parts = expr
    if opcode in {"lit", "arg", "cfg"}:
        return expr
    if opcode == "letv":
        value, local, body = parts
        return [opcode, lower(value, scope), lower(body, (local, *scope))]
    if opcode == "foldv":
        count, initial, accumulator, index, body = parts
        return [opcode, lower(count, scope), lower(initial, scope),
                lower(body, (accumulator, index, *scope))]
    return [opcode, *(lower(part, scope) for part in parts)]


def case(slug, title, contract, config, program, changed, args, oracle):
    return dict(id=f"sicp-{slug}", title=title, contract=contract, config=config,
                program=lower(program), changed=lower(changed),
                samples=[[row, oracle(row, False), oracle(row, True)] for row in args])


def lexical(change):
    # Lower the setter and getter onto the same updated frame. The inner
    # procedure's parameter shadows the outer procedure's primitive name.
    return bound(arg(0), lambda captured:
                 bound(add(captured, cfg(0)), lambda updated:
                       bound(arg(1), lambda operand:
                             choose(arg(2), mul(updated, operand),
                                    (sub if change else add)(updated, operand)))))


def lexical_oracle(row, changed):
    captured, operand, procedure = row
    captured += 3
    if procedure:
        return captured * operand
    return captured - operand if changed else captured + operand


def power(base, count):
    return fold(count, lit(1), lambda value, _index: mul(value, lit(base)))


def digit(value, base, position):
    return mod(div(value, power(base, position)), lit(base))


def lazy(change):
    # State holds the result, force trace, and a two-entry memo table. Each
    # thunk reads its caller cell after the body has updated the cell.
    def step(state, site, cell):
        selected = digit(arg(2), 3, site)
        if change:
            selected = choose(selected, sub(lit(3), selected), lit(0))

        def force(slot):
            def update(memo):
                value = choose(eq(slot, lit(1)), cell, arg(1))
                new_total = mul(value, lit(1048576))
                trace = mod(div(state, lit(4096)), lit(256))
                trace_delta = mul(add(mul(trace, lit(15)), slot), lit(4096))
                return choose(memo, add(state, mul(memo, lit(1048576))),
                              add(state, add(mul(value, power(16, slot)),
                                             add(trace_delta, new_total))))

            return choose(slot, bound(digit(state, 16, slot), update), state)

        return bound(selected, force)

    return bound(add(arg(0), cfg(0)), lambda cell:
                 div(fold(lit(3), lit(0), lambda state, site: step(state, site, cell)),
                     lit(4096)))


def lazy_oracle(row, changed):
    x, y, schedule = row
    cell = [x]
    trace, memo = [], {}
    thunks = {1: lambda: cell[0], 2: lambda: y}
    cell[0] += 2
    total = 0
    for site in range(3):
        slot = schedule // 3**site % 3
        if changed and slot:
            slot = 3 - slot
        if slot:
            if slot not in memo:
                trace.append(slot)
                memo[slot] = thunks[slot]()
            total += memo[slot]
    trace_number = 0
    for slot in trace:
        trace_number = 16 * trace_number + slot
    return 256 * total + trace_number


def explicit(change):
    # A set bit identifies a pending continuation for that argument. The
    # descending calls are tail transfers except when the caller suspends add.
    def descend(state, index):
        def transfer(n):
            return choose(eq(mod(n, lit(2)), lit(int(change))),
                          add(state, power(2, index)),
                          add(state, mul(add(div(state, lit(4096)), cfg(0)), lit(4096))))

        return bound(sub(arg(0), index), transfer)

    def resume(value, index, stack):
        depth = sub(sub(arg(0), lit(1)), index)
        return choose(digit(stack, 2, depth), add(value, add(index, lit(1))), value)

    return bound(fold(arg(0), mul(arg(1), lit(4096)), descend), lambda state:
                 fold(arg(0), div(state, lit(4096)),
                      lambda val, index: resume(val, index, mod(state, lit(4096)))))


def explicit_oracle(row, changed):
    n, seed = row

    def run(n, accumulator):
        if n == 0:
            return accumulator
        if n % 2 == int(changed):
            return n + run(n - 1, accumulator)
        return run(n - 1, 2 * accumulator + 2)

    return run(n, seed)


def amb(change):
    # The least significant digit is the current cell. Each trial saves the
    # cell while assignment and any successful output update the branch frame.
    radix = lit(16)

    def outer(frame, i):
        def inner(frame, j):
            candidate = add(arg(1), add(mul(i, lit(3)), j))

            def trial(saved_cell):
                assigned = add(sub(frame, saved_cell), candidate)

                def branch(state):
                    x = mod(state, radix)
                    passes = (lt if change else eq)(mod(x, cfg(1)), arg(2))
                    # Append x to the ordered answer prefix, leaving x live in
                    # the low digit until the failure continuation restores it.
                    success = add(mul(state, radix), x)
                    observed = choose(passes, success, state)
                    return add(mul(div(observed, radix), radix), saved_cell)

                return bound(assigned, branch)

            return bound(mod(frame, radix), trial)

        return fold(cfg(0), frame, inner)

    return fold(arg(3), arg(0), outer)


def amb_oracle(row, changed):
    initial, start, threshold, outer = row
    answers = []
    x = initial
    for i in range(outer):
        for j in range(2):
            saved = x
            x = start + 3 * i + j
            if (x % 4 < threshold if changed else x % 4 == threshold):
                answers.append(x)
            x = saved
    answers.append(x)
    packed = 0
    for value in answers:
        packed = 16 * packed + value
    return packed


def query(change):
    # All edges originate at a lower-numbered vertex. Extending the packed
    # proof frame by vertex order is a topological schedule of the two rules.
    def extend(proofs, target):
        def join(count, source):
            edge = get(add(lit(2), add(mul(source, cfg(0)), target)))
            return add(count, mul(digit(proofs, 32, source), edge))

        count = add(eq(target, arg(0)), fold(target, lit(0), join))
        return add(proofs, mul(count, power(32, target)))

    def project(proofs):
        def answer(count, vertex):
            guard = lt(lit(1), vertex) if change else lit(1)
            present = eq(digit(arg(1), 2, vertex), lit(0))
            return choose(both(guard, present),
                          add(count, sub(digit(proofs, 32, vertex), eq(vertex, arg(0)))),
                          count)

        return fold(cfg(0), lit(0), answer)

    return bound(fold(cfg(0), lit(0), extend), project)


def query_oracle(row, changed):
    start, blocked, *edges = row

    def proofs(source):
        for target in range(source + 1, 4):
            for _ in range(edges[source * 4 + target]):
                yield target
                yield from proofs(target)

    return sum(1 for target in proofs(start)
               if not (blocked & 1 << target) and (not changed or target >= 2))


def machine(change):
    # Registers: a, b, continuation and PC. Both call sites transfer to the
    # same Euclid controller and use its computed goto when b becomes zero.
    def pack(a, b, cont, pc, steps):
        return add(a, add(mul(b, lit(32)),
                          add(mul(cont, lit(1024)),
                              add(mul(pc, lit(4096)), mul(steps, lit(16384))))))

    def step(state, _index):
        def transition(pc):
            def registers(a, b, cont):
                steps = add(div(state, lit(16384)), lit(1))
                return choose(eq(pc, lit(2)),
                              pack(a, arg(1 if change else 2), lit(0), lit(1), steps),
                              choose(b, pack(b, mod(a, b), cont, lit(1), steps),
                                     pack(a, b, cont, cont, steps)))

            active = bound(mod(state, lit(32)), lambda a:
                           bound(digit(state, 32, lit(1)), lambda b:
                                 bound(mod(div(state, lit(1024)), lit(4)),
                                       lambda cont: registers(a, b, cont))))
            return choose(pc, active, state)

        return bound(mod(div(state, lit(4096)), lit(4)), transition)

    initial = pack(arg(0), arg(2 if change else 1), lit(2), lit(1), lit(0))

    return bound(fold(cfg(0), initial, step), lambda state:
                 add(mod(state, lit(32)), mul(div(state, lit(16384)), lit(32))))


def machine_oracle(row, changed):
    a, b, c = row
    steps = 3  # two subroutine returns and the second call's setup
    for operand in ((c, b) if changed else (b, c)):
        while operand:
            a, operand = operand, a % operand
            steps += 1
    return a + 32 * steps


def preserving(change):
    need = "x" if change else "n"
    modifications = {"n", "x"}
    requested = {"n"}
    saves = modifications & {need} & requested
    radix = 2048
    stack_slot = lit(radix**2)
    push_slot = lit(radix**3)

    def save_n(state):
        return add(state, add(mul(mod(state, lit(radix)), stack_slot), push_slot))

    def first_fragment(state):
        new_n = add(cfg(0), lit(1024))
        new_x = add(digit(state, radix, lit(1)), cfg(0))
        stack_and_pushes = mul(div(state, stack_slot), stack_slot)
        return add(stack_and_pushes, add(mul(new_x, lit(radix)), new_n))

    def restore_n(state):
        saved = digit(state, radix, lit(2))
        new_x = digit(state, radix, lit(1))
        pushes = mul(div(state, push_slot), push_slot)
        return add(pushes, add(mul(new_x, lit(radix)), saved))

    def second_fragment(state):
        result = sub(digit(state, radix, lit(int(need == "x"))), lit(1024))
        pushes = div(state, push_slot)
        return add(mul(result, lit(2)), pushes)

    initial = add(add(arg(0), lit(1024)), mul(add(arg(1), lit(1024)), lit(radix)))
    with_save = bound(initial, save_n) if saves else initial
    modified = bound(with_save, first_fragment)
    with_restore = bound(modified, restore_n) if saves else modified
    return bound(with_restore, second_fragment)


def preserving_oracle(row, changed):
    n, x = row
    return 2 * (x + 7) if changed else 2 * n + 1


def collector(change):
    # The low four base-5 digits are a work queue of original object IDs.
    # Positions in that queue are the forwarding addresses (zero is null).
    def scan(state, step):
        def visit(queue, source, field):
            target_index = add(lit(1), add(mul(sub(source, lit(1)), lit(2)), field))

            def forward(target):
                def find(address, position):
                    return choose(eq(digit(queue, 5, position), target),
                                  add(position, lit(1)), address)

                def insert(address):
                    copied_factor = mul(lit(625), power(5, step))
                    return add(state, mul(address, copied_factor))

                def append(copied):
                    return add(insert(add(copied, lit(1))), mul(target, power(5, copied)))

                return choose(target,
                              bound(fold(cfg(0), lit(0), find), lambda address:
                                    choose(address, insert(address),
                                           append(fold(cfg(0), lit(0), lambda count, position:
                                                       add(count, lt(lit(0), digit(queue, 5, position))))))),
                              state)

            return forward(get(target_index))

        return bound(mod(state, lit(625)), lambda queue:
                     bound(digit(queue, 5, div(step, lit(2))), lambda source:
                           choose(source, visit(queue, source,
                                                (sub(lit(1), mod(step, lit(2))) if change
                                                 else mod(step, lit(2)))), state)))

    return fold(mul(cfg(0), lit(2)), arg(0), scan)


def collector_oracle(row, changed):
    root, *heap = row
    if root == 0:
        return 0
    queue = [root]
    forwarded = {root: 1}
    output = []
    for old_address in queue:
        fields = heap[2 * (old_address - 1):2 * old_address]
        for target in (reversed(fields) if changed else fields):
            if target and target not in forwarded:
                queue.append(target)
                forwarded[target] = len(queue)
            output.append(forwarded.get(target, 0))
    return sum(value * 5**i for i, value in enumerate(queue + [0] * (4 - len(queue)) + output))


def edges(start, blocked, *facts):
    row = [start, blocked] + [0] * 16
    for source, target in facts:
        row[2 + 4 * source + target] += 1
    return row


CASES = [
    case("lexical-addressing", "Syntax analysis and lexical addressing",
         "Args are [captured cell, operand, procedure tag]. The two values are in "
         "[-1000, 1000] and tag 0 or 1 selects a first-class arithmetic procedure. "
         "The shared cell is updated by config[0] before the inner closure reads it. "
         "The program edit changes only the procedure at tag 0, addition to subtraction.",
         [3], lexical(False), lexical(True), [[4, 8, 0], [-5, 7, 1], [13, -4, 0]],
         lexical_oracle),
    case("call-by-need", "Call-by-need",
         "Args are [caller cell 0..13, second cell 1..15, demand schedule 0..26]. "
         "Three little-endian ternary digits encode skip, thunk 1, or thunk 2. "
         "Thunk 1 sees the caller cell after the update by config[0]. Each thunk "
         "is memoized on first demand. A nonzero nibble memo value signals a successful "
         "force. The result packs total demand values times 256 plus the first-force "
         "order as base-16 thunk IDs. The edit swaps the demand sites' thunk IDs.",
         [2], lazy(False), lazy(True), [[3, 10, 0], [6, 3, 13], [1, 9, 16], [9, 4, 20], [5, 14, 5]],
         lazy_oracle),
    case("explicit-control", "Explicit control and procedure linkage",
         "Args are [recursive countdown 0..12, initial accumulator 0..20]. "
         "Each tail call transfers twice the accumulator plus config[0]. Non-tail "
         "calls stack a continuation to add the local countdown on return. The edit "
         "changes the parity of arguments that require a pending continuation.",
         [2], explicit(False), explicit(True), [[0, 7], [4, 0], [7, 2], [12, 20]],
         explicit_oracle),
    case("amb", "Backtracking with reversible assignment",
         "Args are [initial cell 1..15, first choice 1..5, residue 0..3, outer count 0..4]. "
         "For each runtime outer choice i, config[0] inner choices assign x = first + 3*i+j. "
         "A passing branch reports x and backtracking restores the previous cell. "
         "The fallback reports the restored cell. Answers pack as base-16 digits, "
         "first answer most significant. Config[1] sets the modulus and the edit "
         "changes the acceptance predicate from equality to strict less-than.",
         [2, 4], amb(False), amb(True), [[3, 5, 0, 0], [7, 1, 1, 4], [9, 3, 3, 3], [4, 2, 2, 1]],
         amb_oracle),
    case("rule-based-query", "Rule-based query",
         "Args are [start vertex 0..3, blocked vertex mask 0..15, 16 row-major edge counts]. "
         "Config[0] sets the vertex count to four. Counts 0..2 are facts with "
         "multiplicity and are zero on and below the diagonal. Direct and recursive "
         "rules produce one proof per nonempty path with duplicate facts contributing "
         "separate proofs. Ground negation removes proofs ending at blocked vertices. "
         "The edited query guard additionally removes endpoints below 2.",
         [4], query(False), query(True),
         [edges(0, 0), edges(0, 0, (0, 1), (0, 1), (1, 2), (0, 2), (2, 3)),
          edges(0, 8, (0, 1), (1, 2), (2, 3)), edges(1, 4, (1, 2), (2, 3), (1, 3))],
         query_oracle),
    case("register-machine-assembly", "Register-machine assembly",
         "Args are three register values in 0..31. The controller invokes the same "
         "Euclid subroutine twice, with different return addresses. Each controller "
         "step executes either a remainder transition, computed return, or second-call "
         "setup. The result packs the final gcd plus 32 times the active step count. "
         "Config[0] bounds the controller run. The edited controller swaps the order "
         "of the last two input registers at the two call sites.",
         [24], machine(False), machine(True), [[0, 0, 0], [6, 10, 3], [21, 0, 14], [8, 13, 30]],
         machine_oracle),
    case("register-preservation", "Register preservation",
         "Args are [n, x] in [-1000, 1000]. The first fragment sets n to config[0] "
         "and adds that value to x. The linked second fragment originally reads n. "
         "Static liveness inserts a save and restore of n because n is requested "
         "live across the sequence. The edited fragment reads the updated x. "
         "The result is twice the observed register plus the number of pushes.",
         [7], preserving(False), preserving(True), [[46, 3], [0, -7], [-6, 10]],
         preserving_oracle),
    case("copying-collection", "Copying collection",
         "Args are [root, eight reference fields in old-address order]. Config[0] "
         "fixes a four-record heap, two fields per record. All pointers are 0..4 "
         "with 0 null. The result contains four little-endian base-5 digits listing "
         "old addresses in forwarding order, padded with zeros, then eight digits "
         "for copied reference fields in the new address space. The edit swaps "
         "the source fields at every scan, changing the forwarding order.",
         [4], collector(False), collector(True),
         [[0, 4, 2, 1, 3, 4, 3, 2, 1], [3, 0, 0, 2, 2, 4, 1, 3, 4],
          [1, 2, 3, 4, 1, 4, 1, 0, 4], [4, 0, 0, 1, 0, 2, 0, 3, 1]],
         collector_oracle),
]
