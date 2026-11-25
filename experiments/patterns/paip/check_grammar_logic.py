"""Independent expected results for the PAIP grammar and logic compilers."""

from itertools import product

from .grammar import PhraseRule, compile_generator, compile_preference_parser
from .logic import (CUT, Clause, DCGRule, Guard, Literal, World, compile_horn,
                    compile_unification_grammar, compile_worlds)


class Checks:
    def __init__(self):
        self.count = 0

    def equal(self, actual, expected):
        if actual != expected:
            raise AssertionError(f"expected {expected!r}, got {actual!r}")
        self.count += 1

    def raises(self, exception, operation):
        try:
            operation()
        except exception:
            self.count += 1
        else:
            raise AssertionError(f"expected {exception.__name__}")


def check_generation(check):
    rules = {
        "S": [("NP", "VP")],
        "NP": [("Det", "Mods", "N")],
        "Det": [("the",), ("a",)],
        "Mods": [(), ("Adj", "Mods")],
        "Adj": [("blue",), ("small",)],
        "N": [("bird",), ("dog",)],
        "VP": [("rests",), ("flies",)],
    }
    grammar = compile_generator(rules, "S")
    check.equal(grammar.run(max_depth=2), ())
    for depth in range(3, 7):
        modifiers = sorted(adjectives for size in range(depth - 2)
                           for adjectives in product(("blue", "small"), repeat=size))
        expected = tuple((det, *adjectives, noun, verb) for det, adjectives, noun, verb
                         in product(("the", "a"), modifiers, ("bird", "dog"), ("rests", "flies")))
        check.equal(grammar.run(max_depth=depth), expected)
        check.equal(grammar.run(max_depth=depth, limit=3), expected[:3])
    check.equal(grammar.run(max_depth=3, trees=True, limit=1), (
        ("S", ("NP", ("Det", "the"), ("Mods",), ("N", "bird")), ("VP", "rests")),
    ))
    picked = grammar.run(max_depth=5, seed=17)
    check.equal(len(picked), 1)
    check.equal(picked, grammar.run(max_depth=5, seed=17))
    check.equal(picked[0] in grammar.run(max_depth=5), True)
    check.equal(compile_generator({"a": [("b",)], "b": [("a",)]}, "a").run(), ())
    check.equal(compile_generator({"a": [()]}, "a").run(), ((),))
    rules["N"][:] = [("otter",)]
    changed = compile_generator(rules, "S")
    check.equal(changed.run(3, limit=1), (("the", "otter", "rests"),))
    check.equal(grammar.run(3, limit=1), (("the", "bird", "rests"),))
    check.equal("otter" in changed.source and "otter" not in grammar.source, True)
    check.raises(ValueError, lambda: grammar.run(-1))
    check.raises(ValueError, lambda: compile_generator(rules, "Missing"))


