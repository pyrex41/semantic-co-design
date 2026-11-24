#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { LIMIT = 65536 };
static int64_t input[LIMIT], output[LIMIT];
static size_t emitted;
static int64_t bank_0[LIMIT];

static int64_t read_at(const int64_t *values, int64_t length, int64_t index) {
    if (index < 0 || index >= length) exit(2);
    return values[index];
}

static void write_at(int64_t *values, int64_t index, int64_t value) {
    if (index < 0 || index >= LIMIT) exit(2);
    values[index] = value;
}

static void emit(int64_t value) {
    if (emitted == LIMIT) exit(2);
    output[emitted++] = value;
}

static void evaluate(int64_t count) {
    int64_t slots[1024] = {0};
    memset(bank_0, 0, sizeof bank_0);
    slots[4] = INT64_C(-1);
    while ((slots[0] < count) != 0) {
        slots[1] = ((slots[0] + INT64_C(1)) + read_at(input, count, slots[0]));
        slots[0] = (slots[0] + INT64_C(1));
        while ((slots[0] < slots[1]) != 0) {
            if ((slots[4] == INT64_C(-1)) != 0) {
                slots[9] = read_at(input, count, slots[0]);
                if ((slots[2] == INT64_C(0)) != 0) {
                    if ((slots[9] == INT64_C(165)) != 0) {
                        slots[6] = INT64_C(0);
                        slots[2] = INT64_C(1);
                    } else {
                        slots[4] = slots[3];
                    }
                } else {
                    if ((slots[2] == INT64_C(1)) != 0) {
                        if (((slots[9] == INT64_C(1)) || ((slots[9] == INT64_C(2)) || INT64_C(0))) != 0) {
                            slots[5] = slots[9];
                            slots[7] = (INT64_C(7) + slots[9]);
                            slots[2] = INT64_C(2);
                        } else {
                            slots[4] = slots[3];
                        }
                    } else {
                        if ((slots[2] == INT64_C(2)) != 0) {
                            slots[6] = (slots[6] + (slots[9] * INT64_C(256)));
                            if ((INT64_C(32) < slots[6]) != 0) {
                                slots[4] = slots[3];
                            } else {
                                slots[7] = (slots[7] + slots[9]);
                                slots[2] = INT64_C(3);
                            }
                        } else {
                            if ((slots[2] == INT64_C(3)) != 0) {
                                slots[6] = (slots[6] + (slots[9] * INT64_C(1)));
                                if ((INT64_C(32) < slots[6]) != 0) {
                                    slots[4] = slots[3];
                                } else {
                                    slots[7] = (slots[7] + slots[9]);
                                    slots[2] = INT64_C(4);
                                }
                                if (((slots[4] == INT64_C(-1)) && (slots[6] == INT64_C(0))) != 0) {
                                    slots[2] = INT64_C(5);
                                }
                            } else {
                                if ((slots[2] == INT64_C(4)) != 0) {
                                    write_at(bank_0, slots[8], slots[9]);
                                    slots[8] = (slots[8] + INT64_C(1));
                                    slots[7] = (slots[7] + slots[9]);
                                    if ((slots[8] == slots[6]) != 0) {
                                        slots[2] = INT64_C(5);
                                    }
                                } else {
                                    if (((slots[7] % INT64_C(256)) == slots[9]) != 0) {
                                        emit(INT64_C(-1));
                                        emit(slots[5]);
                                        emit(slots[6]);
                                        slots[10] = INT64_C(0);
                                        while ((slots[10] < slots[6]) != 0) {
                                            emit(read_at(bank_0, LIMIT, slots[10]));
                                            slots[10] = (slots[10] + INT64_C(1));
                                        }
                                        slots[2] = INT64_C(0);
                                        slots[8] = INT64_C(0);
                                    } else {
                                        slots[4] = slots[3];
                                    }
                                }
                            }
                        }
                    }
                }
                slots[3] = (slots[3] + INT64_C(1));
            }
            slots[0] = (slots[0] + INT64_C(1));
        }
        emit(INT64_C(-2));
        emit(slots[3]);
        emit(slots[4]);
        emit(slots[2]);
        if ((slots[2] == INT64_C(4)) != 0) {
            emit((slots[6] - slots[8]));
        } else {
            emit(INT64_C(0));
        }
    }
}

int main(void) {
    char *line = NULL;
    size_t capacity = 0;
    ssize_t bytes;
    while ((bytes = getline(&line, &capacity, stdin)) >= 0) {
        int64_t count = 0;
        char *cursor = line;
        if (memchr(line, '\0', (size_t)bytes)) return 2;
        while (*cursor) {
            while (isspace((unsigned char)*cursor)) ++cursor;
            if (!*cursor) break;
            char *end;
            errno = 0;
            intmax_t value = strtoimax(cursor, &end, 10);
            if (end == cursor || errno || count == LIMIT ||
                (*end && !isspace((unsigned char)*end))) return 2;
            input[count++] = value;
            cursor = end;
        }
        emitted = 0;
        evaluate(count);
        for (size_t i = 0; i < emitted; ++i) {
            if (i) putchar(' ');
            printf("%" PRId64, output[i]);
        }
        putchar('\n');
        if (fflush(stdout) != 0) return 2;
    }
    free(line);
    return ferror(stdin) ? 2 : 0;
}
