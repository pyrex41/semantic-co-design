#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

enum opcode { PUSH_INPUT, PUSH_LITERAL, ADD, MULTIPLY };
struct instruction { enum opcode op; uint32_t literal; };

/* Programs start with the accumulator on the stack and leave one word. */
uint32_t interpret(const struct instruction *program, size_t length,
                   uint32_t seed, const uint32_t *input, size_t count) {
    uint32_t accumulator = seed;
    for (size_t i = 0; i < count; ++i) {
        uint32_t stack[16] = {accumulator};
        size_t depth = 1;
        for (size_t pc = 0; pc < length; ++pc) {
            switch (program[pc].op) {
            case PUSH_INPUT:
            case PUSH_LITERAL:
                if (depth == 16) abort();
                stack[depth++] = program[pc].op == PUSH_INPUT ? input[i] : program[pc].literal;
                break;
            case ADD:
            case MULTIPLY:
                if (depth < 2) abort();
                --depth;
                if (program[pc].op == ADD)
                    stack[depth - 1] += stack[depth];
                else
                    stack[depth - 1] *= stack[depth];
                break;
            default:
                abort();
            }
        }
        if (depth != 1) abort();
        accumulator = stack[0];
    }
    return accumulator;
}
