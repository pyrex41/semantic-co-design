"""Compare selected projection observations to Chapter 3 and solver APIs.

The Newton comparison uses the published solver with a discrete update and
checks the baseline against the floor of the converged real-valued solution. Circuit checks exercise the circuit's agenda directly:
the projection describes generic transport events, not a gate topology.
"""
from math import floor

from experiments.patterns.sicp.procedures import compile_solver
from experiments.patterns.sicp.state import Contradiction, specialize_circuit, specialize_constraints


def _agenda(args, config, changed):
    circuit = specialize_circuit(('input',), ())()
    emitted = []
    delays = config[::-1] if changed else config
    for index in range(0, len(args), 2):
        time, bit = args[index:index + 2]
        # Enqueue from time zero, retaining insertion order for equal deadlines.
        circuit.agenda._schedule(time + delays[bit],
            lambda value: emitted.append((circuit.agenda.now, value)), bit)
    circuit.agenda.run()
    trace = 0
    for time, bit in emitted:
        trace = trace * 100 + 2 * time + bit
    return trace


def _connector(args, total):
    network = specialize_constraints(('x', 'y', 'total'),
        (('sum', 'x', 'y', 'total'),), constants={'total': total})()
    owners = [object(), object()]
    owned = [False, False]
    conflicts = 0
    for index in range(0, len(args), 2):
        op, value = args[index:index + 2]
        slot = op // 2
        connector = network[('x', 'y')[slot]]
        if op % 2 == 0:
            connector.retract(owners[slot])
            owned[slot] = False
        else:
            try:
                acquired = connector.assert_value(value, owners[slot])
            except Contradiction:
                conflicts += 1
            else:
                owned[slot] = owned[slot] or acquired
    x, y = network['x'].read(), network['y'].read()
    mask = int(owned[0]) + 2 * int(owned[1])
    return (0 if x is None else (int(x) + 1) * 1000) + (0 if y is None else (int(y) + 1) * 100) + mask * 10 + conflicts


def check(cases):
    """Check matching fixture observations; return per-family comparison counts."""
    by_id = {case['id']: case for case in cases}
    counts = {'sicp-circuit': 0, 'sicp-constraints': 0, 'sicp-newton-square-root': 0}
    case = by_id['sicp-circuit']
    for args, original, changed in case['samples']:
        for edited, expected in ((False, original), (True, changed)):
            actual = _agenda(args, case['config'], edited)
            assert actual == expected, (case['id'], args, edited, actual, expected)
            counts[case['id']] += 1
    case = by_id['sicp-constraints']
    for args, original, changed in case['samples']:
        for edited, expected in ((False, original), (True, changed)):
            actual = _connector(args, case['config'][int(edited)])
            assert actual == expected, (case['id'], args, edited, actual, expected)
            counts[case['id']] += 1
    case = by_id['sicp-newton-square-root']
    target = case['config'][0]
    solver = compile_solver(lambda x: x * x - target, transform='newton',
                            derivative=lambda x: 2 * x, tolerance=1e-10,
                            max_steps=case['config'][1])
    for args, original, changed in case['samples']:
        root = floor(solver(args[0]))
        assert root == original, (case['id'], args, root, original)
        counts[case['id']] += 1
        for policy, expected in zip(case['config'][2:], (original, changed)):
            def discrete_step(guess):
                following = floor((guess + target / guess) / 2)
                return guess if guess - following <= policy else following

            integer_solver = compile_solver(discrete_step, tolerance=0.5,
                                            max_steps=case['config'][1])
            assert integer_solver(args[0]) == expected
            counts[case['id']] += 1
    return counts