def check_preferences(check):
    def join(left, _, right):
        return left + right

    def without(left, _, right):
        return tuple(value for value in left if value not in right)

    def subset(left, _, right):
        return 4 if set(right.meaning) <= set(left.meaning) else -3

    def prefer_short(left, _, right):
        return 4 if left.span[1] - left.span[0] == 1 else -3

    def singleton(left, _):
        return left if len(left) == 1 else None

    rules = [
        PhraseRule("Expr", ("Atom",)),
        PhraseRule("Atom", ("one",), lambda _: (1,)),
        PhraseRule("Atom", ("two",), lambda _: (2,)),
        PhraseRule("Expr", ("Expr", "and", "Expr"), join, 1),
        PhraseRule("Expr", ("Expr", "only"), singleton),
    ]
    grammar = compile_preference_parser(
        [*rules, PhraseRule("Expr", ("Expr", "without", "Expr"), without, subset)],
        "Expr",
    )
    phrase = "one and two without one"
    results = grammar.run(phrase)
    check.equal(tuple((p.meaning, p.score, p.span) for p in results),
                (((2,), 5, (0, 5)), ((1, 2), -2, (0, 5))))
    one = ("Expr", ("Atom", "one"))
    two = ("Expr", ("Atom", "two"))
    check.equal(tuple(p.tree for p in results), (
        ("Expr", ("Expr", one, "and", two), "without", one),
        ("Expr", one, "and", ("Expr", two, "without", one)),
    ))
    changed = compile_preference_parser(
        [*rules, PhraseRule("Expr", ("Expr", "without", "Expr"), without, prefer_short)],
        "Expr",
    )
    check.equal(tuple((p.meaning, p.score) for p in changed.run(phrase)),
                (((1, 2), 5), ((2,), -2)))
    check.equal(grammar.run("one and"), ())
    check.equal(grammar.run("one two"), ())
    check.equal(grammar.run(""), ())
    check.equal(tuple(p.meaning for p in grammar.run("one only")), ((1,),))
    check.equal(grammar.run("one and two only")[0].tree,
                ("Expr", one, "and", ("Expr", two, "only")))
    check.equal(grammar.run("one without one only")[0].meaning, ())
    lexicon = [PhraseRule("Name", ("Kim",)), PhraseRule("Verb", ("rests",)),
               PhraseRule("S", ("Name", "Verb"))]
    closed = compile_preference_parser(lexicon, "S")
    opened = compile_preference_parser(lexicon, "S", open_categories=("Name",))
    check.equal(closed.run("Noor rests"), ())
    check.equal(tuple(p.meaning for p in opened.run("Noor rests")),
                (("S", "Noor", "rests"),))
    check.equal(opened.run("rests rests"), ())
    check.raises(ValueError, lambda: compile_preference_parser(
        [PhraseRule("A", ("B",)), PhraseRule("B", ("A",))], "A"))
    overflow = compile_preference_parser(
        [PhraseRule("S", ("A", "A")), PhraseRule("A", ("a",), preference=1.5e308)], "S")
    check.raises(ValueError, lambda: overflow.run("a a"))


def check_horn(check):
    edges = (("a", "b"), ("a", "c"), ("b", "d"), ("c", "e"), ("e", "f"))
    clauses = [
        *(Clause(("edge", *edge)) for edge in edges),
        Clause(("path", "?source", "?target"), (("edge", "?source", "?target"),)),
        Clause(("path", "?source", "?target"),
               (("edge", "?source", "?middle"), ("path", "?middle", "?target"))),
        Clause(("valid", "c")),
        Clause(("valid", "e")),
        Clause(("pair", ("node", "b", "d"))),
        Clause(("pair", ("node", "e", "e"))),
        Clause(("token", Literal("?word"))),
        Clause(("folded", "yes"), (("=", ("node", 1), ("node", 1)),)),
        Clause(("folded", "no"), (("=", ("node", 1), ("node", True)),)),
    ]
    program = compile_horn(clauses)
    reach = (("path", "a", "?target"),)
    check.equal(program.run(reach),
                tuple({"?target": target} for target in ("b", "c", "d", "e", "f")))
    check.equal(program.run(reach, max_depth=2), ({"?target": "b"}, {"?target": "c"}))
    check.equal(program.run(reach, limit=1), ({"?target": "b"},))
    check.equal(program.run(reach, limit=0), ())
    check.equal(program.run((("path", "a", "?x"), ("path", "?x", "?y"), ("valid", "?x"))),
                ({"?x": "c", "?y": "e"}, {"?x": "c", "?y": "f"}, {"?x": "e", "?y": "f"}))
    check.equal(program.run((("pair", ("node", "?x", "?x")),)), ({"?x": "e"},))
    check.equal(program.run((("pair", ("node", "?", "?_")),)), ({}, {}))
    check.equal(program.run((("token", Literal("?word")),)), ({},))
    check.equal(program.run((("token", "?name"),)), ({"?name": "?word"},))
    check.equal(program.run((("=", "?x", ("box", "?x")),)), ())
    check.equal(program.run((("=", "?x", ("box", "?y")), ("=", "?y", "?x"))), ())
    check.equal(program.run((("=", "?x", "?y"),)), ({"?x": "?x", "?y": "?x"},))
    cyclic = []
    cyclic.append((cyclic,))
    check.raises(ValueError, lambda: program.run((("=", "?x", cyclic),)))
    check.equal(program.run((("absent", "a", "b"),)), ())
    check.equal(program.run((("edge", "a"),)), ())
    check.equal(program.run(()), ({},))
    check.equal(program.run((("folded", "?value"),)), ({"?value": "yes"},))
    check.equal("_equality(" in program.source, False)
    changed = compile_horn([*clauses, Clause(("edge", "d", "g"))])
    check.equal(changed.run(reach),
                tuple({"?target": target} for target in ("b", "c", "d", "g", "e", "f")))
    check.equal(program.run(reach)[-1], {"?target": "f"})
    check.raises(ValueError, lambda: compile_horn((Clause(CUT),)))
    check.raises(ValueError, lambda: program.run((("!", 1),)))


