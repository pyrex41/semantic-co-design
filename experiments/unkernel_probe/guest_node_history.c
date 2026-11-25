/* A whole recorded history through the node, inside the guest. Built with the generated node.c
   included (-DNODE_C="path", -DSTATE_WIDTH=n), so the step is the same code as the line executable.

     s <state integers>    the node's state is now this (boot, or what a crash left)
     e <event integers>    one step: node_step(state ++ event); prints the output vector and keeps
                           its first STATE_WIDTH integers as the next state

   The driver, not the host, carries the state from one step to the next. */
#include NODE_C

static int64_t state[STATE_WIDTH], row[LIMIT], result[LIMIT];

int main(void) {
    char *line = NULL;
    size_t size = 0;
    ssize_t bytes;
    int have = 0;
    while ((bytes = getline(&line, &size, stdin)) >= 0) {
        char kind = line[0], *cursor = line + 1;
        int64_t count = 0, *into = kind == 's' ? state : row + STATE_WIDTH;
        int64_t room = kind == 's' ? STATE_WIDTH : LIMIT - STATE_WIDTH;
        if (kind != 's' && kind != 'e') return 2;
        while (*cursor) {
            while (isspace((unsigned char)*cursor)) ++cursor;
            if (!*cursor) break;
            char *end;
            errno = 0;
            intmax_t value = strtoimax(cursor, &end, 10);
            if (end == cursor || errno || count == room || (*end && !isspace((unsigned char)*end))) return 2;
            into[count++] = value;
            cursor = end;
        }
        if (kind == 's') {
            if (count != STATE_WIDTH) return 2;
            have = 1;
            continue;
        }
        if (!have) return 2;
        memcpy(row, state, sizeof state);
        int64_t written = node_step(row, STATE_WIDTH + count, result, LIMIT);
        if (written < 0) {
            printf("trap %" PRId64 "\n", -written);
        } else {
            if (written < STATE_WIDTH) return 2;
            for (int64_t i = 0; i < written; ++i) {
                if (i) putchar(' ');
                printf("%" PRId64, result[i]);
            }
            putchar('\n');
            memcpy(state, result, sizeof state);
        }
        if (fflush(stdout) != 0) return 2;
    }
    free(line);
    return ferror(stdin) ? 2 : 0;
}
