"""Demand-driven staged stream programs from SICP ch. 3."""

from .state import _emit

_FORCING = object()


class Stream:
    """An eager head and a delayed tail shared by all readers of this cell."""

    __slots__ = ("_head", "_promise", "_tail")

    def __init__(self, head, tail):
        self._head = head
        self._promise = tail
        self._tail = None

    @property
    def head(self):
        return self._head

    @property
    def tail(self):
        promise = self._promise
        if promise is _FORCING:
            raise RuntimeError("stream forced before its current tail was produced")
        if promise is not None:
            self._promise = _FORCING
            try:
                self._tail = promise()
            except BaseException:
                self._promise = promise
                raise
            self._promise = None
        return self._tail


def from_iterable(values):
    """Consume one item immediately, then advance only as new tails are forced."""
    iterator = iter(values)

    def advance():
        try:
            head = next(iterator)
        except StopIteration:
            return None
        return Stream(head, advance)

    return advance()


def take(stream, n):
    """Read at most n values without forcing the tail of the final value."""
    if type(n) is not int or n < 0:
        raise ValueError("stream prefix length must be a nonnegative integer")
    result = []
    for i in range(n):
        if stream is None:
            break
        result.append(stream.head)
        if i + 1 < n:
            stream = stream.tail
    return result


def specialize_pipeline(stages):
    """Allocate one output cell per accepted item in a fixed map/filter order."""
    stages = tuple(stages)
    lines = [
        "def consume(cursor):",
        "    while cursor is not None:",
        "        value = cursor.head",
    ]
    for i, (operation, function) in enumerate(stages):
        if not callable(function):
            raise ValueError("stream stages need a function")
        if operation == "map":
            lines.append(f"        value = functions[{i}](value)")
        elif operation == "filter":
            lines.extend([
                f"        if not functions[{i}](value):",
                "            cursor = cursor.tail",
                "            continue",
            ])
        else:
            raise ValueError("unknown stream stage")
    lines.extend([
        "        return Stream(value, lambda: consume(cursor.tail))",
        "    return None",
    ])
    return _emit(lines, "consume", Stream=Stream, functions=tuple(f for _, f in stages))


def specialize_window(width, stride, combine):
    """Bind a fixed lookahead and stride for lazy grouping or overlapping transforms."""
    if (type(width) is not int or width < 1 or type(stride) is not int
            or stride < 1 or stride > width or not callable(combine)):
        raise ValueError("expected a callable and 1 <= stride <= width")
    lines = ["def consume(first):"]
    for i in range(width):
        node = "first" if i == 0 else f"node{i}"
        previous = "first" if i == 1 else f"node{i - 1}"
        if i:
            lines.append(f"    {node} = {previous}.tail")
        lines.extend([
            f"    if {node} is None:",
            "        return None",
        ])
    heads = ", ".join(["first.head", *(f"node{i}.head" for i in range(1, width))])
    end = "first" if stride == 1 else f"node{stride - 1}"
    lines.append(f"    return Stream(combine({heads}), lambda: consume({end}.tail))")
    return _emit(lines, "consume", Stream=Stream, combine=combine)


def _alternate(ready, waiting):
    current = ready()
    if current is None:
        return waiting()
    return Stream(current.head, lambda: _alternate(waiting, lambda: current.tail))


def interleave(first, second):
    """Give both memoized streams a turn, even when the first is infinite."""
    return _alternate(lambda: first, lambda: second)


def specialize_pairs(combine):
    """Enumerate stream index pairs i <= j using a fixed combining operation."""
    if not callable(combine):
        raise ValueError("pair enumeration needs a combining function")

    def row(head, right):
        if right is None:
            return None
        return Stream(combine(head, right.head), lambda: row(head, right.tail))

    def enumerate_pairs(left, right):
        if left is None or right is None:
            return None
        return Stream(combine(left.head, right.head), lambda: _alternate(
            lambda: row(left.head, right.tail),
            lambda: enumerate_pairs(left.tail, right.tail),
        ))

    return enumerate_pairs


def specialize_accumulator(update):
    """Accumulate a history by replaying a runtime stream of requests."""
    if not callable(update):
        raise ValueError("accumulator needs a state transition")

    def consume(state, requests):
        if requests is None:
            return None
        next_state = update(state, requests.head)
        return Stream(next_state, lambda: consume(next_state, requests.tail))

    return consume


def specialize_feedback(derivative, time_step):
    """Bind a rate function and step for first-order discrete integration."""
    if not callable(derivative):
        raise ValueError("feedback needs a derivative function")

    def advance(history):
        next_value = history.head + time_step * derivative(history.head)
        return Stream(next_value, lambda: advance(history.tail))

    def solve(initial):
        # The first value is available before the feedback input is demanded.
        history = Stream(initial, lambda: advance(history))
        return history

    return solve
