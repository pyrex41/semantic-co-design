"""Bounded integer projections of the ten PAIP core compiler families."""


def lit(value):
    return ["lit", value]


def arg(index):
    return ["arg", index]


def cfg(index):
    return ["cfg", index]


def ref(name):
    return ["ref", name]


def bind(name, value, body):
    return ["bind", name, value, body]


def fold(index, acc, count, initial, body):
    return ["fold", index, acc, count, initial, body]


def get(index):
    return ["getv", index]


def choose(test, yes, no):
    return ["ifv", test, yes, no]


def lower(expr, scope=()):
    """Resolve named construction-time variables to projection lexical addresses."""
    op, *parts = expr
    if op in ("lit", "arg", "cfg", "lenv"):
        return expr
    if op == "ref":
        return ["var", scope.index(parts[0])]
    if op == "bind":
        name, value, body = parts
        return ["letv", lower(value, scope), lower(body, (name, *scope))]
    if op == "fold":
        index, acc, count, initial, body = parts
        return ["foldv", lower(count, scope), lower(initial, scope),
                lower(body, (acc, index, *scope))]
    return [op, *(lower(part, scope) for part in parts)]


def binary(op):
    return lambda left, right: [op, left, right]


add = binary("add")
sub = binary("sub")
mul = binary("mul")
div = binary("divv")
mod = binary("modv")
eq = binary("eqv")
lt = binary("ltv")
both = binary("andv")
either = binary("orv")
minimum = binary("minv")


def case(slug, title, contract, program, changed, config, samples):
    return dict(id="paip-" + slug, title=title, contract=contract,
                program=lower(program), changed=lower(changed), config=config, samples=samples)


def segment(minimum_length):
    split = ref("split")
    row_equal = fold(
        "item", "equal", split, lit(1),
        both(ref("equal"), eq(get(ref("item")), get(add(add(split, lit(1)), ref("item"))))),
    )
    valid = both(eq(["lenv"], add(mul(split, lit(2)), lit(1))),
                 both(eq(get(split), cfg(0)), row_equal))
    return fold("split", "found", ["lenv"], lit(-1),
                choose(both(lt(ref("found"), lit(0)), both(lt(lit(minimum_length - 1), split), valid)),
                       split, ref("found")))


def translator(literal_first):
    payload = arg(1)
    generic = bind("reflected", sub(cfg(2), payload),
                   add(lit(1000), mul(lit(11), ref("reflected"))))
    specific = add(lit(2000), payload)
    dispatch = choose(eq(payload, cfg(1)), specific, generic) if literal_first else generic
    return choose(both(eq(["lenv"], lit(2)), eq(arg(0), cfg(0))), dispatch, lit(-1))


def student(multiplier):
    # RHS values are integers; solve in numerator/denominator form and reduce
    # after propagation into y + x = total.
    noise = eq(arg(6), cfg(5))
    offset = choose(noise, lit(7), lit(6))
    valid_sum = both(eq(arg(0), cfg(0)), both(eq(arg(1), cfg(1)),
                     both(eq(arg(2), cfg(2)), eq(arg(3), cfg(3)))))
    valid_isolated = both(eq(arg(5), cfg(4)), both(eq(get(ref("offset")), cfg(2)),
                          both(eq(get(add(ref("offset"), lit(1))), cfg(3)),
                               eq(["lenv"], add(ref("offset"), lit(3))))))
    divisor = fold("step", "gcd", lit(multiplier), lit(1),
                   choose(both(eq(mod(ref("x"), add(ref("step"), lit(1))), lit(0)),
                               eq(mod(lit(multiplier), add(ref("step"), lit(1))), lit(0))),
                          add(ref("step"), lit(1)), ref("gcd")))
    solved = bind("x", get(add(ref("offset"), lit(2))),
                  bind("gcd", divisor,
                       add(mul(div(ref("x"), ref("gcd")), lit(10000)),
                           add(mul(div(sub(mul(arg(4), lit(multiplier)), ref("x")), ref("gcd")),
                                   lit(10)), div(lit(multiplier), ref("gcd"))))))
    return choose(valid_sum, choose(eq(["lenv"], lit(5)), lit(-2),
                                    bind("offset", offset, choose(valid_isolated, solved, lit(-1)))),
                  lit(-1))


