"""Integer projections of the SICP procedural and data abstraction cases."""


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


def bind(value, body):
    return ["letv", value, body]


def fold(count, initial, body):
    return ["foldv", count, initial, body]


def get(index):
    return ["getv", index]


def add(left, right):
    return op("add", left, right)


def sub(left, right):
    return op("sub", left, right)


def mul(left, right):
    return op("mul", left, right)


def div(left, right):
    return op("divv", left, right)


def rem(left, right):
    return op("modv", left, right)


def eq(left, right):
    return op("eqv", left, right)


def lt(left, right):
    return op("ltv", left, right)


def positive_remainder(value, radix):
    return bind(rem(value, radix), choose(lt(var(0), lit(0)), add(var(0), radix), var(0)))


def pack(left, right, radix):
    return add(mul(left, radix), right)


def item(slug, title, contract, program, changed, config, samples):
    return {
        "id": "sicp-" + slug,
        "title": title,
        "contract": contract,
        "program": program,
        "changed": changed,
        "config": config,
        "samples": samples,
    }


def integer_newton(tolerance):
    # A ceiling-root-or-higher start makes every accepted step a decrease.
    next_guess = div(add(var(0), div(cfg(0), var(0))), lit(2))
    return fold(cfg(1), arg(0), bind(next_guess, choose(
        lt(cfg(tolerance), sub(var(1), var(0))), var(0), var(1),
    )))


def recurrence(last_coefficient, previous_coefficient, offset):
    # Fold state packs the previous and current terms, each offset by 128.
    radix = lit(256)
    bias = lit(128)
    previous = sub(div(var(0), radix), bias)
    current = sub(rem(var(0), radix), bias)
    following = add(add(mul(current, cfg(last_coefficient)),
                        mul(previous, cfg(previous_coefficient))), cfg(offset))
    initial = pack(add(arg(1), bias), add(arg(2), bias), radix)
    sequence = fold(sub(arg(0), lit(1)), initial,
                    pack(rem(var(0), radix), add(following, bias), radix))
    return choose(eq(arg(0), lit(0)), arg(1), sub(rem(sequence, radix), bias))


def modular_power(exponent_slot):
    """Specialize four exponent bits into a modular multiplication schedule."""
    def product(left, right):
        return rem(mul(left, right), cfg(0))

    normalized = positive_remainder(arg(0), cfg(0))
    # Both supported exponents have their leading bit at position 3.
    plan = normalized
    for place in (4, 2, 1):
        bit = rem(div(cfg(exponent_slot), lit(place)), lit(2))
        squared = product(var(0), var(0))
        plan = bind(plan, choose(bit, product(squared, normalized), squared))
    return plan


def fixed_gcd(divisor_slot, span):
    radix = lit(16)

    def table_entry(residue):
        # Both inputs and the full bounded Euclidean fold are static for
        # each leaf. The live dividend is used only to choose a leaf.
        state = var(0)
        a, b = div(state, radix), rem(state, radix)
        next_state = choose(eq(b, lit(0)), state, pack(b, rem(a, b), radix))
        initial = pack(cfg(divisor_slot), lit(residue), radix)
        return div(fold(lit(5), initial, next_state), radix)

    def lookup(start, end):
        if end - start == 1:
            return table_entry(start)
        middle = (start + end) // 2
        return choose(lt(var(0), lit(middle)), lookup(start, middle), lookup(middle, end))

    residue = positive_remainder(arg(0), cfg(divisor_slot))
    return bind(residue, lookup(0, span))


def prime(candidate):
    # The fixed sorted prime divisor schedule covers candidates through 100.
    verdict = lit(1)
    for divisor in (7, 5, 3, 2):
        verdict = choose(lt(candidate, lit(divisor * divisor)), lit(1),
                         choose(eq(rem(candidate, lit(divisor)), lit(0)), lit(0), verdict))
    return choose(lt(candidate, cfg(0)), lit(0), verdict)


