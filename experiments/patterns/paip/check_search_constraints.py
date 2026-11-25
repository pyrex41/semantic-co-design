"""Independent replays for the PAIP search, planning, labeling, and game residuals."""

from itertools import product
from random import Random

from .constraints import ConstraintResult, specialize_constraints, specialize_waltz
from .search import GameDecision, SearchPath, specialize_game, specialize_planner, specialize_search


def check_graph_search():
    graph = {
        "s": (("a", 10), ("b", 1)),
        "a": (("x", 1),),
        "x": (("s", 0), ("g", 1)),
        "b": (("g", 2),),
        "isolated": (),
    }
    estimates = {"b": 1}
    long_path = SearchPath(("s", "a", "x", "g"), 12)
    short_path = SearchPath(("s", "b", "g"), 3)
    expectations = {
        "breadth_first": short_path, "depth_first": long_path,
        "best_first": long_path, "beam": short_path, "a_star": short_path,
    }
    sources = {}
    for policy, expected in expectations.items():
        search = specialize_search(graph, policy, heuristic=estimates, beam_width=2)
        assert search.run("s", "g") == expected, policy
        assert search.run("a", "a") == SearchPath(("a",), 0)
        assert search.run("g", "s") is None
        assert search.run("s", "isolated") is None
        sources[policy] = search.source
    assert len(set(sources.values())) == len(expectations)
    assert "popleft" in sources["breadth_first"] and "popleft" not in sources["depth_first"]
    assert "best_costs" in sources["a_star"] and "best_costs" not in sources["best_first"]

    new_estimate = specialize_search(graph, "best_first", heuristic={"a": 1})
    assert new_estimate.run("s", "g") == short_path
    assert new_estimate.source != sources["best_first"]
    changed_graph = dict(graph, s=(("b", 1), ("a", 10)))
    changed = specialize_search(changed_graph, "depth_first")
    assert changed.run("s", "g") == short_path
    assert changed.source != sources["depth_first"]

    # The first visit to a is through a more expensive path. An admissible but
    # inconsistent h causes that visit before the optimal predecessor.
    reopen = {"s": (("a", 3), ("b", 1)), "a": (("g", 5),), "b": (("a", 1),)}
    search = specialize_search(reopen, "a_star", heuristic={"b": 6})
    assert search.run("s", "g") == SearchPath(("s", "b", "a", "g"), 7)

    trap = {"s": (("dead", 1), ("live", 2)), "dead": (("s", 0),), "live": (("g", 1),)}
    narrow = specialize_search(trap, "beam")
    wide = specialize_search(trap, "beam", beam_width=2)
    assert narrow.run("s", "g") is None
    assert wide.run("s", "g") == SearchPath(("s", "live", "g"), 3)
    assert narrow.source != wide.source

    # Independently exhaust simple paths for small weighted graphs. Parallel
    # and cyclic frontier paths do not necessarily share a predecessor.
    rng = Random(321)
    nodes = ("a", "b", "c", "d", "e")
    for _ in range(24):
        graph = {
            node: tuple((other, rng.randrange(8)) for other in nodes
                        if other != node and rng.randrange(3) == 0)
            for node in nodes
        }
        search = specialize_search(graph, "a_star")
        for start in nodes:
            for goal in nodes:
                distances = _simple_path_costs(graph, start, goal, frozenset())
                result = search.run(start, goal)
                if not distances:
                    assert result is None
                else:
                    assert result.cost == min(distances), (graph, start, goal, result)
                    assert result.states[0] == start and result.states[-1] == goal
                    assert result.cost == sum(dict(graph[a])[b]
                                              for a, b in zip(result.states, result.states[1:]))


def _simple_path_costs(graph, node, goal, seen):
    if node == goal:
        return (0,)
    return tuple(cost + remaining for other, cost in graph[node]
                 if other not in seen
                 for remaining in _simple_path_costs(graph, other, goal, seen | {node}))


def _replay_plan(operators, initial, plan):
    operations = {name: (set(needs), set(adds), set(deletes))
                  for name, needs, adds, deletes in operators}
    state = set(initial)
    for action in plan:
        needs, adds, deletes = operations[action]
        assert needs <= state, (action, state, needs)
        state = (state - deletes) | adds
    return state


