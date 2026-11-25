"""Search, planning, and game residuals from PAIP chapters 4, 6, 13, and 18.

specialize_search(graph, policy, heuristic=None, beam_width=1) takes an ordered
mapping of node names (strings) to ordered (destination, nonnegative integer
cost) edges. A destination may be absent from the mapping and becomes a sink.
The heuristic maps nodes to nonnegative integer estimates, with zero as the
default. The policies are "breadth_first", "depth_first", "best_first" (order
by h), "beam" (order by g + h, truncating the entire frontier), and "a_star"
(order by g + h and reopen a node when a cheaper path is found). The residual
takes (start, goal) and returns SearchPath(states, cost) or None. A* is optimal
when the estimates are admissible for that goal, including zero at the goal.

specialize_planner(operators, method="gps", beam_width=10) takes ordered
(name, preconditions, adds, deletes) tuples with string action and fact names.
The residual takes ordered (initial_facts, goal_facts) iterables and returns
the tuple of actions or None. "gps" tries ordered backward goal achievement
and its reverse, ranks operators by unmet preconditions, rejects loops on the
goal stack, and checks for clobbered goals. "graph" traverses reachable fact
sets breadth first. "beam" ranks
forward states by actions taken plus unmet goals, with a bounded frontier.

The chapter 13 search example uses generic methods to select the frontier
behavior. Here the configured policy selects the generated frontier code.
General object dispatch and method combination are outside this module.

specialize_game(weights, depth=..., algorithm="alpha_beta") takes a square
matrix of integer positional weights for an even board of side at least four,
a nonnegative ply limit and either "minimax" or "alpha_beta". The residual
takes (board, player), where board is a sequence of row strings with '.', 'X',
or 'O', and player is 'X' or 'O'. It returns GameDecision(move, score, visited).
A move is a zero-based (row, column), or None at a pass, terminal board or
zero ply limit. Search counts placements toward the limit. Terminal wins and
losses dominate heuristic scores and compare by signed disc margin. Ties in
move score choose the first legal move in row-major order.
"""

from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count
from textwrap import dedent

from .residual import compile_residual


@dataclass(frozen=True)
class SearchPath:
    states: tuple[str, ...]
    cost: int


def _require_name(name):
    if type(name) is not str or not name:
        raise ValueError("names must be nonempty strings")


def _require_nonnegative(value):
    if type(value) is not int or value < 0:
        raise ValueError("costs and estimates must be nonnegative integers")


def _require_width(beam_width):
    if type(beam_width) is not int or beam_width < 1:
        raise ValueError("beam width must be a positive integer")


def specialize_search(graph, policy, *, heuristic=None, beam_width=1):
    if policy not in {"breadth_first", "depth_first", "best_first", "beam", "a_star"}:
        raise ValueError("unknown search policy")
    if policy == "beam":
        _require_width(beam_width)
    graph = {node: tuple(edges) for node, edges in graph.items()}
    nodes = dict.fromkeys(graph)
    for node, edges in graph.items():
        _require_name(node)
        for destination, cost in edges:
            _require_name(destination)
            _require_nonnegative(cost)
            nodes[destination] = None
    estimates = {} if heuristic is None else dict(heuristic)
    for node, estimate in estimates.items():
        if node not in nodes:
            raise ValueError(f"estimate for unknown node {node!r}")
        _require_nonnegative(estimate)

    lines = [f"NODES = frozenset({tuple(nodes)!r})", "", "def _successors(node):"]
    for node, edges in graph.items():
        if edges:
            literals = tuple((destination, cost) for destination, cost in edges)
            lines.extend([f"    if node == {node!r}:", f"        return {literals!r}"])
    lines.append("    return ()")
    if policy in {"best_first", "beam", "a_star"}:
        lines.extend(["", "def _estimate(node):"])
        for node, estimate in estimates.items():
            if estimate:
                lines.extend([f"    if node == {node!r}:", f"        return {estimate}"])
        lines.append("    return 0")
    lines.extend([
        "",
        "def run(start, goal):",
        "    if start not in NODES or goal not in NODES:",
        "        raise ValueError('start and goal must be graph nodes')",
    ])

    if policy == "breadth_first":
        body = _UNINFORMED.format(
            frontier="deque([(start, (start,), 0)])",
            pop="popleft", edges="_successors(node)",
        )
    elif policy == "depth_first":
        body = _UNINFORMED.format(
            frontier="[(start, (start,), 0)]",
            pop="pop", edges="reversed(_successors(node))",
        )
    elif policy == "best_first":
        body = _BEST_FIRST
    elif policy == "beam":
        body = _BEAM.format(beam_width=beam_width)
    else:
        body = _A_STAR

    lines.extend("    " + line if line else "" for line in dedent(body).strip().splitlines())
    return compile_residual(
        "graph_search", "\n".join(lines) + "\n",
        {"SearchPath": SearchPath, "deque": deque, "count": count,
         "heappush": heappush, "heappop": heappop},
    )


