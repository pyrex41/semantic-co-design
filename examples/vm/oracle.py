"""Independent stack-machine oracle for the VM fixtures."""
from __future__ import annotations

U32 = 1 << 32


def u32(value: int) -> int:
    return value % U32


def run(program: list, values: list[int]) -> list[int]:
    seed, empty_policy, body = program
    if not values:
        return [u32(seed)] if empty_policy == 0 else []
    accumulator = seed
    for position, current in enumerate(values):
        if position == 0 and empty_policy == 1:
            accumulator = current
            continue
        stack = [accumulator]
        for instruction in body:
            opcode = instruction[0]
            if opcode == 0:
                stack.append(current)
            elif opcode == 1:
                stack.append(instruction[1])
            elif opcode in (2, 3, 4):
                right, left = stack.pop(), stack.pop()
                if opcode == 2:
                    stack.append(u32(left + right))
                elif opcode == 3:
                    stack.append(u32(left * right))
                else:
                    stack.append(max(u32(left), u32(right)))
            else:
                raise ValueError(f"unknown opcode {opcode}")
        accumulator = stack[-1]
    return [u32(accumulator)]


def cases() -> list[tuple[str, list[int], list[int]]]:
    programs = [
        ("sum", [0, 0, [[0], [2]]]),
        ("maximum", [0, 1, [[0], [4]]]),
        ("checksum", [17, 0, [[1, 31], [3], [0], [2]]]),
    ]
    vectors = [[], [0, 1, -1], [2**31 - 1, -(2**31)], [7, 7, 3]]
    return [(name, row, run(program, row)) for name, program in programs for row in vectors]
