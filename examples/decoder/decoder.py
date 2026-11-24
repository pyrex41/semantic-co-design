"""Independent incremental decoder oracle.

Static grammar is ``[magic, [accepted_kinds], endian, max_length, seed]``.
Runtime rows encode chunks as ``[chunk_length, byte, ...]`` repeatedly.
"""
from __future__ import annotations


def decode(static: list, row: list[int]) -> list[int]:
    magic, accepted, endian, maximum, seed = static
    accepted = set(accepted)
    out: list[int] = []
    pos = 0
    phase = 0
    kind = length = checksum = 0
    payload: list[int] = []
    error = -1
    frozen = False

    def pending() -> int:
        return max(0, length - len(payload)) if phase == 4 else 0

    def checkpoint() -> None:
        out.extend([-2, pos, error, phase, pending()])

    def fail(at: int, new_phase: int) -> None:
        nonlocal error, frozen, phase, pos
        error, phase, frozen = at, new_phase, True
        pos = at + 1

    i = 0
    while i < len(row):
        chunk_len = row[i]
        i += 1
        chunk_end = min(len(row), i + max(0, chunk_len))
        while i < chunk_end:
            byte = row[i] & 0xFF
            i += 1
            if frozen:
                continue
            absolute = pos
            pos += 1
            if phase == 0:
                if byte != magic: fail(absolute, 0)
                else: phase = 1
            elif phase == 1:
                if byte not in accepted: fail(absolute, 1)
                else: kind, checksum, phase = byte, seed + byte, 2
            elif phase in (2, 3):
                if phase == 2:
                    length = byte * (256 if endian == 0 else 1)
                else:
                    length += byte * (1 if endian == 0 else 256)
                if length > maximum:
                    fail(absolute, phase)
                else:
                    checksum += byte
                    if phase == 2:
                        phase = 3
                    else:
                        payload = []
                        phase = 4 if length else 5
            elif phase == 4:
                payload.append(byte); checksum += byte
                if len(payload) == length: phase = 5
            elif phase == 5:
                if byte != checksum % 256: fail(absolute, 5)
                else:
                    out.extend([-1, kind, length, *payload]); phase = 0
        checkpoint()
    return out


def frame(static: list, kind: int, payload: list[int]) -> list[int]:
    magic, _, endian, _, seed = static
    n = len(payload)
    lengths = [n >> 8, n & 255] if endian == 0 else [n & 255, n >> 8]
    checksum = (seed + kind + sum(lengths) + sum(payload)) & 255
    return [magic, kind, *lengths, *payload, checksum]
