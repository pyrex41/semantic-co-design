/* Handwritten implementations for the four fixed benchmark fixtures. */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
enum { LIMIT = 65536 };
static int64_t input[LIMIT], output[LIMIT];
static size_t emitted;
static void emit(int64_t value) {
    if (emitted >= LIMIT) exit(2);
    output[emitted++] = value;
}

#if defined(BENCH_VM)
static void evaluate(int64_t count) {
    uint32_t acc = 17;
    for (int64_t i = 0; i < count; ++i) acc = acc * 31u + (uint32_t)input[i];
    emit(acc);
}
#elif defined(BENCH_QUERY)
static void evaluate(int64_t count) {
    int64_t left = input[1], right = input[2];
    const int64_t *l = input + 3, *r = l + 2 * left;
    for (int64_t i = 0; i < left; ++i)
        for (int64_t j = 0; j < right; ++j)
            if (l[2*i] == r[2*j]) {
                emit(l[2*i]); emit(l[2*i+1]); emit(r[2*j]); emit(r[2*j+1]);
            }
}
#elif defined(BENCH_RAILWAY)
static void evaluate(int64_t count) {
    int reservation = 0, train = 0, position = 0;
    for (int64_t i = 0; i < count; ++i) {
        int route = 1 + input[i] / 4, kind = input[i] % 4, verdict = 0;
        if (kind == 0 && !reservation) { reservation = position = route; verdict = 1; }
        if (kind == 1 && reservation == route && !train) { reservation = 0; verdict = 2; }
        if (kind == 2 && reservation == route && !train) { train = route; verdict = 3; }
        if (kind == 3 && train == route) { reservation = train = 0; verdict = 4; }
        emit(verdict); emit(reservation); emit(train); emit(position);
    }
}
#elif defined(BENCH_DECODER)
static void evaluate(int64_t count) {
    int pos = 0, error = -1, phase = 0, kind = 0, len = 0, used = 0, sum = 0;
    int payload[32];
    for (int64_t i = 0; i < count;) {
        int n = input[i++];
        while (n-- && i < count) {
            int byte = input[i++];
            if (error >= 0) continue;
            switch (phase) {
            case 0: if (byte == 165) phase++; else error = pos; break;
            case 1: if (byte == 1 || byte == 2) { kind = byte; sum = 7 + byte; phase++; }
                    else error = pos;
                    break;
            case 2: if (byte == 0) phase++; else error = pos; break;
            case 3: if (byte <= 32) { len = byte; used = 0; sum += byte; phase = len ? 4 : 5; }
                    else error = pos;
                    break;
            case 4: payload[used++] = byte; sum += byte; if (used == len) phase++; break;
            case 5: if (byte != (sum & 255)) error = pos;
                    else {
                        emit(-1); emit(kind); emit(len);
                        for (int j = 0; j < len; ++j) emit(payload[j]);
                        phase = 0;
                    }
                    break;
            }
            pos++;
        }
        emit(-2); emit(pos); emit(error); emit(phase); emit(phase == 4 ? len - used : 0);
    }
}
#endif
