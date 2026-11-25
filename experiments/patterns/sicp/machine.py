"""Assemble a fixed SICP-style register controller to execution procedures."""

from dataclasses import dataclass, field

from .evaluator import _names


_UNASSIGNED = object()


@dataclass(frozen=True)
class MachineResult:
    outputs: tuple
    steps: int
    pushes: int
    max_depth: int


@dataclass(frozen=True)
class InstructionSequence:
    """A straight-line fragment and its register read/write effects."""

    needs: frozenset
    modifies: frozenset
    code: tuple


def preserving(registers, first, second):
    """Compose fragments, protecting live values only where first changes them."""
    needs, modifies, code = first.needs, first.modifies, first.code
    for name in _names(registers):
        if name in second.needs and name in modifies:
            needs = needs | {name}
            modifies = modifies - {name}
            code = (("save", name), *code, ("restore", name))
    return InstructionSequence(needs | (second.needs - modifies),
                               modifies | second.modifies, (*code, *second.code))


@dataclass(frozen=True)
class _Address:
    machine: object
    position: int


@dataclass
class _State:
    registers: list
    flag: bool = False
    stack: list = field(default_factory=list)
    pushes: int = 0
    max_depth: int = 0

    def read(self, offset):
        value = self.registers[offset]
        if value is _UNASSIGNED:
            raise ValueError("read from an unassigned register")
        return value


def compile_machine(registers, controller, *, inputs=(), outputs=(), operations=None):
    """Assemble assign/test/branch/goto/save/restore/perform and optional halt.

    Expressions are const, reg, label or op tuples. A controller label is
    ``('label', name)``. A test or perform takes an op expression, a branch
    takes a label name, and goto takes a label or reg expression. The result
    is a function of input register values with an optional step budget.
    """
    registers = _names(registers)
    offsets = {name: position for position, name in enumerate(registers)}
    input_slots = tuple(offsets[name] for name in _names(inputs))
    output_slots = tuple(offsets[name] for name in outputs)
    operations = dict(operations or {})
    machine = object()
    labels, instructions = {}, []
    for instruction in controller:
        if instruction[0] == "label":
            _, name = instruction
            if name in labels:
                raise ValueError(f"duplicate controller label {name!r}")
            labels[name] = _Address(machine, len(instructions))
        else:
            instructions.append(instruction)
    end = len(instructions)

    def operand(expression):
        form, value, *items = expression
        if form == "op":
            operation = operations[value]
            if not callable(operation):
                raise ValueError(f"operation {value!r} is not callable")
            arguments = tuple(map(operand, items))
            return lambda state: operation(*(arg(state) for arg in arguments))
        if items:
            raise ValueError(f"extra operands in {expression!r}")
        if form == "reg":
            offset = offsets[value]
            return lambda state: state.read(offset)
        if form == "label":
            address = labels[value]
            return lambda state: address
        if form == "const":
            return lambda state: value
        raise ValueError(f"unknown operand {form!r}")

    def assemble(instruction):
        form, *args = instruction
        if form == "assign":
            target, source = args
            offset = offsets[target]
            value = operand(source)

            def assign(state):
                state.registers[offset] = value(state)

            return assign
        if form in {"test", "perform"}:
            expression, = args
            if expression[0] != "op":
                raise ValueError(f"{form} requires an op expression")
            action = operand(expression)

            def test(state):
                state.flag = action(state) is not False

            def perform(state):
                action(state)

            return test if form == "test" else perform
        if form == "branch":
            name, = args
            target = labels[name].position
            return lambda state: target if state.flag else None
        if form == "goto":
            expression, = args
            if expression[0] == "label":
                _, name = expression
                target = labels[name].position
                return lambda state: target
            if expression[0] != "reg":
                raise ValueError("goto requires a label or register")
            address = operand(expression)

            def jump(state):
                target = address(state)
                if not isinstance(target, _Address) or target.machine is not machine:
                    raise ValueError("continuation register does not contain a label address")
                return target.position

            return jump
        if form == "save":
            name, = args
            offset = offsets[name]

            def save(state):
                state.stack.append(state.read(offset))
                state.pushes += 1
                state.max_depth = max(state.max_depth, len(state.stack))

            return save
        if form == "restore":
            name, = args
            offset = offsets[name]

            def restore(state):
                if not state.stack:
                    raise ValueError("restore from an empty register stack")
                state.registers[offset] = state.stack.pop()

            return restore
        if form == "halt" and not args:
            return lambda state: end
        raise ValueError(f"unknown instruction {form!r}")

    code = tuple(map(assemble, instructions))

    def run(*values, budget=None):
        if len(values) != len(input_slots):
            raise TypeError("incorrect input arity")
        if budget is not None and (type(budget) is not int or budget < 0):
            raise ValueError("machine budget must be a nonnegative integer")
        state = _State([_UNASSIGNED] * len(registers))
        for offset, value in zip(input_slots, values):
            state.registers[offset] = value
        pc, steps = 0, 0
        while pc != end:
            if steps == budget:
                raise RuntimeError("instruction budget exhausted")
            target = code[pc](state)
            pc = pc + 1 if target is None else target
            steps += 1
        return MachineResult(tuple(map(state.read, output_slots)), steps, state.pushes, state.max_depth)

    return run
