"""Executable bounded witnesses for the eleven GoF behavioral relationships."""


def lit(value):
    return ["lit", value]


def arg(index):
    return ["arg", index]


def cfg(index):
    return ["cfg", index]


def var(index):
    return ["var", index]


def op(name, left, right):
    return [name, left, right]


def choose(test, yes, no):
    return ["ifv", test, yes, no]


def get(index):
    return ["getv", index]


def pack(*fields, radix=10):
    """Pack fields from most to least significant."""
    value = fields[0]
    for field in fields[1:]:
        value = op("add", op("mul", value, lit(radix)), field)
    return value


def digit(value, index, radix=10):
    return op("modv", op("divv", value, lit(radix**index)), lit(radix))


def case(name, contract, program, changed, config, samples):
    return {
        "id": "gof-" + name.lower().replace(" ", "-"),
        "title": name,
        "contract": contract,
        "program": program,
        "changed": changed,
        "config": config,
        "samples": samples,
    }


def chain(order):
    result = cfg(4)
    for handler in reversed(order):
        result = choose(op("ltv", arg(0), cfg(handler * 2)),
                        result, cfg(handler * 2 + 1))
    return choose(op("eqv", ["lenv"], lit(1)), result, lit(-1))


def command(enable_undo):
    # Low two decimal digits hold receiver balances. The next digits record
    # each reached operation in the live order of the queued requests.
    queued = get(op("add", var(1), lit(2)))
    request = var(0)
    receiver = digit(request, 1)
    amount = digit(request, 0)
    operation = digit(request, 2)
    previous = var(1)
    reverse = op("eqv", operation, lit(2))
    audit = op("eqv", operation, lit(1))
    reaches = choose(reverse, enable_undo, lit(1))
    delta = choose(audit, lit(0), choose(reverse, op("sub", lit(0), amount), amount))
    scale = choose(receiver, lit(10), lit(1))
    balance = op("add", op("modv", previous, lit(100)), op("mul", scale, delta))
    trace = op("divv", previous, lit(100))
    token = op("add", lit(1), op("add", receiver, op("mul", operation, lit(2))))
    recorded = op("add", op("mul", trace, lit(10)), token)
    body = ["letv", queued, choose(reaches, op("add", op("mul", recorded, lit(100)), balance), previous)]
    initial = pack(arg(1), arg(0))
    return ["foldv", op("sub", ["lenv"], lit(2)), initial, body]


def interpreter(root):
    # The grammar tree is specialized. The literal-only subtree has no
    # dependency on the fresh operand environment.
    expression = op(root, op("mul", arg(0), op("add", cfg(0), cfg(1))), arg(1))
    return choose(op("ltv", ["lenv"], lit(2)), lit(-1), expression)


def iterator(offset):
    # Each cursor owns its position. Advancing one does not consume the
    # other; the two independent traces share the same projected buffer.
    def run(start):
        records = op("divv", op("sub", ["lenv"], lit(1)), cfg(0))
        count = op("maxv", lit(0), op("sub", records, start))
        record = op("add", start, var(1))
        index = op("add", lit(1), op("add", op("mul", record, cfg(0)), offset))
        return ["foldv", count, lit(0), pack(var(0), get(index))]
    return pack(run(lit(0)), run(arg(0)), radix=10000)


def mediator(order):
    value = arg(1)
    for colleague in order:
        value = op("add", value, cfg(0)) if colleague == 0 else op("mul", value, cfg(1))
    stages = pack(lit(order[0] + 1), lit(order[1] + 1))
    return choose(arg(0), pack(stages, value, radix=1000), arg(1))


def memento(field):
    # Snapshot handles choose between two captured originator versions. Only
    # the selected field is restored into the current originator.
    saved = get(op("add", op("mul", arg(7), lit(2)), field))
    return pack(choose(field, arg(4), saved),
                choose(field, saved, arg(5)), arg(6))


def observer(reverse_order):
    # Run two publish operations. The first uses the initial registry and a
    # callback may detach B. The second sees that mutation.
    active_a = op("modv", arg(0), lit(2))
    active_b = op("divv", arg(0), lit(2))
    matches_a = op("eqv", arg(1), cfg(0))
    matches_b = op("orv", op("eqv", arg(1), cfg(0)), op("eqv", arg(1), cfg(1)))
    calls_a = op("andv", active_a, matches_a)
    calls_b = op("andv", active_b, matches_b)
    next_b = op("andv", calls_b, choose(op("andv", calls_a, arg(2)), lit(0), lit(1)))

    def publish(calls_b):
        hooks = [(calls_a, op("add", lit(10), arg(3))),
                 (calls_b, op("add", lit(20), arg(3)))]
        trace = lit(0)
        for enabled, token in reversed(hooks) if reverse_order else hooks:
            trace = choose(enabled, pack(trace, token, radix=100), trace)
        return trace

    return pack(publish(calls_b), publish(next_b), radix=10000)


