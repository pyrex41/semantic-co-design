"""Check the PAIP game projection against actual two-ply Othello searches.

Run: nix develop --command python3 -m experiments.patterns.projections.catalog_game
"""
from ..paip.check_search_constraints import _othello_children
from ..paip.search import specialize_game
from .cases.paip_search import CASES

WEIGHTS = ((20, 1, 3, 15), (2, -4, -4, 2),
           (2, -4, -4, 2), (15, 3, 1, 20))
POSITIONS = (
    (("...X", ".OXX", ".XXX", "XOOO"), "O"),
    (("XXX.", ".OX.", "OOOX", "XO.O"), "X"),
)


def check(cases):
    case = cases['paip-adversarial-search']
    two_ply = specialize_game(WEIGHTS, depth=2, algorithm='alpha_beta')
    one_ply = specialize_game(WEIGHTS, depth=1, algorithm='alpha_beta')
    leaf_game = specialize_game(WEIGHTS, depth=0, algorithm='alpha_beta')
    assert len(case['samples']) == len(POSITIONS)
    for (board, player), (leaves, expected, changed_expected) in zip(POSITIONS, case['samples'], strict=True):
        other = 'O' if player == 'X' else 'X'
        children = _othello_children(board, player)
        assert len(children) == 2
        values, opponent_scores = [], []
        for _, child in children:
            replies = _othello_children(child, other)
            assert len(replies) == 2
            # The root has a legal move at every leaf, so depth-zero scores
            # are positional evaluations, without pass or terminal handling.
            assert all(_othello_children(leaf, player) for _, leaf in replies)
            values.extend(leaf_game.run(leaf, player).score for _, leaf in replies)
            opponent_scores.append(-one_ply.run(child, other).score)
        assert values == leaves, (board, values, leaves)
        assert opponent_scores == [min(values[:2]), min(values[2:])]
        assert two_ply.run(board, player).score + case['config'][0] == expected
        # The source edit makes the root minimize while its opponent still
        # minimizes the root's score. Reuse the original one-ply searches.
        assert min(opponent_scores) + case['config'][0] == changed_expected
        assert expected != changed_expected
    return 2 * len(POSITIONS)


if __name__ == "__main__":
    observations = check({case["id"]: case for case in CASES})
    print(f"PAIP game: {observations} profile observations from {len(POSITIONS)} Othello positions")
