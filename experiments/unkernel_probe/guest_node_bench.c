/* The step alone, without parsing or printing in the loop: reads every row from stdin once, then
   runs node_step over all of them argv[1] times and prints a checksum of every output. Built with
   the generated node.c included (-DNODE_C="path"). The difference between two repeat counts is
   the cost of the steps and of nothing else. */
#include NODE_C

enum { ROWS = 4096, WIDTH = 64 };
static int64_t rows[ROWS][WIDTH], counts[ROWS], result[LIMIT];

int main(int argc, char **argv) {
    char *line = NULL;
    size_t size = 0;
    int64_t n = 0;
    long repeats = argc > 1 ? strtol(argv[1], NULL, 10) : 1;
    while (getline(&line, &size, stdin) >= 0) {
        char *cursor = line;
        if (n == ROWS) return 2;
        while (*cursor) {
            while (isspace((unsigned char)*cursor)) ++cursor;
            if (!*cursor) break;
            char *end;
            if (counts[n] == WIDTH) return 2;
            rows[n][counts[n]++] = strtoimax(cursor, &end, 10);
            if (end == cursor) return 2;
            cursor = end;
        }
        ++n;
    }
    uint64_t sum = 0;
    int64_t steps = 0, traps = 0;
    for (long r = 0; r < repeats; ++r)
        for (int64_t i = 0; i < n; ++i) {
            int64_t written = node_step(rows[i], counts[i], result, LIMIT);
            ++steps;
            if (written < 0) { ++traps; sum = sum * 31 + (uint64_t)written; continue; }
            for (int64_t k = 0; k < written; ++k) sum = sum * 31 + (uint64_t)result[k];
        }
    printf("bench steps %" PRId64 " traps %" PRId64 " checksum %" PRIu64 "\n", steps, traps, sum);
    return 0;
}
