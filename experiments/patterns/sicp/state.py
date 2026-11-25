"""Staged state, digital simulation, and constraint propagation from SICP ch. 3."""

from collections import defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction
from heapq import heappop, heappush
from itertools import count
from threading import Lock
from types import MappingProxyType


def _emit(lines, name, **bindings):
    source = "\n".join(lines) + "\n"
    exec(compile(source, f"<sicp {name}>", "exec"), bindings)
    factory = bindings[name]
    factory.source = source
    return factory


def _nonnegative(value):
    if type(value) is not int or value < 0:
        raise ValueError("expected a nonnegative integer")
    return value


@dataclass(frozen=True, eq=False)
class Account:
    balance: object
    deposit: object
    withdraw: object
    _lock: object
    _adjust: object


def specialize_account(withdrawal_fee=0):
    """Bind withdrawal policy, then create independent, serialized accounts."""
    fee = _nonnegative(withdrawal_fee)
    if fee:
        def withdrawal_total(amount):
            return amount + fee
    else:
        def withdrawal_total(amount):
            return amount

    def make(balance):
        balance = _nonnegative(balance)
        lock = Lock()

        def adjust(delta):
            nonlocal balance
            if balance + delta < 0:
                return None
            balance += delta
            return balance

        def read():
            with lock:
                return balance

        def deposit(amount):
            amount = _nonnegative(amount)
            with lock:
                return adjust(amount)

        def withdraw(amount):
            total = withdrawal_total(_nonnegative(amount))
            with lock:
                return adjust(-total)

        return Account(read, deposit, withdraw, lock, adjust)

    return make


def bind_transfer(source, target):
    """Bind the accounts and lock order once, then transfer runtime amounts."""
    if source is target:
        def transfer(amount):
            return source.balance() >= _nonnegative(amount)

        return transfer

    # Both live locks have stable, distinct identities throughout this binding.
    first, second = sorted((source._lock, target._lock), key=id)
    debit, credit = source._adjust, target._adjust

    def transfer(amount):
        _nonnegative(amount)
        with first, second:
            if debit(-amount) is None:
                return False
            credit(amount)
            return True

    return transfer


@dataclass(frozen=True)
class Cell:
    read: object
    write: object


def specialize_table(fields):
    """Bind public keys to fixed cells. Equal slot numbers share cell identity.

    The profile is a mapping from public keys to contiguous nonnegative slot
    numbers. Instance arguments are the initial values in slot order.
    """
    fields = tuple(fields.items())
    slots = set(_nonnegative(slot) for _, slot in fields)
    if slots != set(range(len(slots))):
        raise ValueError("table slots must be contiguous from zero")

    def cell(value):
        def read():
            return value

        def write(new):
            nonlocal value
            value = new

        return Cell(read, write)

    lines = [
        "def make(*initial):",
        f"    if len(initial) != {len(slots)}:",
        '        raise ValueError("wrong number of initial table values")',
    ]
    for slot in range(len(slots)):
        lines.append(f"    cell{slot} = cell(initial[{slot}])")
    entries = ", ".join(f"keys[{i}]: cell{slot}" for i, (_, slot) in enumerate(fields))
    lines.append(f"    return MappingProxyType({{{entries}}})")
    return _emit(lines, "make", cell=cell, keys=tuple(key for key, _ in fields),
                 MappingProxyType=MappingProxyType)


def specialize_memoized(function, *, key=None):
    """Bind a recursive computation and key policy, then allocate fresh caches.

    The function receives its memoized self as its first argument. Successful
    results persist for each key in an instance. Keys must be hashable and
    identify equivalent calls. Memoized instances expect one driving thread.
    """
    if key is None:
        def key(*arguments):
            return arguments
    if not callable(function) or not callable(key):
        raise TypeError("the recursion and key policies must be callable")

    def make():
        results, active = {}, set()

        def evaluate(*arguments):
            item = key(*arguments)
            if item in results:
                return results[item]
            if item in active:
                raise ValueError("recursive demand for the same memoization key")
            active.add(item)
            try:
                result = function(evaluate, *arguments)
            finally:
                active.remove(item)
            results[item] = result
            return result

        return evaluate

    return make