def infix(edited):
    # A five-digit base-16 prefix tree. Operators and operands occupy disjoint digit ranges.
    def precedence(op):
        return choose(eq(op, cfg(0)), lit(3 if edited else 1),
                      choose(eq(op, cfg(1)), lit(2), lit(4 if edited else 3)))

    def alias(op):
        return choose(eq(op, cfg(0)), lit(15), op) if edited else op

    def tree(*digits):
        result = lit(0)
        for digit in digits:
            result = add(mul(result, lit(16)), digit)
        return result

    a, left, b, right, c = map(arg, range(5))
    right_associative = eq(left, cfg(2)) if not edited else lit(0)
    split_left = either(lt(precedence(left), precedence(right)),
                        both(eq(precedence(left), precedence(right)), right_associative))
    return choose(split_left, tree(alias(left), a, alias(right), b, c),
                  tree(alias(right), alias(left), a, b, c))


def rewrite(literal_first):
    # Child expressions use a constant tag or a binary sum tag.
    def child(start):
        return choose(eq(arg(start), cfg(2)), add(arg(start + 1), arg(start + 2)), arg(start + 1))

    lhs, rhs = ref("lhs"), ref("rhs")
    pick = choose(eq(lhs, cfg(3)), lit(300), add(lit(100), lhs)) if literal_first else add(lit(100), lhs)
    same = choose(eq(lhs, rhs), lhs, add(lit(1000), add(mul(lhs, lit(20)), rhs)))
    return bind("lhs", child(1), bind("rhs", child(4), choose(eq(arg(0), cfg(0)), pick, same)))


def integrate(edited):
    # coeff*x^factor multiplied by a unary function of x^power, or a bare monomial.
    coeff, factor, fn, power = map(arg, range(4))
    monomial = choose(eq(factor, lit(-1)), add(lit(200000), coeff),
                      add(lit(100000), add(mul(coeff, lit(100)), add(factor, lit(1)))))
    matched = eq(fn, cfg(2 if edited else 1))
    coefficient = sub(lit(0), coeff) if edited else coeff
    substituted = add(lit(500000 if edited else 400000),
                      add(mul(add(coefficient, lit(10)), lit(100)), add(mul(power, lit(10)), power)))
    residual = add(lit(600000), add(mul(coeff, lit(1000)),
                                   add(mul(add(factor, lit(1)), lit(100)), add(mul(fn, lit(10)), power))))
    return choose(eq(fn, cfg(0)), monomial,
                  choose(both(matched, eq(factor, sub(power, lit(1)))), substituted, residual))


def canonical(differentiate_y):
    # Two dense bivariate polynomials, each with x/y degree at most one.
    # Convolution visits the canonical coefficient indexed by the requested powers.
    x, y = ref("x"), ref("y")
    i, j = ref("i"), ref("j")
    other_x, other_y = sub(x, i), sub(y, j)
    valid = both(both(lt(lit(-1), other_x), lt(other_x, lit(2))),
                 both(lt(lit(-1), other_y), lt(other_y, lit(2))))
    product = fold(
        "item", "sum", lit(4), lit(0),
        bind("i", div(ref("item"), lit(2)),
             bind("j", mod(ref("item"), lit(2)),
                  choose(valid, add(ref("sum"), mul(get(add(lit(3), ref("item"))),
                                                   get(add(lit(7), add(mul(other_x, lit(2)), other_y))))),
                         ref("sum")))),
    )
    differential = eq(arg(0), cfg(0))
    x_shift = lit(0) if differentiate_y else differential
    y_shift = differential if differentiate_y else lit(0)
    multiplier = choose(differential, y if differentiate_y else x, lit(1))
    return bind("x", add(arg(1), x_shift), bind("y", add(arg(2), y_shift), mul(multiplier, product)))


def expert(strict):
    # Evaluate a shared leaf once, then apply the three rules that depend on it.
    # Keep the positive evidence in millionths until the final certainty output.
    cutoff = cfg(1 if strict else 0)
    hot, repair, positive = ref("hot"), ref("repair"), ref("positive")
    supported = lt(cutoff, hot)
    combined = sub(add(mul(repair, lit(100)), mul(cfg(4), lit(10000))),
                   mul(repair, cfg(4)))
    negative = mul(cfg(5), lit(10000))
    mixed = div(mul(lit(100), add(positive, negative)),
                sub(lit(1000000), minimum(positive, sub(lit(0), negative))))
    attention = choose(supported, div(mul(hot, cfg(3)), lit(100)), lit(0))
    premise = minimum(hot, arg(1))
    unsafe = choose(lt(cutoff, premise), premise, lit(0))
    return bind("hot", arg(0),
                bind("repair", choose(supported, mul(hot, cfg(2)), lit(0)),
                     bind("positive", combined,
                          add(mul(mixed, lit(1000000)), add(mul(attention, lit(1000)), unsafe)))))


