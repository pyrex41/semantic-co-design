/* The four functions a freestanding compiler may call on its own (for an array
   initializer, a structure copy). The Solo5 bindings have them but keep them local.
   Compiled with -fno-builtin -fno-tree-loop-distribute-patterns so that these loops are
   not themselves turned back into calls. */
#include <stddef.h>

void *memset(void *to, int byte, size_t count) {
    unsigned char *p = to;
    while (count--) *p++ = (unsigned char)byte;
    return to;
}

void *memcpy(void *restrict to, const void *restrict from, size_t count) {
    unsigned char *p = to;
    const unsigned char *q = from;
    while (count--) *p++ = *q++;
    return to;
}

void *memmove(void *to, const void *from, size_t count) {
    unsigned char *p = to;
    const unsigned char *q = from;
    if (p < q)
        while (count--) *p++ = *q++;
    else
        while (count--) p[count] = q[count];
    return to;
}

int memcmp(const void *a, const void *b, size_t count) {
    const unsigned char *p = a, *q = b;
    for (; count--; ++p, ++q)
        if (*p != *q) return *p < *q ? -1 : 1;
    return 0;
}