class Agenda:
    """A dynamic priority queue whose equal-time events execute in FIFO order."""

    def __init__(self):
        self.now = 0
        self.processed = 0
        self._order = count()
        self._pending = []

    def _schedule(self, delay, action, value):
        heappush(self._pending, (self.now + delay, next(self._order), action, value))

    def run(self, *, until=None, max_events=10_000):
        """Run eligible events. A time bound does not advance an idle clock."""
        if until is not None:
            _nonnegative(until)
        _nonnegative(max_events)
        processed = 0
        while self._pending and (until is None or self._pending[0][0] <= until):
            if processed == max_events:
                raise RuntimeError("circuit agenda event budget exhausted")
            self.now, _, action, value = heappop(self._pending)
            action(value)
            processed += 1
            self.processed += 1
        return processed


@dataclass(frozen=True)
class Circuit:
    agenda: Agenda
    inputs: object
    wires: object
    trace: list


def _index(names):
    names = tuple(names)
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("port names must be distinct strings")
    return names, {name: n for n, name in enumerate(names)}


def _bit(value):
    if type(value) is not int or value not in (0, 1):
        raise ValueError("wire signals must be integer bits")
    return value


def specialize_circuit(wires, gates, *, probes=()):
    """Compile (op, input_names, output_name, delay) gates into direct callbacks.

    Supported operations are not, and, and or. Each driven wire has one driver.
    Undriven wires are input ports. All wires start at zero.
    """
    names, index = _index(wires)
    operators = {"not": (1, "1 - {}"), "and": (2, "{} & {}"), "or": (2, "{} | {}")}
    probe_ids = tuple(index[name] for name in probes)
    if len(set(probe_ids)) != len(probe_ids):
        raise ValueError("duplicate circuit probe")
    drivers = set()
    fanout = defaultdict(list)
    compiled = []
    for operation, inputs, output, delay in gates:
        arity, expression = operators[operation]
        inputs = tuple(index[name] for name in inputs)
        output = index[output]
        delay = _nonnegative(delay)
        if len(inputs) != arity or output in drivers or delay == 0:
            raise ValueError("invalid gate arity, multiple drivers, or zero delay")
        drivers.add(output)
        for wire in inputs:
            fanout[wire].append(len(compiled))
        compiled.append((inputs, output, delay, expression))

    lines = [
        "def instantiate():",
        "    agenda = Agenda()",
        "    trace = []",
        f"    values = [0] * {len(names)}",
    ]
    for i in range(len(names)):
        lines.extend([
            f"    def read{i}():",
            f"        return values[{i}]",
            f"    def write{i}(value):",
            f"        if value == values[{i}]:",
            "            return False",
            f"        values[{i}] = value",
        ])
        if i in probe_ids:
            lines.append(f"        trace.append((agenda.now, names[{i}], value))")
        for gate in reversed(fanout[i]):
            lines.append(f"        gate{gate}()")
        lines.append("        return True")
        if i not in drivers:
            lines.extend([
                f"    def input{i}(value):",
                f"        return write{i}(_bit(value))",
            ])
    for i, (inputs, output, delay, expression) in enumerate(compiled):
        expr = expression.format(*(f"values[{wire}]" for wire in inputs))
        lines.extend([
            f"    def gate{i}():",
            f"        agenda._schedule({delay}, write{output}, {expr})",
        ])
    for i in probe_ids:
        lines.append(f"    trace.append((agenda.now, names[{i}], values[{i}]))")
    for i, (inputs, _, _, _) in enumerate(compiled):
        for _ in inputs:
            lines.append(f"    gate{i}()")
    entries = ", ".join(f"names[{i}]: input{i}" for i in range(len(names)) if i not in drivers)
    reads = ", ".join(f"names[{i}]: read{i}" for i in range(len(names)))
    lines.extend([
        f"    inputs = MappingProxyType({{{entries}}})",
        f"    wires = MappingProxyType({{{reads}}})",
        "    return Circuit(agenda, inputs, wires, trace)",
    ])
    return _emit(lines, "instantiate", Agenda=Agenda, Circuit=Circuit,
                 MappingProxyType=MappingProxyType, _bit=_bit, names=names)


