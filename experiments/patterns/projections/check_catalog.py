"""Cross-check projection fixtures against independent existing catalog APIs."""

from fractions import Fraction
from functools import reduce
from itertools import count, product
from math import floor
from operator import add, mul
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from ..gof.build import CFLAGS, compile_library
from ..paip.constraints import specialize_constraints
from ..paip.grammar import PhraseRule, compile_generator, compile_preference_parser
from ..paip.logic import Clause, DCGRule, World, compile_horn, compile_unification_grammar, compile_worlds
from ..paip.search import specialize_planner, specialize_search
from ..sicp import data, procedures, state, streams
from . import catalog_behavior, catalog_game, catalog_languages, catalog_objects, catalog_state
from .catalog_paip import CORE_ORACLES
from .check import catalog, check, require, run


def pack(values, radix, initial=0):
    return reduce(lambda result, value: radix * result + value, values, initial)


def check_go_f(cases):
    chain = cases["gof-chain-of-responsibility"]
    iterator = cases["gof-iterator"]
    observer = cases["gof-observer"]
    template = cases["gof-template-method"]
    first_limit, first_value, second_limit, second_value, fallback = chain["config"]
    stride, offset = iterator["config"]
    shared_tag, other_tag = observer["config"]
    prepare, minimum, operand = template["config"]
    handlers = [[first_limit, first_value], [second_limit, second_value]]
    subscribers = {
        "a": {"notify": "notice_a", "tags": [shared_tag]},
        "b": {"notify": "notice_b", "tags": [shared_tag, other_tag]},
    }
    profile = {}
    for suffix, reverse in (("original", False), ("edited", True)):
        profile[f"chain_{suffix}"] = {
            "pattern": "chain_of_responsibility",
            "handlers": list(reversed(handlers)) if reverse else handlers,
            "default": fallback,
        }
        profile[f"iterator_{suffix}"] = {
            "pattern": "iterator", "stride": stride, "offset": offset + reverse,
        }
        profile[f"observer_{suffix}"] = {
            "pattern": "observer",
            "subscribers": dict(reversed(subscribers.items())) if reverse else subscribers,
        }
        profile[f"template_{suffix}"] = {
            "pattern": "template_method", "prepare": "prepare_input",
            "compute": "compute_add" if reverse else "compute_times",
            "finish": "finish_output", "minimum": minimum,
        }

    client = f"""
#include "gof.h"
#include <inttypes.h>
#include <stdio.h>

typedef struct {{
    unsigned trace;
    unsigned admitted;
    uint32_t finish_value;
}} Context;

typedef struct {{
    unsigned trace;
    bool detach_b;
    void *registry;
    void (*unsubscribe_b)(void *);
}} ObserverContext;

void notice_a(void *context, uint32_t tag, uint32_t value) {{
    ObserverContext *c = context;
    (void)tag;
    c->trace = c->trace * 100 + 10 + value;
    if (c->detach_b) c->unsubscribe_b(c->registry);
}}

void notice_b(void *context, uint32_t tag, uint32_t value) {{
    ObserverContext *c = context;
    (void)tag;
    c->trace = c->trace * 100 + 20 + value;
}}

static void unsubscribe_original(void *registry) {{
    gof_observer_original_subscribe_b(registry, false);
}}

static void unsubscribe_edited(void *registry) {{
    gof_observer_edited_subscribe_b(registry, false);
}}

uint32_t prepare_input(void *context, uint32_t input) {{
    ((Context *)context)->trace = 1;
    return input + {prepare}u;
}}

uint32_t compute_times(void *context, uint32_t value) {{
    Context *c = context;
    c->trace = c->trace * 10 + 2;
    return value * {operand}u;
}}

uint32_t compute_add(void *context, uint32_t value) {{
    Context *c = context;
    c->trace = c->trace * 10 + 2;
    return value + {operand}u;
}}

void finish_output(void *context, bool admitted, uint32_t value) {{
    Context *c = context;
    c->trace = c->trace * 10 + 3;
    c->admitted = admitted;
    c->finish_value = value;
}}

int main(void) {{
"""
    expected = []

    def observe(case, body):
        nonlocal client
        for args, original, edit in case["samples"]:
            for suffix, wanted in (("original", original), ("edited", edit)):
                client += "    {\n" + body(args, suffix) + "    }\n"
                expected.append((case["id"], suffix, args, wanted))

    def chain_client(args, suffix):
        values = ", ".join(map(str, args or [0]))
        return (f"        const uint32_t args[] = {{{values}}};\n"
                f'        printf("%" PRId64 "\\n", gof_chain_{suffix}(args, {len(args)}));\n')

    def iterator_client(args, suffix):
        start, *records = args
        values = ", ".join(map(str, records or [0]))
        return f"""
        const uint32_t args[] = {{{values}}};
        gof_iterator_{suffix}_cursor first = gof_iterator_{suffix}_begin(args, {len(records)});
        gof_iterator_{suffix}_cursor second = gof_iterator_{suffix}_begin(args, {len(records)});
        uint32_t value;
        unsigned long long left = 0, right = 0;
        for (size_t i = 0; i != {start}; ++i) (void)gof_iterator_{suffix}_next(&second, &value);
        while (gof_iterator_{suffix}_next(&first, &value)) left = left * 10 + value;
        while (gof_iterator_{suffix}_next(&second, &value)) right = right * 10 + value;
        printf("%llu\\n", left * 10000 + right);
"""

    def template_client(args, suffix):
        initial, previous = args
        return f"""
        Context context = {{0}};
        uint32_t output = {previous};
        bool admitted = gof_template_{suffix}(&context, {initial}, &output);
        if (admitted != context.admitted) return 1;
        unsigned long long packed = ((context.trace * 100 + context.admitted) * 100
                                     + context.finish_value) * 100 + output;
        printf("%llu\\n", packed);
"""

    def observer_client(args, suffix):
        active, tag, detach, payload = args
        return f"""
        gof_observer_{suffix}_registry registry = {{0}};
        gof_observer_{suffix}_subscribe_a(&registry, {active & 1});
        gof_observer_{suffix}_subscribe_b(&registry, {active & 2});
        ObserverContext context = {{0, {detach}, &registry, unsubscribe_{suffix}}};
        gof_observer_{suffix}_notify(&registry, &context, {tag}, {payload});
        unsigned first = context.trace;
        context.trace = 0;
        gof_observer_{suffix}_notify(&registry, &context, {tag}, {payload});
        printf("%u\\n", first * 10000 + context.trace);
"""

    observe(chain, chain_client)
    observe(iterator, iterator_client)
    observe(observer, observer_client)
    observe(template, template_client)
    with TemporaryDirectory(prefix="projection-catalog-gof-") as directory:
        work = Path(directory)
        release = work / "library"
        compile_library(profile, release)
        source = work / "client.c"
        source.write_text(client + "    return 0;\n}\n")
        executable = work / "client"
        run([os.environ.get("CLANG", "clang"), *CFLAGS, "-I", release,
             source, release / "libgof.a", "-o", executable])
        observed = list(map(int, run([executable]).splitlines()))
    require(len(observed) == len(expected), "GoF catalog client returned the wrong number of outputs")
    for (slug, version, args, wanted), actual in zip(expected, observed, strict=True):
        compare(slug, version, args, wanted, actual)
    return len(expected)