def edge(reverse):
    # Each table index represents the two cells relative to the requested player.
    window = ref("window")
    row = mul(window, lit(6))

    def relative(offset):
        return bind("cell", get(add(add(lit(1), row), lit(offset))),
                    choose(eq(ref("cell"), lit(0)), lit(0),
                           choose(eq(ref("cell"), arg(0)), lit(1), lit(2))))

    left, right = (1, 0) if reverse else (0, 1)
    lookup = lit(0)
    for index in reversed(range(9)):
        lookup = choose(eq(ref("index"), lit(index)), cfg(index), lookup)
    return fold("window", "score", lit(2), lit(0),
                bind("index", add(mul(relative(left), lit(3)), relative(right)),
                     add(ref("score"), lookup)))


def scheme(edited):
    # The compiled state packs a control address, the captured mutable counter
    # and the value delivered to the continuation. Mode 0 has four addresses.
    state = ref("state")
    pc, counter, value = ref("pc"), ref("counter"), ref("value")
    increment = lit(2 if edited else 1)

    def transition(address, count, delivered):
        return add(mul(lit(address), lit(10000)), add(mul(lit(100), count), delivered))

    enter = transition(1, counter, value)
    call = choose(lt(counter, arg(1)), transition(2, add(counter, lit(1)), value),
                  transition(3, counter, value))
    reenter = transition(1, counter, add(value, increment))
    step = bind("pc", div(state, lit(10000)),
                bind("counter", mod(div(state, lit(100)), lit(100)),
                     bind("value", mod(state, lit(100)),
                          choose(eq(pc, lit(0)), enter,
                                 choose(eq(pc, lit(1)), call,
                                        choose(eq(pc, lit(2)), reenter, state))))))
    resume = mod(fold("iteration", "state", add(mul(arg(1), lit(2)), lit(2)),
                      transition(0, lit(0), arg(2)), step), lit(10000))
    tail = fold("iteration", "state", arg(1), arg(2),
                add(state, mul(sub(arg(1), ref("iteration")), increment)))
    escape = add(cfg(0), arg(2))
    return choose(eq(arg(0), lit(0)), resume, choose(eq(arg(0), lit(1)), tail, escape))


