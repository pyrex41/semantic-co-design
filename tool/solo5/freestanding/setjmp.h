/* setjmp for a guest with no C library, from the compiler's own builtins.

   node.c leaves a step that has no result with longjmp(failure, code), code 1 to 3.
   __builtin_longjmp can only deliver 1, so the code travels beside it. The builtins
   require that the jump leaves the function that called setjmp, which holds: node.c
   jumps from the step body or its helpers to `run`. */
#ifndef FREESTANDING_SETJMP_H
#define FREESTANDING_SETJMP_H

typedef void *jmp_buf[5];
static int freestanding_jump_code;

#define setjmp(buffer) (__builtin_setjmp(buffer) ? freestanding_jump_code : 0)
#define longjmp(buffer, code) (freestanding_jump_code = (code), __builtin_longjmp((buffer), 1))

#endif
