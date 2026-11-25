"""Precompute finite ternary board-pattern scores (PAIP 18.12)."""

from itertools import product

from .residual import compile_residual


def compile_edge_table(size, edges, score):
    """Build a lookup evaluator for equally sized board windows.

    ``edges`` contains ordered tuples of distinct flat cell indices. Up to ten
    cells per window are supported. ``score`` receives a tuple of ternary digits
    with 0=empty, 1=current player and 2=opponent, and runs only at build time.
    The residual scores dynamic ``'.'``, ``'X'`` and ``'O'`` input rows from
    either player's point of view, summing scores for the fixed windows.
    """
    if type(size) is not int or size < 1 or not callable(score):
        raise ValueError("expected a positive side length and a callable scorer")
    edges = tuple(tuple(edge) for edge in edges)
    if not edges or not 1 <= len(edges[0]) <= 10:
        raise ValueError("expected board windows of 1 to 10 cells")
    width = len(edges[0])
    for edge in edges:
        if len(edge) != width or not all(type(i) is int and 0 <= i < size * size for i in edge):
            raise ValueError("expected equally sized windows with valid flat cell indices")
        if len(set(edge)) != width:
            raise ValueError("window cells must be distinct")
    values = tuple(score(digits) for digits in product(range(3), repeat=width))
    if not all(type(value) is int for value in values):
        raise ValueError("expected integer local scores")
    reads = []
    for edge in edges:
        index = " + ".join(f"{3 ** (width - i - 1)} * cells[{pos}]" for i, pos in enumerate(edge))
        reads.append(f"table[{index}]")
    addition = " +\n            ".join(reads)
    source = f"""table = {values!r}

def run(board, player):
    board = tuple(board)
    if player not in ('X', 'O'):
        raise ValueError('player must be X or O')
    if len(board) != {size} or any(type(row) is not str or len(row) != {size} for row in board):
        raise ValueError('board has the wrong dimensions')
    digits = {{'.': 0, player: 1, 'O' if player == 'X' else 'X': 2}}
    try:
        cells = tuple(digits[cell] for cell in ''.join(board))
    except KeyError as error:
        raise ValueError('board cells must be X, O, or .') from error
    return ({addition})
"""
    return compile_residual("edge-table", source)
