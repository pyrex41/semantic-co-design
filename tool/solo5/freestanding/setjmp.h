/* sigsetjmp for a guest with no C library, from the compiler's own builtins.

   node.c leaves a step that has no result with siglongjmp(failure, code), code 1 to 3,
   and arms it with sigsetjmp(failure, 0): the signal mask is never saved, and a guest
   has none. __builtin_longjmp can only deliver 1, so the code travels beside it. The
   builtins require that the jump leaves the function that called sigsetjmp, which
   holds: node.c jumps from the step body or its helpers to `run`.

   Saving a mask is refused at compile time rather than ignored: this header has no
   way to do it. */
#ifndef FREESTANDING_SETJMP_H
#define FREESTANDING_SETJMP_H

typedef void *jmp_buf[5];
typedef void *sigjmp_buf[5];
static int freestanding_jump_code;

#define setjmp(buffer) (__builtin_setjmp(buffer) ? freestanding_jump_code : 0)
#define longjmp(buffer, code) (freestanding_jump_code = (code), __builtin_longjmp((buffer), 1))
#define sigsetjmp(buffer, savemask) ((void)sizeof(char[(savemask) == 0 ? 1 : -1]), setjmp(buffer))
#define siglongjmp(buffer, code) longjmp(buffer, code)

#endif