class Contradiction(ValueError):
    """A constraint or external assignment disagrees with a known value."""


@dataclass(frozen=True)
class Connector:
    read: object
    assert_value: object
    retract: object


_USER = object()


def specialize_constraints(connectors, equations, *, constants=None):
    """Compile a graph of exact rational sum/product constraints.

    Equations are (op, a, b, result) with op "sum" or "product".
    A connector reports None if no value is known. An assertion can supply an
    optional source token. Retraction requires the original token by identity.
    """
    names, index = _index(connectors)
    constants = {} if constants is None else constants
    seeds = tuple(Fraction(value) for value in constants.values())
    rules = []
    fanout = defaultdict(list)
    for operation, a, b, result in equations:
        terms = (index[a], index[b], index[result])
        if operation not in ("sum", "product") or len(set(terms)) != len(terms):
            raise ValueError("constraint needs a sum/product and three distinct connectors")
        for term in terms:
            fanout[term].append(len(rules))
        rules.append((operation, terms))

    lines = [
        "def instantiate():",
        f"    values = [None] * {len(names)}",
        "    assertions = {}",
        "    pending = deque()",
        "    changes = []",
        "    def drain():",
        "        while pending:",
        "            pending.popleft()()",
        "        changes.clear()",
        "    def seed():",
    ]
    for i, name in enumerate(constants):
        lines.append(f"        put{index[name]}(seeds[{i}])")
    lines.extend([
        "        drain()",
        "    def assert_value(index, put, value, source):",
        "        try:",
        "            changed = put(Fraction(value))",
        "            drain()",
        "        except Contradiction:",
        "            for changed_index in changes:",
        "                values[changed_index] = None",
        "            pending.clear()",
        "            changes.clear()",
        "            raise",
        "        if changed:",
        "            assertions[index] = (source, values[index], put)",
        "        return changed",
        "    def retract(index, source):",
        "        entry = assertions.get(index)",
        "        if entry is None or entry[0] is not source:",
        "            return False",
        "        del assertions[index]",
        f"        values[:] = [None] * {len(names)}",
        "        seed()",
        "        for _, value, put in assertions.values():",
        "            put(value)",
        "            drain()",
        "        return True",
    ])
    for i in range(len(names)):
        lines.extend([
            f"    def read{i}():",
            f"        return values[{i}]",
            f"    def tell{i}(value, source=_USER):",
            f"        return assert_value({i}, put{i}, value, source)",
            f"    def forget{i}(source=_USER):",
            f"        return retract({i}, source)",
            f"    def put{i}(value):",
            f"        old = values[{i}]",
            "        if old is not None:",
            "            if old != value:",
            f"                raise Contradiction((names[{i}], old, value))",
            "            return False",
            f"        values[{i}] = value",
            f"        changes.append({i})",
        ])
        for rule in fanout[i]:
            lines.append(f"        pending.append(infer{rule})")
        lines.append("        return True")
    for i, (operation, (a, b, result)) in enumerate(rules):
        lines.extend([
            f"    def infer{i}():",
            f"        a, b, result = values[{a}], values[{b}], values[{result}]",
        ])
        if operation == "product":
            lines.extend([
                "        if a == 0 or b == 0:",
                f"            put{result}(Fraction(0))",
                "            return",
            ])
        forward, reverse = ("+", "-") if operation == "sum" else ("*", "/")
        lines.extend([
            "        if a is not None and b is not None:",
            f"            put{result}(a {forward} b)",
            "        elif result is not None and a is not None:",
            f"            put{b}(result {reverse} a)",
            "        elif result is not None and b is not None:",
            f"            put{a}(result {reverse} b)",
        ])
    lines.append("    seed()")
    entries = ", ".join(f"names[{i}]: Connector(read{i}, tell{i}, forget{i})" for i in range(len(names)))
    lines.append(f"    return MappingProxyType({{{entries}}})")
    return _emit(lines, "instantiate", names=names, seeds=seeds, deque=deque, _USER=_USER,
                 Contradiction=Contradiction, Fraction=Fraction, Connector=Connector,
                 MappingProxyType=MappingProxyType)
