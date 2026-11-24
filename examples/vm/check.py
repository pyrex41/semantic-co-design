import json
from pathlib import Path
import random

from specializer.replay import verify
from .oracle import run

ROOT = Path(__file__).parent


def main():
    rng = random.Random(17)
    rows = [[], [0], [-1], [2**32 - 1, 1], [-2**31, 2**31-1],
            [2**32-1] * 8] + [[rng.randrange(2**32) for _ in range(n)] for n in range(1, 15)]
    for name in ("sum", "maximum", "checksum"):
        fixed = json.loads((ROOT / f"{name}.json").read_text())
        program = verify("vm", fixed, rows, [run(fixed, row) for row in rows])
        assert str(program).count("'while'") == 1
    fixed = [0, 0, [[0], [2]]]
    verify("vm", fixed, [[3, 7]], [[2**32 - 10]], mutations={
        "shen": ("[u32 [add A B]]", "[u32 [sub A B]]"),
        "maude": ("box(u32 box(add A B))", "box(u32 box(sub A B))")})
    fixed = [0, 0, [[0], [4]]]
    verify("vm", fixed, [[3, 7]], [[7]])
    print(f"VM: 3 programs × {len(rows)} inputs, interpreter and bytecode mutations passed.")


if __name__ == "__main__":
    main()