_UNINFORMED = """
    frontier = {frontier}
    expanded = set()
    while frontier:
        node, path, distance = frontier.{pop}()
        if node in expanded:
            continue
        if node == goal:
            return SearchPath(path, distance)
        expanded.add(node)
        for destination, cost in {edges}:
            if destination not in expanded:
                frontier.append((destination, path + (destination,), distance + cost))
    return None
"""


_BEST_FIRST = """
    order = count()
    frontier = [(_estimate(start), next(order), start, (start,), 0)]
    expanded = set()
    while frontier:
        _, _, node, path, distance = heappop(frontier)
        if node in expanded:
            continue
        if node == goal:
            return SearchPath(path, distance)
        expanded.add(node)
        for destination, cost in _successors(node):
            if destination not in expanded:
                heappush(frontier, (_estimate(destination), next(order), destination,
                                    path + (destination,), distance + cost))
    return None
"""


_BEAM = """
    frontier = [(_estimate(start), 0, start, (start,))]
    expanded_costs = {{}}
    while frontier:
        _, distance, node, path = frontier.pop(0)
        if node == goal:
            return SearchPath(path, distance)
        expanded_costs[node] = distance
        for destination, cost in _successors(node):
            next_cost = distance + cost
            if next_cost < expanded_costs.get(destination, float('inf')):
                frontier.append((next_cost + _estimate(destination), next_cost,
                                 destination, path + (destination,)))
        frontier.sort(key=lambda entry: entry[0])
        retained = []
        queued = set()
        for entry in frontier:
            destination = entry[2]
            if destination not in queued:
                retained.append(entry)
                queued.add(destination)
                if len(retained) == {beam_width}:
                    break
        frontier = retained
    return None
"""


_A_STAR = """
    order = count()
    frontier = [(_estimate(start), next(order), start, (start,), 0)]
    best_costs = {start: 0}
    while frontier:
        _, _, node, path, distance = heappop(frontier)
        if distance != best_costs[node]:
            continue
        if node == goal:
            return SearchPath(path, distance)
        for destination, cost in _successors(node):
            next_cost = distance + cost
            if next_cost < best_costs.get(destination, float('inf')):
                best_costs[destination] = next_cost
                heappush(frontier, (next_cost + _estimate(destination), next(order),
                                    destination, path + (destination,), next_cost))
    return None
"""


def specialize_planner(operators, *, method="gps", beam_width=10):
    if method not in {"gps", "graph", "beam"}:
        raise ValueError("unknown planning method")
    if method == "beam":
        _require_width(beam_width)
    facts = {}
    actions = set()
    normalized = []
    for action, preconditions, adds, deletes in operators:
        _require_name(action)
        if action in actions:
            raise ValueError(f"duplicate action {action!r}")
        actions.add(action)
        groups = tuple(tuple(group) for group in (preconditions, adds, deletes))
        for group in groups:
            for fact in group:
                _require_name(fact)
                if fact not in facts:
                    facts[fact] = 1 << len(facts)
        normalized.append((action, *groups))

    def mask(group):
        return sum(facts[fact] for fact in set(group))

    lines = [f"FACT_BITS = {facts!r}"]
    if method == "gps":
        lines.append(dedent(_ACHIEVE_ALL).strip("\n"))
        achievers = {}
        precondition_masks = []
        for index, (action, preconditions, adds, deletes) in enumerate(normalized):
            precondition_bits = tuple(facts[fact] for fact in dict.fromkeys(preconditions))
            precondition_masks.append(mask(preconditions))
            lines.extend([
                "",
                f"def _apply_{index}(state, stack):",
                f"    result = _achieve_all(state, {precondition_bits!r}, {precondition_masks[-1]}, stack)",
                "    if result is not None:",
                "        facts, actions = result",
                f"        return ((facts & ~{mask(deletes)}) | {mask(adds)}, actions + ({action!r},))",
            ])
            for fact in dict.fromkeys(adds):
                achievers.setdefault(facts[fact], []).append(index)

        lines.extend([
            "",
            "def _achieve(state, goal, stack):",
            "    if state[0] & goal:",
            "        return state",
            "    if stack & goal:",
            "        return None",
            "    stack |= goal",
        ])
        for goal, operator_indexes in achievers.items():
            lines.append(f"    if goal == {goal}:")
            if len(operator_indexes) == 1:
                lines.append(f"        return _apply_{operator_indexes[0]}(state, stack)")
                continue
            lines.append("        candidates = (")
            for index in operator_indexes:
                lines.append(f"            (({precondition_masks[index]} & ~state[0]).bit_count(), _apply_{index}),")
            lines.extend([
                "        )",
                "        for _, apply in sorted(candidates, key=lambda candidate: candidate[0]):",
                "            result = apply(state, stack)",
                "            if result is not None:",
                "                return result",
            ])
        lines.append("    return None")
    else:
        lines.extend(["", "def _successors(facts):"])
        for action, preconditions, adds, deletes in normalized:
            required = mask(preconditions)
            lines.extend([
                f"    if facts & {required} == {required}:",
                f"        yield ((facts & ~{mask(deletes)}) | {mask(adds)}, {action!r})",
            ])
        if not normalized:
            lines.append("    yield from ()")

    lines.append(dedent(_INITIAL_STATE).strip("\n"))
    if method == "gps":
        body = _RUN_GPS
    elif method == "graph":
        body = _RUN_GRAPH
    else:
        body = _RUN_BEAM.format(beam_width=beam_width)
    lines.extend("    " + line if line else "" for line in dedent(body).strip().splitlines())
    return compile_residual("planning", "\n".join(lines) + "\n", {"deque": deque})