def bisect(target):
    # Maintain a bracket and return its lower endpoint on the integer grid.
    radix = lit(128)
    midpoint = div(add(div(var(0), radix), rem(var(0), radix)), lit(2))
    step = bind(midpoint, choose(
        lt(cfg(target), mul(var(0), var(0))),
        pack(div(var(1), radix), var(0), radix),
        pack(var(0), rem(var(1), radix), radix),
    ))
    result = div(fold(cfg(2), pack(arg(0), arg(1), radix), step), radix)
    bracket_valid = op("andv", lt(mul(arg(0), arg(0)), cfg(target)),
                       lt(cfg(target), mul(arg(1), arg(1))))
    return choose(bracket_valid, result, lit(-1))


def rational_offset(offset):
    # Common denominator and input scaling are fixed. The live numerator
    # still decides the normalizing gcd.
    numerator = add(mul(arg(0), cfg(0)), cfg(offset))
    gcd = lit(1)
    for divisor in (2, 3, 4, 6, 12):
        gcd = choose(eq(rem(var(0), lit(divisor)), lit(0)), lit(divisor), gcd)
    return bind(numerator, bind(gcd, pack(
        add(div(var(1), var(0)), lit(128)), div(cfg(1), var(0)), lit(16),
    )))


def interval(negative_left):
    radix = lit(64)
    bias = lit(32)
    if negative_left:
        lower = mul(choose(lt(arg(1), lit(0)), cfg(3), cfg(2)), arg(1))
        upper = mul(choose(lt(lit(0), arg(0)), cfg(3), cfg(2)), arg(0))
    else:
        lower = mul(choose(lt(arg(0), lit(0)), cfg(1), cfg(0)), arg(0))
        upper = mul(choose(lt(arg(1), lit(0)), cfg(0), cfg(1)), arg(1))
    return pack(add(lower, bias), add(upper, bias), radix)


def tree(shape):
    if isinstance(shape, int):
        return arg(shape)
    left, right = shape
    # A nonassociative combiner exposes topology and child order.
    return add(mul(cfg(0), tree(left)), tree(right))


def painter(order_slot):
    # The four transform parameters describe x -> (a+b*x)/2 and
    # x -> c+d*x. Compose against each static unit diagonal endpoint.
    scale_origin, scale_edge, reflect_origin, reflect_edge = map(cfg, range(4))

    def diagonal_x_twice(x):
        scaled_after_reflected = add(scale_origin, mul(scale_edge, add(reflect_origin, mul(reflect_edge, x))))
        reflected_after_scaled = add(mul(lit(2), reflect_origin),
                                     mul(reflect_edge, add(scale_origin, mul(scale_edge, x))))
        return choose(cfg(order_slot), reflected_after_scaled, scaled_after_reflected)

    endpoint = arg(7)
    segment = arg(6)
    diagonal_x = choose(eq(endpoint, lit(0)), diagonal_x_twice(lit(0)), diagonal_x_twice(lit(1)))
    local_x_twice = choose(eq(segment, lit(0)), mul(lit(2), endpoint), diagonal_x)
    local_y = choose(eq(segment, lit(0)), lit(0), endpoint)

    def coordinate(origin, horizontal, vertical):
        return add(add(mul(lit(2), arg(origin)), mul(arg(horizontal), local_x_twice)),
                   mul(mul(lit(2), arg(vertical)), local_y))

    return pack(add(coordinate(0, 2, 4), lit(32)),
                add(coordinate(1, 3, 5), lit(32)), lit(64))


def ordered_membership(indices):
    if not indices:
        return lit(0)
    middle = len(indices) // 2
    key = cfg(indices[middle])
    return choose(eq(arg(0), key), lit(1), choose(
        lt(arg(0), key), ordered_membership(indices[:middle]),
        ordered_membership(indices[middle + 1:]),
    ))