CASES = [
    case("segment-matcher", "Repeated segment matching",
         "Up to 31 flat integer tokens. Delimiter 9 may appear inside a capture. Return the repeated row length or -1. The edit changes star to plus.",
         segment(0), segment(1), [9],
         [([9], 0, -1), ([0, 9, 1, 9, 0, 9, 1], 3, 3), ([0, 9, 1, 0], -1, -1),
          ([3, 9, 3], 1, 1), ([], -1, -1)]),
    case("ordered-translator", "Ordered match and translation",
         "Send is token 7 and payloads are digits 0–9. Generic reflection produces 1000+11*(9-payload), encoding two uses of one reflected value. Literal 4 produces 2004 when moved ahead of the generic rule. -1 denotes no matching rule.",
         translator(False), translator(True), [7, 4, 9],
         [([7, 4], 1055, 2004), ([7, 2], 1077, 1077), ([8, 4], -1, -1), ([7], -1, -1)]),
    case("student", "Word equations and exact propagation",
         "Tokens [y,plus,x,equals,A,twice,(the),x,equals,B] encode two equations, with A and B in 0–20. Return 10000*xnum+10*ynum+den using a reduced shared denominator. -2 is an unresolved first equation and -1 is malformed syntax. The edit changes the twice template's multiplier to three.",
         student(2), student(3), [10, 11, 12, 13, 14, 15],
         [([10, 11, 12, 13, 10, 14, 15, 12, 13, 7], 70132, 70233),
          ([10, 11, 12, 13, 8, 14, 12, 13, 6], 30051, 20061),
          ([10, 11, 12, 13, 10], -2, -2), ([10, 12, 11, 13, 10], -1, -1)]),
    case("infix", "Infix precedence with operator aliases",
         "A five-token expression alternates operands 0–9 and operators 10–12. Operators +, *, and ^ have ascending precedence, and ^ associates to the right. Return a base-16 prefix tree. The edit sets + precedence to 3, changes its output alias to 15, and raises ^ to precedence 4 with left associativity.",
         infix(False), infix(True), [10, 11, 12],
         [([1, 10, 3, 11, 2], 0xA1B32, 0xBF132), ([2, 12, 3, 12, 4], 0xC2C34, 0xCC234),
          ([2, 11, 3, 10, 4], 0xAB234, 0xB2F34), ([2, 11, 3, 12, 4], 0xB2C34, 0xB2C34)]),
    case("rewriting", "Bottom-up ordered rewriting",
         "Root tag 0 is pick and 1 is same. Each child is [tag,a,b], with 0 denoting a and 2 denoting a+b. Child results are 0–19. Pick returns 100+value, with the specific pick(1) rule returning 300 when moved first. Same returns the common child or an encoded residual 1000+20*left+right.",
         rewrite(False), rewrite(True), [0, 1, 2, 1],
         [([0, 2, 0, 1, 0, 0, 0], 101, 300), ([0, 2, 1, 2, 0, 0, 0], 103, 103),
          ([1, 2, 2, 3, 2, 4, 1], 5, 5), ([1, 0, 3, 0, 0, 4, 0], 1064, 1064)]),
    case("integration", "Monomial substitution in symbolic integration",
         "Input [coefficient,factor,function,power] with coefficient 1–9, factor -1–8, and power 1–9. Function 0 is a bare monomial, 1 cos, 2 sin. Result tags 1 and 2 encode power rule with implicit denominator exponent+1 and log. Tags 4/5 encode sin/-cos substitution with coefficient offset by 10 and denominator power. Tag 6 preserves the input as 600000+1000*coefficient+100*(factor+1)+10*function+power. The edit replaces the cos rule with the sin rule.",
         integrate(False), integrate(True), [0, 1, 2],
         [([3, 1, 1, 2], 401322, 603212), ([2, 1, 2, 2], 602222, 500822),
          ([1, 0, 1, 2], 601112, 601112), ([4, 2, 0, 1], 100403, 100403),
          ([3, -1, 0, 1], 200003, 200003)]),
    case("canonical-algebra", "Canonical polynomial convolution and differentiation",
         "Input [operation,xdegree,ydegree,a00,a01,a10,a11,b00,b01,b10,b11] gives two dense bivariate polynomials and requests a canonical product coefficient. Operation 1 differentiates with respect to x before lookup. The edit differentiates with respect to y. Coefficients are -9–9 and query degrees 0–2.",
         canonical(False), canonical(True), [1],
         [([0, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8], 60, 60),
          ([0, 1, 1, 5, 6, 7, 8, 1, 2, 3, 4], 60, 60),
          ([1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0], 2, 2),
          ([1, 0, 0, 1, 2, 3, 0, 1, 2, 1, 0], 4, 4),
          ([1, 0, 0, 1, 3, 2, 0, 1, 2, 1, 0], 3, 5),
          ([0, 1, 1, 0, -1, 1, 0, 0, 1, 1, 0], 0, 0)]),
    case("expert-system", "Backward chaining with uncertain evidence",
         "Input [hot,manual] gives premise certainty from -100 to 100 in hundredths. The shared hot fact supports repair and attention; unsafe requires both premises. Repair also combines unconditional positive and negative evidence. Return 1000000*repair+1000*attention+unsafe. Each reported certainty truncates to hundredths. Rule support must exceed the cutoff, which the edit raises from 30 to 90.",
         expert(False), expert(True), [30, 90, 75, 50, 50, -40],
         [([80, -100], 66040000, 16000000), ([80, 70], 66040070, 16000000),
          ([20, 70], 16000000, 16000000), ([30, 100], 16000000, 16000000),
          ([90, 90], 72045090, 16000000), ([100, 95], 79050095, 79050095)]),
    case("edge-table", "Ternary edge window indexing",
         "Input [player,board...] uses a flattened 3x3 board with empty 0 and players 1/2. Index the precomputed nine weights by each window's two relative cells and sum. Windows are (0,1) and (6,7). The edit reverses both windows.",
         edge(False), edge(True), [0, 0, 0, 5, 8, 5, -5, -5, -5],
         [([1, 1, 1, 0, 0, 0, 0, 2, 1, 0], 3, 13), ([2, 1, 1, 0, 0, 0, 0, 2, 1, 0], 0, -10),
          ([1, 0, 2, 1, 0, 0, 2, 1, 0, 2], 5, -5), ([2, 0, 0, 0, 0, 0, 0, 0, 0, 0], 0, 0)]),
    case("scheme-compiler", "Lexical state, tail calls, and continuation resumption",
         "Input [mode,count,initial], with count 0–30 and initial 0–9. Mode 0 encodes final shared counter and resumed binding as 100*counter+value. Mode 1 accumulates a descending tail loop. Mode 2 escapes into an enclosing addition. The edit doubles the value increment in the resumed and tail paths.",
         scheme(False), scheme(True), [1],
         [([0, 2, 5], 207, 209), ([0, 0, 5], 5, 5), ([0, 5, 0], 505, 510),
          ([1, 8, 1], 37, 73), ([1, 0, 9], 9, 9), ([2, 0, 5], 6, 6)]),
]