def check_planning():
    school = (
        ("drive", ("home", "car-works"), ("school",), ("home",)),
        ("install", ("needs-battery", "shop-knows", "shop-paid"), ("car-works",), ()),
        ("tell", ("on-phone",), ("shop-knows",), ()),
        ("call", ("number",), ("on-phone",), ()),
        ("lookup", ("phone-book",), ("number",), ()),
        ("pay", ("money",), ("shop-paid",), ("money",)),
    )
    initial = ("home", "needs-battery", "phone-book", "money")
    expected = ("lookup", "call", "tell", "pay", "install", "drive")
    gps = specialize_planner(school)
    assert gps.run(initial, ("school",)) == expected
    assert gps.run(initial, ("school", "money")) is None
    assert gps.run(initial, ("money", "school")) is None
    assert gps.run(initial, ("home", "home")) == ()
    assert gps.run(initial + ("coat",), ("school", "coat")) == expected
    assert gps.run(initial, ("missing",)) is None
    assert "stack & goal" in gps.source and "current[0] & required == required" in gps.source
    for method in ("graph", "beam"):
        search = specialize_planner(school, method=method)
        plan = search.run(initial, ("school",))
        assert len(plan) == len(expected) and "school" in _replay_plan(school, initial, plan)
        assert search.run(initial, ("school", "money")) is None
        assert search.source != gps.source

    # Breaking the cycle requires a different operator for part, and an
    # unreachable loop must terminate without modifying its input.
    cycle = (
        ("assemble", ("part",), ("finish",), ()),
        ("extract", ("finish",), ("part",), ()),
        ("buy", ("cash",), ("part",), ("cash",)),
    )
    loop = specialize_planner(cycle)
    assert loop.run((), ("finish",)) is None
    assert loop.run(("cash",), ("finish",)) == ("buy", "assemble")
    assert specialize_planner(cycle[:2]).run(("cash",), ("finish",)) is None

    ranked = (
        ("assemble", ("part",), ("finish",), ()),
        ("buy", ("cash",), ("part",), ("cash",)),
        ("use-finished", ("stock",), ("finish",), ("stock",)),
    )
    assert specialize_planner(ranked).run(("cash", "stock"), ("finish",)) == ("use-finished",)

    # Reversing goals can recover from deleting an already satisfied goal.
    wash = (
        ("wash", (), ("clean",), ("dry",)),
        ("dry", ("clean",), ("dry",), ()),
    )
    assert specialize_planner(wash).run(("dry",), ("dry", "clean")) == ("wash", "dry")

    clobber = (
        ("join", ("a", "b"), ("done",), ()),
        ("make-b", ("a",), ("b",), ("a",)),
    )
    assert specialize_planner(clobber).run(("a",), ("done",)) is None

    greedy = (
        ("fast-a", ("x",), ("a",), ("y",)),
        ("safe-a", ("x",), ("a",), ()),
        ("make-b", ("y",), ("b",), ("x",)),
    )
    assert specialize_planner(greedy).run(("x", "y"), ("a", "b")) is None
    graph = specialize_planner(greedy, method="graph")
    plan = graph.run(("x", "y"), ("a", "b"))
    assert plan == ("safe-a", "make-b")
    assert {"a", "b"} <= _replay_plan(greedy, ("x", "y"), plan)
    changed = specialize_planner((greedy[1], greedy[0], greedy[2]))
    assert changed.run(("x", "y"), ("a", "b")) == plan
    assert changed.source != specialize_planner(greedy).source

    trap = (
        ("take-shortcut", ("resource",), ("a",), ("resource",)),
        ("prepare", ("resource",), ("ready",), ("resource",)),
        ("finish", ("ready",), ("a", "b"), ()),
    )
    narrow = specialize_planner(trap, method="beam", beam_width=1)
    wide = specialize_planner(trap, method="beam", beam_width=2)
    assert narrow.run(("resource",), ("a", "b")) is None
    assert wide.run(("resource",), ("a", "b")) == ("prepare", "finish")
    assert specialize_planner(()).run(("untouched",), ("untouched",)) == ()


def _assignments(domains, constraints, restrictions=None):
    variables = tuple(domains)
    expected = []
    for row in product(*(domains[variable] for variable in variables)):
        assignment = dict(zip(variables, row))
        if restrictions and any(assignment[var] not in allowed for var, allowed in restrictions.items()):
            continue
        if all((assignment[a], assignment[b]) in pairs for a, b, pairs in constraints):
            expected.append(tuple(assignment.items()))
    return tuple(expected)