def huffman(first, second):
    one = lit(1)
    two = lit(2)
    symbol = get(add(var(1), one))
    encode = fold(sub(["lenv"], one), one, choose(
        eq(symbol, cfg(0)), mul(var(0), two),
        pack(var(0), choose(eq(symbol, cfg(first)), two, lit(3)), lit(4)),
    ))

    # Low bit of accumulator is the current tree node. Remaining bits store
    # the base-4 output message. The root is node 0, the interior node is 1.
    state = rem(var(0), two)
    message = div(var(0), two)
    bit = get(add(var(1), one))
    decoded = choose(eq(state, lit(0)),
                     choose(eq(bit, lit(0)), mul(pack(message, cfg(0), lit(4)), two),
                            add(var(0), one)),
                     mul(pack(message, choose(eq(bit, lit(0)), cfg(first), cfg(second)),
                              lit(4)), two))
    final_state = fold(sub(["lenv"], one), lit(0), decoded)
    decode = bind(final_state, choose(eq(rem(var(0), two), lit(0)),
                                     div(var(0), two), lit(-1)))
    return choose(eq(arg(0), lit(0)), encode, decode)


def polynomial(second_degree_slot, output_slots):
    terms = [(add(cfg(i), cfg(j)), mul(arg(left), arg(right)))
             for left, i in enumerate((1, second_degree_slot))
             for right, j in enumerate((1, 3), start=2)]
    result = lit(0)
    for output in output_slots:
        coefficient = cfg(0)
        for degree, term in terms:
            coefficient = add(coefficient, choose(eq(degree, cfg(output)), term, lit(0)))
        result = pack(result, coefficient, lit(64))
    return result


def derivative(variable_slot):
    # In the fixed (x^p+y)(x+c) schema, the configuration chooses p, c,
    # and the differentiation variable. Tangents use the sum, product,
    # and natural-power rules and the configured direction.
    x, y = arg(0), arg(1)
    exponent, offset = cfg(0), cfg(1)
    dx = cfg(variable_slot)
    dy = sub(lit(1), dx)

    def power(count):
        value = lit(1)
        for factor in range(3):
            value = bind(value, choose(lt(lit(factor), count), mul(var(0), x), var(0)))
        return value

    left = add(power(exponent), y)
    right = add(x, offset)
    left_tangent = choose(dx, mul(exponent, power(sub(exponent, lit(1)))), dy)
    return add(mul(left_tangent, right), choose(dx, left, lit(0)))


def complex_operation(operation_slot):
    magnitude, turns, x, y = map(arg, range(4))
    negative = sub(lit(0), magnitude)
    # Conversion shared by the exact mixed method and the coercion path.
    a = choose(eq(turns, lit(0)), magnitude,
               choose(eq(turns, lit(2)), negative, lit(0)))
    b = choose(eq(turns, lit(1)), magnitude,
               choose(eq(turns, lit(3)), negative, lit(0)))

    def rectangular(real, imag):
        return pack(add(real, cfg(0)), add(imag, cfg(0)), lit(64))

    direct = rectangular(add(a, x), add(b, y))
    promoted = rectangular(sub(mul(a, x), mul(b, y)), add(mul(a, y), mul(b, x)))
    # Exact method availability in the fixed table is indexed by operation
    # for the known polar/rectangular argument representations.
    return choose(cfg(operation_slot), direct, promoted)