_ACHIEVE_ALL = """

def _achieve_all(state, goals, required, stack):
    orders = (goals, goals[::-1]) if len(goals) > 1 else (goals,)
    for order in orders:
        current = state
        for goal in order:
            current = _achieve(current, goal, stack)
            if current is None:
                break
        else:
            if current[0] & required == required:
                return current
    return None
"""


_INITIAL_STATE = """

def run(initial, goals):
    initial = frozenset(initial)
    goals = tuple(dict.fromkeys(goals))
    if any(goal not in FACT_BITS and goal not in initial for goal in goals):
        return None
    facts = sum(FACT_BITS.get(fact, 0) for fact in initial)
    goal_bits = tuple(FACT_BITS[goal] for goal in goals if goal in FACT_BITS)
    required = sum(goal_bits)
"""


_RUN_GPS = """
    result = _achieve_all((facts, ()), goal_bits, required, 0)
    return None if result is None else result[1]
"""


_RUN_GRAPH = """
    frontier = deque([(facts, ())])
    seen = {facts}
    while frontier:
        state, actions = frontier.popleft()
        if state & required == required:
            return actions
        for successor, action in _successors(state):
            if successor not in seen:
                seen.add(successor)
                frontier.append((successor, actions + (action,)))
    return None
"""


_RUN_BEAM = """
    frontier = [(facts, ())]
    expanded = set()
    while frontier:
        state, actions = frontier.pop(0)
        if state & required == required:
            return actions
        expanded.add(state)
        for successor, action in _successors(state):
            if successor not in expanded:
                frontier.append((successor, actions + (action,)))
        frontier.sort(key=lambda item: len(item[1]) + (required & ~item[0]).bit_count())
        candidates = []
        queued = set()
        for candidate in frontier:
            if candidate[0] not in queued:
                queued.add(candidate[0])
                candidates.append(candidate)
                if len(candidates) == {beam_width}:
                    break
        frontier = candidates
    return None
"""


@dataclass(frozen=True)
class GameDecision:
    move: tuple[int, int] | None
    score: int
    visited: int