def account(args, config, changed):
    initial, fee = config
    account = state.specialize_account(0 if changed else fee)(initial)
    for request in args:
        if request < 0:
            account.withdraw(-request)
        else:
            account.deposit(request)
    return account.balance()


def transfer(args, config, changed):
    initial, reserve, target_initial = config
    make = state.specialize_account()
    source, target = make(initial), make(target_initial)
    execute = state.bind_transfer(source, target)
    for amount in args:
        if changed or source.balance() - amount >= reserve:
            execute(amount)
    return pack((source.balance(), target.balance()), 10000)


def aliased_table(args, config, changed):
    table = state.specialize_table({0: 0, 1: 1, 2: 0, 3: 1})(config[0], config[0])
    for key, value in zip(args[::2], args[1::2], strict=True):
        table[key].write(value)
    return table[int(changed)].read()


def memoized(args, config, changed):
    slots = {}

    def compute(recur, key):
        result = recur(key - 1) + config[2 if changed else 1] if key else config[0]
        slots[key] = result
        return result

    evaluate = state.specialize_memoized(compute)()
    for key in args:
        evaluate(key)
    return pack((slots[key] + 1 if key in slots else 0 for key in reversed(range(4))),
                10, initial=len(slots))


def pipeline(args, config, changed):
    demand, *values = args
    if demand == 0:
        return 0
    forces = 0

    def source():
        nonlocal forces
        for value in values:
            forces += 1
            yield value

    transform = streams.specialize_pipeline((
        ("filter", lambda value: value >= config[0]),
        ("map", lambda value: value + (1 if changed else 2)),
    ))
    return pack((sum(streams.take(transform(streams.from_iterable(source())), demand)), forces), 100)