def state(reverse_action):
    # State is carried across the two requests. Each successful action records
    # the state it observes along with the live payload.
    event = get(op("add", lit(1), op("mul", var(1), lit(2))))
    payload = get(op("add", lit(2), op("mul", var(1), lit(2))))
    current = digit(var(0), 0)
    trace = op("divv", var(0), lit(10))
    valid = op("eqv", current, event)
    next_state = op("sub", lit(1), current)
    observed = current if reverse_action else next_state
    action = pack(op("add", observed, cfg(0)), payload)
    updated = pack(pack(trace, action, radix=100), next_state)
    return ["foldv", lit(2), arg(0), choose(valid, updated, var(0))]


def strategy(insertion, descending):
    # Compare-and-exchange networks for at most three positions. The schedules
    # insert into a sorted prefix or select the first position, respectively.
    def sort(size, pairs):
        def step(index, packed):
            if index == len(pairs):
                return packed
            first, second = pairs[index]
            values = [digit(var(0), size - position - 1) for position in range(size)]
            a, b = values[first], values[second]
            swap = choose(descending, op("ltv", a, b), op("ltv", b, a))
            values[first], values[second] = choose(swap, b, a), choose(swap, a, b)
            return ["letv", packed, step(index + 1, pack(*values))]

        return step(0, pack(*(arg(index) for index in range(size))))

    pairs = [(0, 1), (1, 2), (0, 1)] if insertion else [(0, 1), (0, 2), (1, 2)]
    length = ["lenv"]
    return choose(op("ltv", length, lit(2)), arg(0),
                  choose(op("eqv", length, lit(2)), sort(2, [(0, 1)]), sort(3, pairs)))


def template_method(compute):
    prepared = var(0)
    run = choose(op("ltv", prepared, cfg(1)), lit(0), lit(1))
    calculated = op(compute, prepared, cfg(2))
    # Pack hook order, finish's admission argument and value, and resulting output.
    admitted = pack(lit(123), lit(1), calculated, calculated, radix=100)
    skipped = pack(lit(13), lit(0), prepared, arg(1), radix=100)
    return ["letv", op("add", arg(0), cfg(0)), choose(run, admitted, skipped)]


def visitor(pair_operator):
    number = op("mul", arg(1), cfg(0))
    pair = op(pair_operator, arg(1), op("mul", arg(2), cfg(1)))
    return choose(op("eqv", arg(0), lit(0)), pack(lit(1), number, radix=1000),
                  choose(op("eqv", arg(0), lit(1)), pack(lit(2), pair, radix=1000), lit(-1)))