def check_cut(check):
    choose = ("choice", "?item")
    detail = ("detail", "?item", "?value")
    false = ("=", "x", "y")
    clauses = (
        Clause(("choice", "a")), Clause(("choice", "b")),
        Clause(("detail", "a", "x")), Clause(("detail", "a", "y")),
        Clause(("detail", "b", "z")), Clause(("allowed", "y")), Clause(("allowed", "z")),
        Clause(("pick", "?item", "?value"), (choose, CUT, detail)),
        Clause(("pick", "fallback", "fallback")),
        Clause(("outer", "?caller", "?value"),
               (("choice", "?caller"), ("pick", "?", "?value"))),
        Clause(("chooseAllowed", "?item", "?value"), (detail, CUT, ("allowed", "?value"))),
        Clause(("retryCaller", "?item", "?value"), (choose, ("chooseAllowed", "?item", "?value"))),
        Clause(("committed", "?item"), (choose, CUT, false)),
        Clause(("committed", "fallback")),
        Clause(("uncommitted", "?item"), (choose, false, CUT)),
        Clause(("uncommitted", "fallback")),
        Clause(("twice", "?item", "?value"),
               (choose, CUT, detail, CUT, ("allowed", "?value"))),
        Clause(("twice", "fallback", "fallback")),
    )
    program = compile_horn(clauses)
    choices = ({"?item": "a", "?value": "x"}, {"?item": "a", "?value": "y"})
    check.equal(program.run((("pick", "?item", "?value"),)), choices)
    check.equal(program.run((choose, CUT, detail)), choices)
    check.equal(program.run((choose, CUT, detail, CUT)), choices[:1])
    check.equal(program.run((CUT, choose)), ({"?item": "a"}, {"?item": "b"}))
    check.equal(program.run((choose, CUT, ("=", "?item", "b"))), ())
    check.equal(program.run((("outer", "?caller", "?value"),)),
                tuple({"?caller": caller, "?value": value}
                      for caller, value in product(("a", "b"), ("x", "y"))))
    check.equal(program.run((("retryCaller", "?item", "?value"),)),
                ({"?item": "b", "?value": "z"},))
    check.equal(program.run((("committed", "?item"),)), ())
    check.equal(program.run((("uncommitted", "?item"),)), ({"?item": "fallback"},))
    check.equal(program.run((("twice", "?item", "?value"),)), ())
    changed = compile_horn(clause for clause in clauses if clause.head != ("detail", "a", "x"))
    check.equal(changed.run((("twice", "?item", "?value"),)),
                ({"?item": "a", "?value": "y"},))


def check_deductive_grammar(check):
    rules = [
        DCGRule(("S", "?meaning"), (
            ("Q", "?outer", "?subject"), ("V", "?verb"), ("Q", "?inner", "?object"),
            Guard(("scope", "?outer", "?subject", "?inner", "?object", "?verb", "?meaning")),
        )),
        DCGRule(("Q", "every", "bird"), ("every", "bird")),
        DCGRule(("Q", "some", "worm"), ("some", "worm")),
        DCGRule(("V", "chases"), ("chases",)),
    ]
    common = ("scope", "every", "?s", "some", "?o", "?verb")
    surface = (*common, ("all", "?s", ("exists", "?o", ("?verb", "?s", "?o"))))
    inverse = (*common, ("exists", "?o", ("all", "?s", ("?verb", "?s", "?o"))))
    grammar = compile_unification_grammar(rules, "S", facts=(Clause(surface), Clause(inverse)))
    sentence = "every bird chases some worm"
    expected = (
        {"?meaning": ("all", "bird", ("exists", "worm", ("chases", "bird", "worm")))},
        {"?meaning": ("exists", "worm", ("all", "bird", ("chases", "bird", "worm")))},
    )
    check.equal(grammar.run(sentence, "?meaning"), expected)
    check.equal(grammar.run(sentence, "?meaning", limit=1), expected[:1])
    check.equal(grammar.run(sentence, expected[1]["?meaning"]), ({},))
    check.equal(grammar.run(sentence + " extra", "?meaning"), ())
    check.equal(grammar.run("every bird chases", "?meaning"), ())
    check.equal(grammar.run(("every", "bird", "chases") + ("some",) * 1400, "?meaning"), ())
    check.equal(compile_unification_grammar(
        rules, "S", facts=(Clause(surface),)
    ).run(sentence, "?meaning"), expected[:1])
    check.raises(ValueError, lambda: grammar.run(sentence))