def pairs(args, config, changed):
    stride = config[int(changed)]
    enumerate_pairs = streams.specialize_pairs(lambda left, right: 100 * left + left + stride * (right - left))
    return streams.take(enumerate_pairs(streams.from_iterable(count()),
                                        streams.from_iterable(count())), args[0] + 1)[-1]


def feedback(args, config, changed):
    forces = 0

    def derivative(x):
        nonlocal forces
        forces += 1
        return (2 if changed else 1) * x

    solution = streams.specialize_feedback(derivative, 1)(config[0])
    highest = 0
    last = solution.head
    for index in args:
        value = streams.take(solution, index + 1)[-1]
        if index > highest:
            highest, last = index, value
    return pack((last, highest, forces), 100)


def recurrence(args, config, changed):
    index, first, second = args
    recent, previous, constant = (config[i] for i in ((0, 3, 4) if changed else (0, 1, 2)))
    return procedures.compile_recurrence((recent, previous), constant)(index, (first, second))


def power(args, config, changed):
    modulus = config[0]
    evaluate = procedures.compile_power(config[2 if changed else 1], lambda x, y: x * y % modulus)
    return evaluate(args[0] % modulus)


def fixed_gcd(args, config, changed):
    return procedures.compile_fixed_gcd(config[int(changed)])(args[0])


def primality(args, config, changed):
    candidate = args[0]
    is_prime = procedures.compile_primality(100)
    return int(is_prime(candidate) and (not changed or is_prime(candidate + config[0])))


def half_interval(args, config, changed):
    target = config[int(changed)]
    solve = procedures.compile_half_interval(lambda value: value * value - target)
    try:
        return floor(solve(*args))
    except ValueError as error:
        if str(error) != "bracket endpoints must have opposing signs":
            raise
        return -1