def check_constraint_search():
    domains = {name: (0, 1) for name in "abc"}
    same = ((0, 0), (1, 1))
    opposite = ((0, 1), (1, 0))
    relations = (("a", "b", opposite), ("b", "c", opposite))
    solver = specialize_constraints(domains, relations)
    assert solver.run() == ConstraintResult(tuple(domains.items()), _assignments(domains, relations))
    fixed = {"a": (0,)}
    assert solver.run(fixed) == ConstraintResult(
        (("a", (0,)), ("b", (1,)), ("c", (0,))), _assignments(domains, relations, fixed),
    )
    assert solver.run({"a": (0,), "c": (1,)}) == ConstraintResult(None, ())
    assert solver.run({"b": ()}) == ConstraintResult(None, ())
    assert solver.run() == ConstraintResult(tuple(domains.items()), _assignments(domains, relations))

    # Arc consistency keeps every label on the odd cycle. Branching proves
    # that no globally consistent assignment exists.
    odd_cycle = relations + (("c", "a", opposite),)
    assert specialize_constraints(domains, odd_cycle).run() == ConstraintResult(tuple(domains.items()), ())
    changed_relations = relations[:-1] + (("b", "c", same),)
    changed = specialize_constraints(domains, changed_relations)
    assert changed.run(fixed).solutions == _assignments(domains, changed_relations, fixed)
    assert changed.run(fixed).solutions != solver.run(fixed).solutions and changed.source != solver.source

    # These asymmetric tables are written in opposite orientations. Their
    # intersection leaves one assignment and requires support in both directions.
    asymmetric = (("a", "b", ((0, 0), (0, 1), (1, 1))), ("b", "a", ((0, 0), (1, 0))))
    directed = specialize_constraints({"a": (0, 1), "b": (0, 1)}, asymmetric)
    assert directed.run({"b": (0,)}) == ConstraintResult(
        (("a", (0,)), ("b", (0,))), ((("a", 0), ("b", 0)),),
    )
    assert directed.run({"a": (1,)}) == ConstraintResult(None, ())
    assert "_revise_0" in solver.source and "adjacent &" in solver.source
    assert specialize_constraints({"a": ()}, ()).run() == ConstraintResult(None, ())
    assert specialize_constraints({}, ()).run() == ConstraintResult((), ((),))


def check_waltz_labeling():
    vertices = {"a": ("L", ("b", "c")), "b": ("L", ("a", "c")), "c": ("L", ("a", "b"))}
    allowed_l = (("R", "L"), ("L", "R"), ("+", "R"), ("L", "+"), ("-", "L"), ("R", "-"))
    diagram = specialize_waltz(vertices)
    compatible = {("R", "L"), ("L", "R"), ("+", "+"), ("-", "-")}

    def oracle(a_order):
        def consistent(a, b, c):
            return ((a[a_order[0]], b[0]) in compatible
                    and (a[a_order[1]], c[0]) in compatible
                    and (b[1], c[1]) in compatible)

        return tuple((("a", a), ("b", b), ("c", c))
                     for a, b, c in product(allowed_l, repeat=3) if consistent(a, b, c))

    original = oracle((0, 1))
    assert len(original) > 1 and diagram.run().solutions == original
    restriction = {"a": (("+", "R"),)}
    assert diagram.run(restriction).solutions == tuple(
        assignment for assignment in original if assignment[0][1] in restriction["a"]
    )
    changed = specialize_waltz(dict(vertices, a=("L", ("c", "b"))))
    assert changed.run().solutions == oracle((1, 0))
    assert changed.run().solutions != original and changed.source != diagram.source


def _opponent(player):
    return {"X": "O", "O": "X"}[player]


def _othello_children(board, player):
    size = len(board)
    rival = _opponent(player)
    children = []
    for row, column in product(range(size), repeat=2):
        if board[row][column] != ".":
            continue
        captured = []
        for dr, dc in product((-1, 0, 1), repeat=2):
            if dr == dc == 0:
                continue
            r, c = row + dr, column + dc
            line = []
            while 0 <= r < size and 0 <= c < size and board[r][c] == rival:
                line.append((r, c))
                r, c = r + dr, c + dc
            if line and 0 <= r < size and 0 <= c < size and board[r][c] == player:
                captured.extend(line)
        if captured:
            child = [list(line) for line in board]
            for r, c in captured + [(row, column)]:
                child[r][c] = player
            children.append(((row, column), tuple("".join(line) for line in child)))
    return children