def specialize_game(weights, *, depth, algorithm="alpha_beta"):
    if algorithm not in {"minimax", "alpha_beta"}:
        raise ValueError("unknown game search algorithm")
    if type(depth) is not int or depth < 0:
        raise ValueError("depth must be a nonnegative integer")
    weights = tuple(tuple(row) for row in weights)
    size = len(weights)
    if size < 4 or size % 2 or any(len(row) != size for row in weights):
        raise ValueError("weights must be an even square of side at least four")
    by_weight = {}
    for index, weight in enumerate(weight for row in weights for weight in row):
        if type(weight) is not int:
            raise ValueError("positional weights must be integers")
        if weight:
            by_weight[weight] = by_weight.get(weight, 0) | (1 << index)

    cells = size * size
    ceiling = sum(abs(weight) for row in weights for weight in row)
    lines = [f"FULL = {(1 << cells) - 1}", f"TERMINAL_BASE = {ceiling + 1}"]
    ray_functions = []
    for row_direction, column_direction in (
        (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1),
    ):
        name = f"_shift_{len(ray_functions)}"
        ray_functions.append(name)
        origins = sum(
            1 << (row * size + column)
            for row in range(size)
            for column in range(size)
            if 0 <= row + row_direction < size and 0 <= column + column_direction < size
        )
        step = size * row_direction + column_direction
        shift = "<<" if step > 0 else ">>"
        lines.extend(["", f"def {name}(disc):", f"    return (disc & {origins}) {shift} {abs(step)}"])

    captures = " |\n            ".join(f"_ray({name}, me, rival, move)" for name in ray_functions)
    terms = " +\n            ".join(
        f"{weight} * ((me & {mask}).bit_count() - (rival & {mask}).bit_count())"
        for weight, mask in by_weight.items()
    )
    lines.extend([
        "",
        "def _flips(me, rival, move):",
        f"    return ({captures})",
        "",
        "def _evaluate(me, rival):",
        f"    return ({terms or '0'})",
        dedent(_GAME_MECHANICS).strip("\n"),
        "",
        "def run(board, player):",
        "    board = tuple(board)",
        f"    if len(board) != {size} or any(type(row) is not str or len(row) != {size} for row in board):",
        "        raise ValueError('board has the wrong dimensions')",
        "    if player not in ('X', 'O'):",
        "        raise ValueError('player must be X or O')",
        "    me = rival = 0",
        "    for index, char in enumerate(''.join(board)):",
        "        if char == player:",
        "            me |= 1 << index",
        "        elif char in ('X', 'O'):",
        "            rival |= 1 << index",
        "        elif char != '.':",
        "            raise ValueError('board cells must be X, O, or .')",
        "    visited = 0",
    ])
    bound = ceiling + cells + 2
    if algorithm == "alpha_beta":
        body = _GAME_ALPHA_BETA.format(depth=depth, bound=bound)
    else:
        body = _GAME_MINIMAX.format(depth=depth, bound=bound)
    lines.extend("    " + line if line else "" for line in dedent(body).strip().splitlines())
    lines.append(
        f"    return GameDecision(None if move is None else divmod(move.bit_length() - 1, {size}), score, visited)"
    )
    return compile_residual("othello", "\n".join(lines) + "\n", {"GameDecision": GameDecision})


_GAME_MECHANICS = """

def _ray(shift, me, rival, move):
    cursor = shift(move) & rival
    captured = cursor
    while cursor:
        beyond = shift(cursor)
        if beyond & me:
            return captured
        cursor = beyond & rival
        captured |= cursor
    return 0


def _moves(me, rival):
    empty = FULL & ~(me | rival)
    while empty:
        move = empty & -empty
        empty ^= move
        flips = _flips(me, rival, move)
        if flips:
            yield move, flips


def _terminal(me, rival):
    margin = me.bit_count() - rival.bit_count()
    if margin > 0:
        return TERMINAL_BASE + margin
    if margin < 0:
        return -TERMINAL_BASE + margin
    return 0
"""


_GAME_MINIMAX = """
    def visit(me, rival, left):
        nonlocal visited
        visited += 1
        legal = tuple(_moves(me, rival))
        if not legal:
            if next(_moves(rival, me), None) is None:
                return _terminal(me, rival), None
            score, _ = visit(rival, me, left)
            return -score, None
        if left == 0:
            return _evaluate(me, rival), None
        best = -{bound}
        best_move = None
        for move, flips in legal:
            score, _ = visit(rival ^ flips, me | move | flips, left - 1)
            if -score > best:
                best, best_move = -score, move
        return best, best_move

    score, move = visit(me, rival, {depth})
"""


_GAME_ALPHA_BETA = """
    def visit(me, rival, left, alpha, beta):
        nonlocal visited
        visited += 1
        legal = tuple(_moves(me, rival))
        if not legal:
            if next(_moves(rival, me), None) is None:
                return _terminal(me, rival), None
            score, _ = visit(rival, me, left, -beta, -alpha)
            return -score, None
        if left == 0:
            return _evaluate(me, rival), None
        best = -{bound}
        best_move = None
        for move, flips in legal:
            score, _ = visit(rival ^ flips, me | move | flips, left - 1, -beta, -alpha)
            if -score > best:
                best, best_move = -score, move
                alpha = max(alpha, best)
            if alpha >= beta:
                break
        return best, best_move

    score, move = visit(me, rival, {depth}, -{bound}, {bound})
"""