def rational(args, config, changed):
    common = config[1]
    offset = Fraction(config[3 if changed else 2], common)
    result = data.compile_rational_add(common // config[0], offset)(args[0])
    return pack((result.numerator + 128, result.denominator), 16)


def intervals(args, config, changed):
    interval = data.Interval(*config[2:4] if changed else config[:2])
    product = data.compile_interval_product(interval)(data.Interval(*args))
    return pack((product.lower + 32, product.upper + 32), 64)


def tree(args, config, changed):
    topology = (0, (1, 2)) if changed else ((0, 1), 2)
    return data.compile_tree_fold(topology, lambda left, right: config[0] * left + right)(args)


def painter(args, config, changed):
    x, y, ex, ey, fx, fy, segment, endpoint = args
    scale_origin, scale_edge, reflect_origin, reflect_edge, *orders = config
    scale = data.Frame((Fraction(scale_origin, 2), 0), (Fraction(scale_edge, 2), 0), (0, 1))
    flip = data.Frame((reflect_origin, 0), (reflect_edge, 0), (0, 1))
    outer, inner = (flip, scale) if orders[int(changed)] else (scale, flip)
    diagonal = data.Segments((((0, 0), (1, 1)),))
    border = data.Segments((((0, 0), (1, 0)),))
    paint = data.compile_painter(data.Overlay((border, data.Transform(outer, data.Transform(inner, diagonal)))))
    point = paint(data.Frame((x, y), (ex, ey), (fx, fy)))[segment][endpoint]
    return pack((int(2 * coord) + 32 for coord in point), 64)


def derivative(args, config, changed):
    exponent, offset, *_ = config
    expr = ("*", ("+", ("**", "x", exponent), "y"), ("+", "x", offset))
    return data.compile_derivative(expr, "y" if changed else "x", ("x", "y"))(*args)


def ordered_set(args, config, changed):
    members = (*config[:3], config[4 if changed else 3], *config[5:])
    return int(data.compile_set_membership(members, representation="ordered")(args[0]))


def huffman(args, config, changed):
    mode, *message = args
    first, second, third = map(str, config)
    codec = data.compile_huffman((first, (third, second) if changed else (second, third)))
    if mode == 0:
        return pack(codec.encode(map(str, message)), 2, initial=1)
    try:
        return pack(map(int, codec.decode(message)), 4)
    except ValueError as error:
        if str(error) != "incomplete Huffman codeword":
            raise
        return -1


def generic_complex(args, config, changed):
    magnitude, turns, real, imaginary = args
    operation = "multiply" if changed else "add"
    tower = data.ArithmeticTower(
        ("polar", "rectangular"),
        {("add", "rectangular", "rectangular"): add, ("multiply", "rectangular", "rectangular"): mul},
        (lambda polar: polar[0] * 1j ** polar[1],),
    )
    result = tower.bind_binary(operation, "polar", "rectangular")((magnitude, turns), complex(real, imaginary))
    bias = config[0]
    return pack((int(result.real) + bias, int(result.imag) + bias), 64)


def polynomial(args, config, changed):
    _, product = data.compile_polynomial_product((0, 2) if changed else (0, 1), (0, 2))
    return pack((coefficient + config[0] for coefficient in product(args[:2], args[2:])), 64)


def horn(args, config, changed):
    mask, source, target = args
    size = config[0]
    clauses = [Clause(("edge", a, b)) for a in range(size) for b in range(size)
               if mask & (1 << (a * size + b))]
    clauses.append(Clause(("path", "?x", "?z"), (("edge", "?x", "?z"),)))
    intermediate = tuple(node for node in range(size) if node != 1) if changed else ("?y",)
    clauses.extend(Clause(("path", "?x", "?z"), (("edge", "?x", via), ("edge", via, "?z")))
                   for via in intermediate)
    query = compile_horn(clauses)
    return int(bool(query.run([("path", source, target)], limit=1)))


def worlds(args, config, changed):
    mask, target = args
    parents = {0: (), 1: ("0",), 2: ("0",) if changed else ("1",)}
    query = compile_worlds({str(i): World(parents[i], (("present", i),) if mask & (1 << i) else ())
                            for i in range(3)})
    return int(bool(query.run(str(target), [("present", "?id")], limit=1)))


def generation(args, config, changed):
    terminals = tuple((str(index),) for index in range(config[0]))
    recursive = (("S",), ("S",)) if changed else (("S", "S"),)
    generator = compile_generator({"S": terminals + recursive}, "S")
    return len(generator.run(args[0] + 1, trees=True))


def preferences(args, config, changed):
    noun, conjunction = map(str, config[:2])
    sign = 1 if changed else -1
    parser = compile_preference_parser((
        PhraseRule("S", ("N",)), PhraseRule("S", ("N", conjunction, "S")),
        *(PhraseRule("N", (noun,), preference=sign * cost) for cost in config[2:]),
    ), "S")
    results = parser.run(tuple(map(str, args)))
    return pack((sign * results[0].score, len(results)), 100) if results else 0


def agreement(args, config, changed):
    determiner, pronoun = map(str, config)
    rules = (
        DCGRule(("sentence",), (determiner, ("noun", "?number"), ("verb", "?number"), pronoun)),
        *(DCGRule(("noun", feature), (str(10 + feature),)) for feature in range(2)),
        *(DCGRule(("verb", 1 - feature if changed else feature), (str(20 + feature),))
          for feature in range(2)),
    )
    parser = compile_unification_grammar(rules, "sentence")
    return int(bool(parser.run(tuple(map(str, args)), limit=1)))


def graph_search(args, config, changed):
    mask, start, goal = args
    size = config[0]
    graph = {str(a): [(str(b), 1) for b in range(size) if mask & (1 << (a * size + b))
                      and (not changed or (a, b) != (1, 2))]
             for a in range(size)}
    search = specialize_search(graph, "breadth_first")
    return int(search.run(str(start), str(goal)) is not None)


def planning(args, config, changed):
    operators = [("unlock", ("key",), ("open",), ())]
    if not changed:
        operators.insert(0, ("pickup", (), ("key",), ()))
    planner = specialize_planner(operators, method="graph")
    initial = [name for index, name in enumerate(("key", "open")) if args[0] & (1 << index)]
    plan = planner.run(initial, ("open",))
    return len(plan) if plan is not None else 9


def coloring(args, config, changed):
    colors = (0, 1, 2)
    unlike = tuple((a, b) for a, b in product(colors, repeat=2) if a != b)
    edges = (("x", "y"), ("y", "z")) if changed else (("x", "y"), ("y", "z"), ("x", "z"))
    solve = specialize_constraints(dict.fromkeys("xyz", colors), ((*edge, unlike) for edge in edges))
    return len(solve.run({"x": (args[0],)}).solutions)


def waltz(args, config, changed):
    def junction(parity):
        return tuple(labels for labels in product((0, 1), repeat=3) if sum(labels) % 2 == parity)

    left, right = junction(0), junction(int(changed))
    shared_edge = ((a, b) for a, b in product(left, right) if a[0] == b[0])
    solve = specialize_constraints({"left": left, "right": right}, (("left", "right", shared_edge),))

    def restrict(labels, observations):
        return tuple(choice for choice in labels
                     if all(observed == -1 or observed == actual
                            for observed, actual in zip(observations, choice[1:], strict=True)))

    return len(solve.run({"left": restrict(left, args[:2]), "right": restrict(right, args[2:])}).solutions)


GOF_IDS = ("gof-chain-of-responsibility", "gof-iterator", "gof-observer", "gof-template-method")
ORACLES = {
    "sicp-account": account,
    "sicp-transfer": transfer,
    "sicp-table": aliased_table,
    "sicp-memoized": memoized,
    "sicp-pipeline": pipeline,
    "sicp-pairs": pairs,
    "sicp-feedback": feedback,
    "sicp-recurrence": recurrence,
    "sicp-modular-power": power,
    "sicp-fixed-gcd": fixed_gcd,
    "sicp-primality": primality,
    "sicp-half-interval": half_interval,
    "sicp-rational-offset": rational,
    "sicp-interval-product": intervals,
    "sicp-tree-fold": tree,
    "sicp-affine-painter": painter,
    "sicp-symbolic-derivative": derivative,
    "sicp-ordered-set": ordered_set,
    "sicp-huffman-codec": huffman,
    "sicp-generic-complex": generic_complex,
    "sicp-polynomial-product": polynomial,
    "paip-horn-clause-compilation": horn,
    "paip-indexed-reasoning-in-worlds": worlds,
    "paip-recursive-phrase-generation": generation,
    "paip-ambiguous-parsing-with-preferences": preferences,
    "paip-deductive-grammars-and-english-feature-profiles": agreement,
    "paip-ordered-graph-and-heuristic-search": graph_search,
    "paip-means-ends-and-forward-state-space-planning": planning,
    "paip-constraint-propagation-and-complete-bounded-search": coloring,
    "paip-waltz-line-labeling": waltz,
    **CORE_ORACLES,
}


def compare(slug, version, args, wanted, observed):
    require(observed == wanted,
            f"{slug}/{version}: catalog oracle {args} returned {observed}, fixture expects {wanted}")


def run_catalog(cases):
    total = check_go_f(cases)
    for slug, oracle in ORACLES.items():
        case = cases[slug]
        for args, original, edited in case["samples"]:
            compare(slug, "original", args, original, oracle(args, case["config"], False))
            compare(slug, "edited", args, edited, oracle(args, case["config"], True))
            total += 2
    return total


def main():
    cases = {case["id"]: case for case in catalog()}
    selected = {slug: cases[slug] for slug in (*GOF_IDS, *ORACLES, *catalog_languages.CHECKERS)}
    observations = run_catalog(selected)
    observations += sum(catalog_languages.check(selected).values())
    object_ids = {case["id"] for case in catalog_objects.CASES}
    object_observations = catalog_objects.check_objects()
    behavior_observations = catalog_behavior.check(cases)
    state_counts = catalog_state.check(cases.values())
    game_observations = catalog_game.check(cases)
    require(all(n > 0 for n in [*state_counts.values(), game_observations]),
            "one or more catalog families had no reference observations")
    covered = selected.keys() | object_ids | set(catalog_behavior.IDS) | state_counts.keys() | {"paip-adversarial-search"}
    require(covered == cases.keys(), f"reference coverage mismatch: {sorted(covered ^ cases.keys())}")
    check(list(cases.values()))
    total = observations + behavior_observations + object_observations + sum(state_counts.values()) + game_observations
    print(f"Original catalog APIs matched {total} observations across {len(covered)} families.")
    print(f"{observations} paired-profile observations, {object_observations} original GoF object observations, "
          f"{behavior_observations} additional GoF behavior observations, "
          f"{sum(state_counts.values())} additional SICP observations, {game_observations} game observations.")


if __name__ == "__main__":
    main()