CASES = [
    item(
        "newton-square-root", "Newton square-root iteration",
        "Input [guess], ceil(sqrt(37)) through 10000. Bind target 37 and 16 integer "
        "Newton steps. Integer division truncates; freeze when the decrease is at "
        "most 0, or 2 after "
        "editing the stopping rule. Return the last integer guess.",
        integer_newton(2), integer_newton(3), [37, 16, 0, 2],
        [[[7], 6, 7], [[20], 6, 6], [[10000], 6, 6], [[9], 6, 6]],
    ),
    item(
        "recurrence", "Sliding-pair affine recurrence and Fibonacci",
        "Input [index, first, second] with index 0..9 and seeds -1..1. Return term "
        "index with seeds at positions 0 and 1. The original uses a(n-1)+a(n-2) "
        "(Fibonacci for seeds 0,1). The edit uses a(n-1)-a(n-2)+2.",
        recurrence(0, 1, 2), recurrence(0, 3, 4), [1, 1, 0, -1, 2],
        [[[0, -1, 1], -1, -1], [[1, 1, -1], -1, -1],
         [[6, 0, 1], 8, 0], [[9, 1, 1], 55, 3]],
    ),
    item(
        "modular-power", "Fixed exponent and multiplication law",
        "Input [base], -10000..10000. Return base raised to 13 modulo 23 with "
        "residue 0..22. Edit the fixed exponent to 10 while preserving the "
        "configured modular multiplication law.",
        modular_power(1), modular_power(2), [23, 13, 10],
        [[[0], 0, 0], [[2], 4, 12], [[-2], 19, 12], [[24], 1, 1]],
    ),
    item(
        "fixed-gcd", "GCD residue table",
        "Input [value], -10000..10000. Return gcd(value, 6), or gcd(value, 8) "
        "after the edit, by selecting the fixed table entry at the nonnegative "
        "residue of value. The object program computes each static entry with "
        "a bounded Euclidean fold and selects the entry with a decision tree.",
        fixed_gcd(0, 6), fixed_gcd(1, 8), [6, 8],
        [[[0], 6, 8], [[-16], 2, 8], [[18], 6, 2], [[35], 1, 1]],
    ),
    item(
        "primality", "Bounded deterministic trial division",
        "Input [candidate], 0..98. Return 1 when candidate is prime, else 0. "
        "The edit requires both candidate and candidate+2 to be prime. The "
        "static divisors 2,3,5,7 suffice through 100.",
        prime(arg(0)), op("andv", prime(arg(0)), prime(add(arg(0), cfg(0)))), [2],
        [[[0], 0, 0], [[2], 1, 0], [[3], 1, 1], [[97], 1, 0], [[49], 0, 0]],
    ),
    item(
        "half-interval", "Higher-order half-interval method",
        "Input [lower, upper] with 0<=lower<upper<=100. Bind f(x)=x²-29, "
        "or f(x)=x²-53 after editing the function. Return -1 if the live "
        "endpoint values have the same sign. Otherwise, eight integer "
        "bisections narrow the bracket to adjacent integers and return "
        "its lower endpoint.",
        bisect(0), bisect(1), [29, 53, 8],
        [[[0, 9], 5, 7], [[0, 6], 5, -1], [[6, 10], -1, 7], [[2, 100], 5, 7]],
    ),
    item(
        "rational-offset", "Normalized rational addition",
        "Input [numerator], -20..20, representing numerator/4. Add the fixed "
        "offset 1/6, or 1/3 after the edit. Return (reduced_numerator+128)*16 "
        "+ reduced_positive_denominator. The common denominator is 12.",
        rational_offset(2), rational_offset(3), [3, 12, 2, 4],
        [[[0], 2070, 2067], [[2], 2083, 2134], [[-4], 1974, 2019], [[3], 2236, 2268]],
    ),
    item(
        "interval-product", "Interval multiplication with a bound sign class",
        "Input [lower, upper], -7..7 with lower<=upper. Multiply the live "
        "interval by [1,4], or by [-3,-1] after editing the fixed left operand. "
        "Return (product_lower+32)*64 + product_upper+32.",
        interval(False), interval(True), [1, 4, -3, -1],
        [[[-4, -2], 1054, 2220], [[2, 3], 2220, 1502],
         [[-2, 4], 1584, 1318], [[0, 0], 2080, 2080]],
    ),
    item(
        "tree-fold", "Fixed hierarchical fold",
        "Input [a,b,c], each -100..100. Fold the fixed tree ((a,b),c) using "
        "combine(x,y)=3*x+y. The edit changes topology to (a,(b,c)).",
        tree(((0, 1), 2)), tree((0, (1, 2))), [3],
        [[[2, 3, 4], 31, 19], [[-2, 3, 4], -5, 7], [[0, -5, 6], -9, -9]],
    ),
    item(
        "affine-painter", "Nested affine transforms and painter overlay",
        "Input [origin_x,origin_y,edge1_x,edge1_y,edge2_x,edge2_y,segment,endpoint]. "
        "Frame components are -4..4. Segment 0 is (0,0) to (1,0); segment 1 is "
        "the diagonal (0,0) to (1,1) reflected horizontally, then mapped to "
        "the right half. Edit by reversing the two transforms. Select either "
        "endpoint 0 or 1, map it through the live frame, and return "
        "(2*x+32)*64 + (2*y+32).",
        painter(4), painter(5), [1, 1, 1, -1, 0, 1],
        [[[0, 0, 1, 0, 0, 1, 0, 1], 2208, 2208],
         [[0, 0, 1, 0, 0, 1, 1, 0], 2208, 2144],
         [[0, 0, 1, 0, 0, 1, 1, 1], 2146, 2082],
         [[3, 2, 1, -4, 4, 1, 1, 1], 3042, 2982]],
    ),
    item(
        "symbolic-derivative", "Derivative of a fixed symbolic expression",
        "Input [x,y] with each integer -100..100. Return the derivative of "
        "(x³+y)(x+2) with respect to x, or with respect to y after editing "
        "the differentiation variable.",
        derivative(2), derivative(3), [3, 2, 1, 0],
        [[[0, 7], 7, 2], [[1, 3], 13, 3], [[-2, 6], -2, 0]],
    ),
    item(
        "ordered-set", "Membership in a fixed ordered set",
        "Input [key], -10000..10000. Return 1 if key is a member of "
        "{-2,1,5,8,13,17,21}, otherwise 0. Replace 8 with 9 after the edit. "
        "Both programs use binary search unrolled over the ordered set.",
        ordered_membership((0, 1, 2, 3, 5, 6, 7)),
        ordered_membership((0, 1, 2, 4, 5, 6, 7)),
        [-2, 1, 5, 8, 9, 13, 17, 21],
        [[[-2], 1, 1], [[8], 1, 0], [[9], 0, 1], [[15], 0, 0], [[21], 1, 1]],
    ),
    item(
        "huffman-codec", "Huffman codebook and decoder transitions",
        "Input [mode,...payload], at most 12 payload entries. Mode 0 encodes "
        "symbol IDs 1..3 with codewords 0,10,11 respectively. The edit swaps "
        "the codewords for 2 and 3. Return the bit stream as a binary integer "
        "with leading sentinel 1. Mode 1 decodes bits 0 or 1 into an integer "
        "with the symbol IDs as base-4 digits, or -1 for an unfinished "
        "codeword. Empty encoded and decoded outputs are 1 and 0 respectively.",
        huffman(1, 2), huffman(2, 1), [1, 2, 3],
        [[[0], 1, 1], [[1], 0, 0], [[0, 1, 2, 3], 43, 46],
         [[1, 0, 1, 0, 1, 1], 27, 30], [[1, 1], -1, -1]],
    ),
    item(
        "generic-complex", "Data-directed complex arithmetic with coercion",
        "Input [magnitude,quarter_turns,x,y] with magnitude 0..5, quarter_turns "
        "0..3 and x,y each -5..5. Bind mixed polar/rectangular addition or "
        "edit to multiplication through the homogeneous rectangular method. "
        "Return (real+32)*64 + imaginary+32.",
        complex_operation(1), complex_operation(2), [32, 1, 0],
        [[[2, 1, 3, 4], 2278, 1574], [[3, 2, 1, -2], 1950, 1894],
         [[5, 3, -5, -5], 1750, 505], [[0, 0, 2, 1], 2209, 2080]],
    ),
    item(
        "polynomial-product", "Fixed-support symbolic polynomial product",
        "Input [a,b,c,d], coefficients -3..3 for supports (0,1) and (0,2). "
        "Edit the first support to (0,2). Return coefficients in ascending "
        "degree order (0,1,2,3), or (0,2,4) after the edit, encoded as "
        "base-64 digits offset by 32. The output degree list is part of the "
        "fixed layout. Group terms by equality of static degree sums, and "
        "keep slots for zero coefficients.",
        polynomial(2, (1, 2, 3, 4)), polynomial(3, (1, 3, 5)), [32, 0, 1, 2, 3, 4],
        [[[0, 0, 0, 0], 8521760, 133152], [[1, 2, 3, 1], 9332834, 145890],
         [[2, -2, 3, 3], 10070426, 157722]],
    ),
]