def check_english_features(check):
    singular, plural = ("agr", 3, "sg"), ("agr", 3, "pl")
    subjective, objective = ("case", "nom"), ("case", "obj")
    nom, gap = ("NP", "?agr", subjective, "?subject"), ("gap", objective, "?object")
    event = ("event", "?verb", "?subject", "?object")
    rules = [
        DCGRule(("S", "decl", "?agr", event),
                (nom, ("VP", "?agr", "finite", "?verb", "?object", (), ()))),
        DCGRule(("S", "wh", "?agr", ("ask", "?object", event)), (
            ("WH", objective, "?object"), ("Aux", "?agr"), nom,
            ("VP", "?agr", "bare", "?verb", "?object", gap, ()),
        )),
        DCGRule(("VP", "?agr", "?form", "?verb", "?object", "?inputGap", "?outputGap"), (
            ("V", "?agr", "?form", "?verb"), Guard(("frame", "?verb", "transitive")),
            ("Object", "?inputGap", "?outputGap", "?object"),
        )),
        DCGRule(("Object", (), (), "?object"), (("NP", "?", objective, "?object"),)),
        DCGRule(("Object", gap, (), "?object")),
        DCGRule(("NP", singular, subjective, "kim"), ("Kim",)),
        DCGRule(("NP", plural, subjective, "they"), ("they",)),
        DCGRule(("NP", plural, objective, "they"), ("them",)),
        DCGRule(("WH", objective, "who"), ("whom",)),
        DCGRule(("V", singular, "finite", "notice"), ("notices",)),
        DCGRule(("V", plural, "finite", "notice"), ("notice",)),
        DCGRule(("V", "?", "bare", "notice"), ("notice",)),
        DCGRule(("Aux", singular), ("does",)),
        DCGRule(("Aux", plural), ("do",)),
    ]
    frame = (Clause(("frame", "notice", "transitive")),)
    grammar = compile_unification_grammar(rules, "S", facts=frame)
    check.equal(grammar.run("Kim notices them", "decl", "?agr", "?sem"),
                ({"?agr": singular, "?sem": ("event", "notice", "kim", "they")},))
    check.equal(grammar.run("they notice them", "decl", "?agr", "?sem"),
                ({"?agr": plural, "?sem": ("event", "notice", "they", "they")},))
    check.equal(grammar.run("whom does Kim notice", "wh", "?agr", "?sem"),
                ({"?agr": singular, "?sem": ("ask", "who", ("event", "notice", "kim", "who"))},))
    for sentence in ("Kim notice them", "they notices them", "them notice Kim",
                     "Kim notices they", "whom do Kim notice", "whom does Kim notice them"):
        check.equal(grammar.run(sentence, "?mood", "?agr", "?sem"), ())
    changed_rules = [*rules, DCGRule(("NP", singular, subjective, "noor"), ("Noor",))]
    changed = compile_unification_grammar(changed_rules, "S", facts=frame)
    check.equal(changed.run("Noor notices them", "decl", singular, "?sem"),
                ({"?sem": ("event", "notice", "noor", "they")},))
    check.equal(grammar.run("Noor notices them", "decl", singular, "?sem"), ())
    check.equal(compile_unification_grammar(rules, "S").run(
        "Kim notices them", "decl", singular, "?sem"), ())
    quoted = compile_unification_grammar((DCGRule(("S",), ("?word",)),), "S")
    check.equal(quoted.run(("?word",)), ({},))
    check.equal(quoted.run(("bird",)), ())


