/* Shared driver for handwritten and generated C. Rows come from the validated
   benchmark corpus. Retain every output and print it after timing. */
#define _POSIX_C_SOURCE 200809L
#define main original_main
#include KERNEL
#undef main
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <time.h>

typedef struct { size_t n, capacity, used; int64_t *data, *answer; } Row;

static double cpu(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t)) exit(2);
    return t.tv_sec + 1e-9 * t.tv_nsec;
}

static void run(Row *row) {
    memcpy(input, row->data, row->n * sizeof(*input));
    emitted = 0;
    evaluate(row->n);
    if (emitted > row->capacity) exit(2);
    row->used = emitted;
    memcpy(row->answer, output, emitted * sizeof(*output));
}

int main(void) {
    size_t count;
    if (scanf("%zu", &count) != 1 || !count || count > 4096) return 2;
    Row *rows = calloc(count, sizeof(*rows));
    if (!rows) return 2;
    for (size_t i = 0; i < count; ++i) {
        Row *r = &rows[i];
        if (scanf("%zu%zu", &r->n, &r->capacity) != 2 || r->n > LIMIT || r->capacity > LIMIT) return 2;
        r->data = calloc(r->n + 1, sizeof(*input));
        r->answer = calloc(r->capacity + 1, sizeof(*output));
        if (!r->data || !r->answer) return 2;
        for (size_t j = 0; j < r->n; ++j)
            if (scanf("%" SCNd64, &r->data[j]) != 1) return 2;
    }
    run(&rows[0]);
    double start = cpu();
    for (size_t i = 0; i < count; ++i) run(&rows[i]);
    printf("%.12f\n", cpu() - start);
    for (size_t i = 0; i < count; ++i) {
        for (size_t j = 0; j < rows[i].used; ++j) printf("%" PRId64 " ", rows[i].answer[j]);
        puts("");
        free(rows[i].data); free(rows[i].answer);
    }
    free(rows);
}