CASES = [
    case("Chain of Responsibility",
         "One amount selects the first qualifying handler; malformed length yields -1. "
         "Configuration supplies two threshold/result pairs and the fallback. Edit reverses priority.",
         chain((0, 1)), chain((1, 0)), [100, 2, 10, 1, 0],
         [[[125], 2, 1], [[25], 1, 1], [[9], 0, 0], [[], -1, -1]]),
    case("Command",
         "Two initial receiver balances followed by up to six queued decimal op/receiver/amount "
         "triples. Operations 0, 1, 2 mean credit, audit and undo-credit. Receiver is 0 or 1. "
         "All intermediate balances are single digits. Result stores reached operation tokens "
         "(1 + receiver + 2*op) above receiver 1 and receiver 0 balances. Edit disables undo.",
         command(cfg(0)), command(lit(0)), [1],
         [[[3, 1, 2, 114, 202], 14513, 1415], [[1, 2, 11, 102], 2331, 2331],
          [[4, 5], 54, 54]]),
    case("Interpreter",
         "Evaluate a nested grammar tree with operand leaves 0 and 1 and two configured literal leaves. "
         "The edit replaces root addition with subtraction. Fewer than two operands yields -1. "
         "Operands are unsigned words and arithmetic must fit signed 32-bit at observation.",
         interpreter("add"), interpreter("sub"), [2, 3],
         [[[4, 7], 27, 13], [[0, 8], 8, -8], [[6], -1, -1]]),
    case("Iterator",
         "First input is the second cursor's starting record (0–4), followed by up to nine "
         "digits of packed records. The first cursor starts at record 0. Ignore partial records "
         "and return the ordered fields from each cursor as two base-10000 fields. "
         "Configuration supplies stride and projection index. Edit selects the next field.",
         iterator(cfg(1)), iterator(op("add", cfg(1), lit(1))), [2, 0],
         [[[1, 1, 2, 3, 4, 5], 130003, 240004], [[0, 8, 2], 80008, 20002],
          [[2, 7], 0, 0]]),
    case("Mediator",
         "Input is route 0 or 1 and value 0–100. Route 0 has no colleagues. Route 1 forwards "
         "the live value through configured addition and multiplication and returns the ordered "
         "colleague IDs above a three-digit result. Edit reverses the colleague order.",
         mediator((0, 1)), mediator((1, 0)), [3, 2],
         [[[1, 7], 12020, 21017], [[0, 9], 9, 9], [[1, 0], 12006, 21003]]),
    case("Memento",
         "Eight digits hold two captured field pairs, the three current fields, and the snapshot "
         "handle (0 or 1). The configured field determines which value to capture and restore. "
         "Output is the updated originator triple. Edit chooses the next field.",
         memento(cfg(0)), memento(op("add", cfg(0), lit(1))), [0],
         [[[1, 2, 4, 5, 8, 7, 9, 0], 179, 829],
          [[1, 2, 4, 5, 8, 7, 9, 1], 479, 859]]),
    case("Observer",
         "Inputs are active mask 0–3, tag, whether A detaches B, and payload digit. Two dispatches "
         "use the same tag and payload. Each dispatch records base-100 tokens 10+payload and "
         "20+payload for callbacks A and B. First dispatch uses its entry snapshot; second sees "
         "the detach. Result combines both traces in base 10000. Edit reverses callback order.",
         observer(False), observer(True), [3, 2],
         [[[3, 3, 1, 5], 15250015, 25150015], [[3, 3, 0, 2], 12221222, 22122212],
          [[2, 3, 1, 8], 280028, 280028], [[3, 2, 1, 4], 240024, 240024],
          [[3, 1, 0, 2], 0, 0]]),
    case("State",
         "Initial state 0 or 1 followed by two event/payload digit pairs. Event 0 opens a closed "
         "state and event 1 closes an open state. Success records observed state plus configured "
         "bias and payload as two digits. Rejected events preserve state without action. "
         "Result is the action trace followed by final state. Edit calls action before transition.",
         state(False), state(True), [1],
         [[[0, 0, 3, 1, 4], 23140, 13240], [[1, 0, 9, 0, 2], 1, 1],
          [[0, 1, 7, 0, 8], 281, 181]]),
    case("Strategy",
         "Sort a sequence of zero to three digits using the selected strategy and return the "
         "sorted digits in decimal. Empty input returns 0. The configured direction is "
         "0 ascending or 1 descending. Edit substitutes selection for insertion and reverses direction.",
         strategy(True, cfg(0)), strategy(False, op("sub", lit(1), cfg(0))), [0],
         [[[3, 1, 2], 123, 321], [[2, 3, 1], 123, 321], [[4, 4, 1], 144, 441],
          [[1, 2], 12, 21], [[], 0, 0]]),
    case("Template Method",
         "Two digits are input and prior output. Prepare adds configured offset, admission tests "
         "a configured threshold and compute uses its configured operand. Finish sees admission "
         "and value on both paths. Result packs hook order (prepare=1, compute=2, finish=3), "
         "admission, finish value, and output in base 100. Edit changes the compute hook.",
         template_method("mul"), template_method("add"), [2, 5, 3],
         [[[4, 7], 123011818, 123010909], [[1, 9], 13000309, 13000309]]),
    case("Visitor",
         "Element tag 0 passes the first field to the number method. Tag 1 passes both fields "
         "to the pair method. Return method ID above a three-digit result. Fields are digits; "
         "unsupported tags yield -1. Edit changes only the pair operation.",
         visitor("add"), visitor("maxv"), [3, 2],
         [[[0, 4, 9], 1012, 1012], [[1, 8, 3], 2014, 2008], [[9, 2, 3], -1, -1]]),
]