def check_indexed_worlds(check):
    calls = []

    def conversion(fact):
        calls.append(fact)
        _, name, meters = fact
        return (("length_cm", name, meters * 100),)

    worlds = {
        "base": World(facts=(
            ("sub", "sparrow", "bird"), ("sub", "bird", "animal"),
            ("val", "studies", "Nell", "radio"),
            ("ind", "Juniper", "sparrow"),
            ("length_m", "river", 12),
            ("reading", ("pair", "z", "z"), 7),
            ("reading", ("pair", "z", "x"), 8),
            ("reading", ("box", "z"), 9),
            ("reading", ("box", True), 10),
            ("reading", ("box", 1), 11),
            ("reading", ("box", Literal("?raw")), 12),
        )),
        "lab": World(("base",), (("rel", "studies", "student", "tool"),)),
        "field": World(("base",), (("sub", "tool", "artifact"),
                                   ("~val", "studies", "Nell", "map"))),
        "merged": World(("lab", "field"), (("sub", "student", "person"),)),
    }
    knowledge = compile_worlds(worlds, attachments={"length_m": conversion})
    build_calls = tuple(calls)
    check.equal(bool(build_calls), True)
    check.equal(knowledge.run("base", (("ind", "Juniper", "?category"),)),
                ({"?category": "sparrow"}, {"?category": "bird"}, {"?category": "animal"}))
    check.equal(knowledge.run("base", (("ind", "Nell", "student"),)), ())
    check.equal(knowledge.run("lab", (("ind", "Nell", "student"),)), ({},))
    check.equal(knowledge.run("field", (("ind", "radio", "artifact"),)), ())
    check.equal(knowledge.run("merged", (("ind", "radio", "artifact"),)), ({},))
    check.equal(knowledge.run("merged", (("ind", "Nell", "person"),)), ({},))
    check.equal(knowledge.run("lab", (("~val", "studies", "Nell", "map"),)), ())
    check.equal(knowledge.run("field", (("~val", "studies", "Nell", "map"),)), ({},))
    check.equal(knowledge.run("field", (("val", "studies", "Nell", "map"),)), ())
    check.equal(knowledge.run("merged", (("length_cm", "river", "?length"),)), ({"?length": 1200},))
    check.equal(knowledge.run("merged", (
        ("val", "studies", "Nell", "?item"), ("ind", "?item", "artifact")
    )), ({"?item": "radio"},))
    check.equal(knowledge.run("base", (("reading", ("pair", "?x", "?x"), "?v"),)),
                ({"?x": "z", "?v": 7},))
    check.equal(knowledge.run("base", (("reading", ("box", 1), "?v"),)), ({"?v": 11},))
    check.equal(knowledge.run("base", (("reading", ("box", True), "?v"),)), ({"?v": 10},))
    check.equal(knowledge.run("base", (("reading", ("box", Literal("?raw")), "?v"),)),
                ({"?v": 12},))
    check.equal(tuple(calls), build_calls)
    modified = compile_worlds(worlds, attachments={
        "length_m": lambda fact: (("length_cm", fact[1], fact[2] * 1000),),
    })
    check.equal(modified.run("base", (("length_cm", "river", "?length"),)), ({"?length": 12000},))
    check.equal(knowledge.run("base", (("length_cm", "river", "?length"),)), ({"?length": 1200},))
    check.raises(ValueError, lambda: compile_worlds({"a": World(("b",)), "b": World(("a",))}))
    check.raises(ValueError, lambda: compile_worlds({"a": World(("b",))}))
    check.raises(ValueError, lambda: compile_worlds({"a": World(facts=(("p", "?x"),))}))
    check.raises(ValueError, lambda: compile_worlds(
        {"a": World(facts=(("count", 0),))}, max_facts=4,
        attachments={"count": lambda fact: (("count", fact[1] + 1),)},
    ))
    check.raises(KeyError, lambda: knowledge.run("missing", ()))


def check_grammar_logic():
    check = Checks()
    check_generation(check)
    check_preferences(check)
    check_horn(check)
    check_cut(check)
    check_deductive_grammar(check)
    check_english_features(check)
    check_indexed_worlds(check)
    return check.count


if __name__ == "__main__":
    print(f"PAIP grammar and logic: {check_grammar_logic()} checks passed")
