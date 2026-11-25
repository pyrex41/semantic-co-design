"""Specialize Cheney's stop-and-copy collector against record layouts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Ref:
    index: int


@dataclass(frozen=True)
class Copy:
    roots: tuple
    heap: tuple


def compile_collector(layouts, root_layout):
    """Capture the roots and the traceable fields of each static record layout.

    A layout is a sequence of 'atom' (payload), 'ref' (Ref or None), or
    'slot' (payload or Ref). A heap is a sequence of (tag, fields) records.
    Collecting a fresh graph returns rewritten roots and new indexed records.
    """
    def fields(kinds):
        readers = []
        for index, kind in enumerate(kinds):
            if kind == "atom":
                def atom(row, relocate, index=index):
                    value = row[index]
                    if isinstance(value, Ref):
                        raise ValueError("reference found in an atom field")
                    return value

                readers.append(atom)
            elif kind == "ref":
                def pointer(row, relocate, index=index):
                    value = row[index]
                    if value is not None and not isinstance(value, Ref):
                        raise ValueError("expected a reference or None")
                    return relocate(value)

                readers.append(pointer)
            elif kind == "slot":
                readers.append(lambda row, relocate, index=index: relocate(row[index]))
            else:
                raise ValueError(f"unknown field kind {kind!r}")

        def scan(row, relocate):
            if len(row) != len(readers):
                raise ValueError("row does not match the specialized layout")
            return tuple(read(row, relocate) for read in readers)

        return scan

    scanners = {name: fields(layout) for name, layout in layouts.items()}
    roots_scanner = fields(root_layout)

    def collect(heap, roots):
        moved = {}
        heap_copy = []

        def relocate(value):
            if isinstance(value, Ref):
                index = value.index
                if type(index) is not int or not 0 <= index < len(heap):
                    raise ValueError(f"invalid heap reference {value!r}")
                if index not in moved:
                    # Copy before following children, so aliases and cycles share
                    # the same destination slot even while it is still unscanned.
                    moved[index] = Ref(len(heap_copy))
                    heap_copy.append(heap[index])
                return moved[index]
            return value

        new_roots = roots_scanner(roots, relocate)
        scan = 0
        while scan < len(heap_copy):
            tag, row = heap_copy[scan]
            heap_copy[scan] = (tag, scanners[tag](row, relocate))
            scan += 1
        return Copy(new_roots, tuple(heap_copy))

    return collect
