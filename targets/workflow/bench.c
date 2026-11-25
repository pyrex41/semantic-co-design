/* Timing driver for a compiled node. It only feeds recorded input vectors to node_step
   and reads the clock; it knows nothing about workflows.

   usage: bench <width> <capacity> <state width> <trials> <repetitions> < vectors
   stdin: the number of vectors, then that many rows of <width> integers.
   Each trial prints "@@", the CPU seconds of <repetitions> passes over all vectors and the
   number of effects those steps emitted, which the caller compares with the other lanes. */
#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

int64_t node_step(const int64_t *in, int64_t count, int64_t *out, int64_t capacity);

static double cpu(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t)) exit(2);
    return t.tv_sec + 1e-9 * t.tv_nsec;
}

int main(int argc, char **argv) {
    if (argc != 6) return 2;
    long width = atol(argv[1]), capacity = atol(argv[2]), state = atol(argv[3]);
    long trials = atol(argv[4]), repetitions = atol(argv[5]), count;
    if (scanf("%ld", &count) != 1 || count < 1 || width < 1 || capacity <= state) return 2;
    int64_t *rows = calloc((size_t)count * (size_t)width, sizeof(*rows));
    int64_t *out = calloc((size_t)capacity, sizeof(*out));
    if (!rows || !out) return 2;
    for (long i = 0; i < count * width; ++i)
        if (scanf("%" SCNd64, &rows[i]) != 1) return 2;
    for (long i = 0; i < count; ++i)                      /* warm up, and refuse a trap */
        if (node_step(rows + i * width, width, out, capacity) < 0) return 3;
    for (long t = 0; t < trials; ++t) {
        int64_t effects = 0;
        double start = cpu();
        for (long r = 0; r < repetitions; ++r)
            for (long i = 0; i < count; ++i) {
                node_step(rows + i * width, width, out, capacity);
                effects += out[state];
            }
        printf("@@ %.9f %" PRId64 "\n", cpu() - start, effects);
    }
    return 0;
}