def _reference_game(board, root, weights, depth):
    visited = 0
    size = len(board)
    terminal_base = sum(abs(weight) for row in weights for weight in row) + 1

    def visit(position, player, remaining):
        nonlocal visited
        visited += 1
        other = _opponent(player)
        legal = _othello_children(position, player)
        if not legal:
            if _othello_children(position, other):
                score, _ = visit(position, other, remaining)
                return score, None
            margin = sum(row.count(root) - row.count(_opponent(root)) for row in position)
            if margin:
                return margin + (terminal_base if margin > 0 else -terminal_base), None
            return 0, None
        if remaining == 0:
            score = 0
            for row, column in product(range(size), repeat=2):
                cell = position[row][column]
                if cell == root:
                    score += weights[row][column]
                elif cell != ".":
                    score -= weights[row][column]
            return score, None
        choose = max if player == root else min
        return choose(((visit(child, other, remaining - 1)[0], move) for move, child in legal),
                      key=lambda entry: entry[0])

    score, move = visit(board, root, depth)
    return GameDecision(move, score, visited)


def check_adversarial_search():
    opening = ("....", ".OX.", ".XO.", "....")
    weights = ((20, 1, 3, 15), (2, -4, -4, 2), (2, -4, -4, 2), (15, 3, 1, 20))
    shallow = specialize_game(weights, depth=1)
    boosted_weights = tuple(tuple(100 if (r, c) == (3, 2) else weight
                                  for c, weight in enumerate(row))
                            for r, row in enumerate(weights))
    boosted = specialize_game(boosted_weights, depth=1)
    assert shallow.run(opening, "X") != boosted.run(opening, "X")
    assert boosted.run(opening, "X").move == (3, 2)
    assert shallow.source != boosted.source

    minimax = specialize_game(weights, depth=4, algorithm="minimax")
    pruned = specialize_game(weights, depth=4, algorithm="alpha_beta")
    assert "alpha >= beta" in pruned.source and "alpha >= beta" not in minimax.source
    assert pruned.run(opening, "X").visited < minimax.run(opening, "X").visited
    assert pruned.run(opening, "X").score != shallow.run(opening, "X").score

    # Check live states across independent legal playouts, including forced
    # passes in both the root and the recursive search.
    rng = Random(303)
    samples = {(opening, "X"), (("XXXX", "XXXX", "XXOO", "OOO."), "O")}
    for _ in range(8):
        board, player = opening, "X"
        for ply in range(13):
            moves = _othello_children(board, player)
            if ply in (3, 6, 9, 12) or not moves:
                samples.add((board, player))
            if moves:
                _, board = rng.choice(moves)
            elif not _othello_children(board, _opponent(player)):
                break
            player = _opponent(player)

    for board, player in sorted(samples):
        expected = _reference_game(board, player, weights, 4)
        assert minimax.run(board, player) == expected, (board, player, expected)
        result = pruned.run(board, player)
        assert (result.move, result.score) == (expected.move, expected.score), (board, player, result, expected)
        assert 0 < result.visited <= expected.visited
    assert any(not _othello_children(board, player) and _othello_children(board, _opponent(player))
               for board, player in samples)

    large = 10**60
    costly = ((-large,) * 4,) * 4
    terminal = ("XXXX", "XXXX", "OOOO", "XXX.")
    game = specialize_game(costly, depth=0)
    assert game.run(terminal, "X") == _reference_game(terminal, "X", costly, 0)
    assert game.run(("XXXX",) * 4, "X") == GameDecision(None, 16 * large + 17, 1)
    assert game.run(("XXXX",) * 4, "O") == GameDecision(None, -16 * large - 17, 1)
    assert game.run(("XOXX", "OXOX", "XXXO", "OOOX"), "X").move is None

    board6 = ("......", "......", "..OX..", "..XO..", "......", "......")
    weights6 = tuple(tuple(row * 6 - col * 2 for col in range(6)) for row in range(6))
    result6 = specialize_game(weights6, depth=2).run(board6, "O")
    expected6 = _reference_game(board6, "O", weights6, 2)
    assert (result6.move, result6.score) == (expected6.move, expected6.score)
