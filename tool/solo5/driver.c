/* The effect shim of a compiled node, written against the Solo5 API (solo5.h).

   It does three things and no others:

   1. turns boot, the result of a block read, the return of a block write, a due
      timer, a clock reading and an arriving packet into one event vector each;
   2. calls the compiled step (node_step, from node.c) with the state and one event;
   3. performs the effects the step returned, in the order it returned them: a block
      write, a block read, a packet, a timer entry, a clock reading.

   Everything a guarantee could rest on is in the specification: what is written, when a
   reply is sent, what a restart reads back, what a retry means. The loop keeps no
   record of requests, never repeats an effect and never reorders two of them. Whatever
   it cannot do (a row outside the device, a failed write, a step with no result) stops
   the node; that is a crash, which the specification already has to survive.

   One thing it refuses although the device could do it: a read of a row that the same
   step wrote earlier. The block write is synchronous, so the device would answer with
   the new row. The simulator has no schedule in which a write becomes durable between
   two effects of one step, so that answer is one no simulated run contains, and an
   invariant checked in simulation would say nothing about it. The node stops instead,
   which is a crash at that effect and is simulated.

   The same file is linked twice: with the Solo5 bindings into a unikernel, and with
   host.c, which provides the same API over a file and a datagram socket.

   Console lines starting with '@' are the record of the run (see README.md):

     @boot NAME NODE NODES CAPACITY BLOCK      devices acquired
     @init S...                                the state node_init returned
     @event N T CODE F...                      the N-th event since boot, at tick T
     @step O...                                what node_step returned for it
     @trap CODE                                node_step had no result; the node stops
     @did K                                    effect K of that step has been performed
     @idle                                     nothing left to do; waiting
     @hold N K                                 stopped after K effects of event N (see hold=)
     @skip WHY LEN                             a frame that is not a request was ignored
     @fail WHY                                 the node stops; inside a step, before the
                                               effect that follows the last @did

   Boot arguments: node=N nodes=M (passed to node_init), hold=N.K (stop for good after
   K effects of the N-th event, so that a test can kill the tender at a known point). */
#include "solo5.h"
#include "node_tables.h"

int64_t node_init(int64_t node, int64_t nodes, int64_t *out, int64_t capacity);
int64_t node_step(const int64_t *in, int64_t count, int64_t *out, int64_t capacity);

#define TICK_NS UINT64_C(1000000)      /* one unit of specification time: a millisecond */
#define ETHERTYPE 0x88B5               /* IEEE 802 local experimental ethertype 1 */
#define NUM_MAX INT64_C(2305843009213693951)
enum { QUEUE = 64, TIMERS = 16, BLOCK_MAX = 4096, FRAME_MAX = 1514, FRAME_MIN = 60, LINE = 4096,
       VECTOR = 1 + EVENT_WIDTH > 4 ? 1 + EVENT_WIDTH : 4 };       /* room for any event */

#define BODY (SOLO5_NET_HLEN + 8)      /* where the integers of a frame start */

typedef char payload_fits[8 * (1 + (EVENT_WIDTH > EFFECT_WIDTH ? EVENT_WIDTH : EFFECT_WIDTH))
                          <= FRAME_MAX - BODY ? 1 : -1];

static int64_t state[STATE_WIDTH], input[INPUT_WIDTH], output[OUTPUT_CAPACITY];
static int64_t queue[QUEUE][VECTOR];               /* completions, oldest first */
static int queue_head, queue_count;
static struct { int armed; int64_t id; int64_t due; } timers[TIMERS];
static solo5_handle_t disk, net;
static struct solo5_block_info disk_info;
static struct solo5_net_info net_info;
static uint8_t block[BLOCK_MAX], frame[FRAME_MAX];
static int64_t events, now, hold_event, hold_count;
static int64_t written[MAX_EFFECTS + 1];           /* keys the step in progress has written */
static int written_count;
static int busy = 1;

/* ---- console */

static char line[LINE];
static int line_length;

static void text(const char *s) {
    while (*s && line_length < LINE - 1) line[line_length++] = *s++;
}

static void number(int64_t value) {
    char digits[24];
    int n = 0;
    uint64_t magnitude = value < 0 ? 0 - (uint64_t)value : (uint64_t)value;
    do { digits[n++] = (char)('0' + magnitude % 10); magnitude /= 10; } while (magnitude);
    if (line_length < LINE - 1) line[line_length++] = ' ';
    if (value < 0 && line_length < LINE - 1) line[line_length++] = '-';
    while (n && line_length < LINE - 1) line[line_length++] = digits[--n];
}

static void numbers(const int64_t *values, int64_t count) {
    for (int64_t i = 0; i < count; ++i) number(values[i]);
}

static void end(void) {
    line[line_length++] = '\n';
    solo5_console_write(line, (size_t)line_length);
    line_length = 0;
}

static void fail(const char *why) {
    text("@fail ");
    text(why);
    end();
    solo5_abort();
}

/* Stop for good, without performing anything further. A test kills the tender here. */
static void hold(int64_t done) {
    text("@hold");
    number(events);
    number(done);
    end();
    for (;;) solo5_yield(solo5_clock_monotonic() + UINT64_C(1000000000), NULL);
}

/* ---- integers on the wire and on the disk: 64-bit two's complement, little endian */

static void pack(uint8_t *to, int64_t value) {
    for (int i = 0; i < 8; ++i) to[i] = (uint8_t)((uint64_t)value >> (8 * i));
}

static int64_t unpack(const uint8_t *from) {
    uint64_t value = 0;
    for (int i = 0; i < 8; ++i) value |= (uint64_t)from[i] << (8 * i);
    return (int64_t)value;
}

static const struct kind *find(const struct kind *kinds, int count, int64_t code) {
    for (int i = 0; i < count; ++i)
        if (kinds[i].code == code) return &kinds[i];
    return NULL;
}

/* ---- completions: results of the node's own effects, waiting to become events */

static void complete(const int64_t *event, int width) {
    if (queue_count == QUEUE) fail("completion-queue-full");
    int64_t *slot = queue[(queue_head + queue_count++) % QUEUE];
    for (int i = 0; i < VECTOR; ++i) slot[i] = i < width ? event[i] : 0;
}

/* ---- effects */

static uint64_t block_offset(int64_t key) {
    if (8 * (1 + ROW_WIDTH) > (int64_t)disk_info.block_size) fail("row-wider-than-block");
    if (key < 0 || (uint64_t)key >= disk_info.capacity / disk_info.block_size) fail("disk-key");
    return (uint64_t)key * disk_info.block_size;
}

/* One row is one block: the word 1, then the row's integers. A block of zeros is no row. */
static void disk_write(const int64_t *fields, int width) {
    uint64_t offset = block_offset(fields[1]);
    for (uint64_t i = 0; i < disk_info.block_size; ++i) block[i] = 0;
    pack(block, 1);
    for (int i = 2; i < width; ++i) pack(block + 8 * (i - 1), fields[i]);
    if (solo5_block_write(disk, offset, block, disk_info.block_size) != SOLO5_R_OK)
        fail("block-write");
    if (written_count == MAX_EFFECTS) fail("more-effects-than-declared");
    written[written_count++] = fields[1];
    if (EV_DISK_ACK) {
        int64_t event[2] = {EV_DISK_ACK, fields[0]};
        complete(event, 2);
    }
}

static void disk_read(const int64_t *fields) {
    const struct kind *result = find(EVENTS, NEVENTS, EV_DISK_READ_RESULT);
    int64_t event[VECTOR];
    if (result == NULL) fail("no-read-result-event");
    for (int i = 0; i < written_count; ++i)      /* see the note at the top of this file */
        if (written[i] == fields[1]) fail("read-after-write");
    if (solo5_block_read(disk, block_offset(fields[1]), block, disk_info.block_size) != SOLO5_R_OK)
        fail("block-read");
    int64_t present = unpack(block);
    if (present != 0 && present != 1) fail("unreadable-row");
    event[0] = EV_DISK_READ_RESULT;
    event[1] = fields[0];
    event[2] = fields[1];
    event[3] = present;
    for (int i = 3; i < result->width; ++i)      /* a missing row reads as the default row */
        event[1 + i] = present ? unpack(block + 8 * (i - 2)) : result->leaves[i] == 's';
    complete(event, 1 + result->width);
}

/* A packet to whoever listens. After the Ethernet header: how many integers follow,
   then the effect's tag code and fields. Requests arrive in the same layout. */
static void packet(const struct kind *kind, const int64_t *fields) {
    size_t size = BODY + 8 * (size_t)(1 + kind->width);
    for (size_t i = 0; i < FRAME_MIN; ++i) frame[i] = 0;
    for (int i = 0; i < SOLO5_NET_ALEN; ++i) {
        frame[i] = 0xff;
        frame[SOLO5_NET_ALEN + i] = net_info.mac_address[i];
    }
    frame[12] = ETHERTYPE >> 8;
    frame[13] = ETHERTYPE & 0xff;
    pack(frame + SOLO5_NET_HLEN, 1 + kind->width);
    pack(frame + BODY, kind->code);
    for (int i = 0; i < kind->width; ++i) pack(frame + BODY + 8 * (1 + i), fields[i]);
    /* Solo5 drops a packet it cannot send; so may any network. */
    (void)solo5_net_write(net, frame, size < FRAME_MIN ? FRAME_MIN : size);
}

static void timer_clear(int64_t id) {
    for (int i = 0; i < TIMERS; ++i)
        if (timers[i].armed && timers[i].id == id) timers[i].armed = 0;
}

static void timer_set(int64_t id, int64_t delay) {
    timer_clear(id);
    for (int i = 0; i < TIMERS; ++i)
        if (!timers[i].armed) {
            timers[i].armed = 1;
            timers[i].id = id;
            timers[i].due = now + (delay > 0 ? delay : 0);
            return;
        }
    fail("timer-table-full");
}

static void perform(const struct kind *kind, const int64_t *fields) {
    switch (kind->role) {
    case R_DISK_WRITE: disk_write(fields, kind->width); break;
    case R_DISK_READ: disk_read(fields); break;
    case R_OUTPUT: packet(kind, fields); break;
    case R_TIMER_SET: timer_set(fields[0], fields[1]); break;
    case R_TIMER_CANCEL: timer_clear(fields[0]); break;
    case R_CLOCK_READ: {
        int64_t event[3] = {EV_CLOCK, fields[0], now};
        complete(event, 3);
        break;
    }
    default: fail("effect-role");
    }
}

/* ---- one event: step, then the effects in order */

static void handle(const int64_t *event, int width) {
    ++events;
    busy = 1;
    now = (int64_t)(solo5_clock_monotonic() / TICK_NS);
    for (int i = 0; i < STATE_WIDTH; ++i) input[i] = state[i];
    for (int i = 0; i < 1 + EVENT_WIDTH; ++i) input[STATE_WIDTH + i] = i < width ? event[i] : 0;
    text("@event");
    number(events);
    number(now);
    numbers(event, width);
    end();
    int64_t written = node_step(input, INPUT_WIDTH, output, OUTPUT_CAPACITY);
    if (written < 0) {
        text("@trap");
        number(written);
        end();
        solo5_abort();
    }
    text("@step");
    numbers(output, written);
    end();
    for (int i = 0; i < STATE_WIDTH; ++i) state[i] = output[i];
    int64_t count = output[STATE_WIDTH], at = STATE_WIDTH + 1;
    written_count = 0;
    for (int64_t k = 0; k < count; ++k) {
        if (events == hold_event && k == hold_count) hold(k);
        const struct kind *kind = find(EFFECTS, NEFFECTS, output[at]);
        if (kind == NULL) fail("effect-tag");
        perform(kind, output + at + 1);
        text("@did");
        number(k);
        end();
        at += 1 + kind->width;
    }
    if (events == hold_event && hold_count >= count) hold(count);
}

/* ---- sources of events */

static int due_timer(void) {
    int best = -1;
    for (int i = 0; i < TIMERS; ++i)
        if (timers[i].armed && (best < 0 || timers[i].due < timers[best].due)) best = i;
    return best;
}

/* A frame is a request when it carries our ethertype, the tag of a request event and
   fields of the declared kinds. Anything else is not for the node. */
static void receive(size_t size) {
    int64_t event[1 + EVENT_WIDTH];
    const char *why = "ethertype";
    if (size >= BODY + 8 && frame[12] == ETHERTYPE >> 8 && frame[13] == (ETHERTYPE & 0xff)) {
        const struct kind *kind = find(EVENTS, NEVENTS, unpack(frame + BODY));
        why = "tag";
        if (kind != NULL && kind->role == R_REQUEST) {
            why = "length";
            if (unpack(frame + SOLO5_NET_HLEN) == 1 + kind->width
                    && size >= BODY + 8 * (size_t)(1 + kind->width)) {
                int valid = 1;
                event[0] = kind->code;
                for (int i = 0; i < kind->width; ++i) {
                    int64_t v = unpack(frame + BODY + 8 * (1 + i));
                    char leaf = kind->leaves[i];
                    if (leaf == 'n' ? v < -NUM_MAX - 1 || v > NUM_MAX
                        : leaf == 'b' ? v != 0 && v != 1 : v < 1 || v > SYMBOLS) valid = 0;
                    event[1 + i] = v;
                }
                why = "field";
                if (valid) {
                    handle(event, 1 + kind->width);
                    return;
                }
            }
        }
    }
    text("@skip ");
    text(why);
    number((int64_t)size);
    end();
}

static int64_t argument(const char *cmdline, const char *name, int64_t otherwise, int64_t *second) {
    for (const char *at = cmdline; *at; ++at) {
        const char *a = at, *b = name;
        while (*b && *a == *b) { ++a; ++b; }
        if (*b == 0 && *a == '=' && (at == cmdline || at[-1] == ' ')) {
            int64_t value = 0;
            for (++a; *a >= '0' && *a <= '9'; ++a) value = value * 10 + (*a - '0');
            if (second != NULL && *a == '.') {
                *second = 0;
                for (++a; *a >= '0' && *a <= '9'; ++a) *second = *second * 10 + (*a - '0');
            }
            return value;
        }
    }
    return otherwise;
}

int solo5_app_main(const struct solo5_start_info *info) {
    int64_t node = argument(info->cmdline, "node", 0, NULL);
    int64_t nodes = argument(info->cmdline, "nodes", 1, NULL);
    hold_event = argument(info->cmdline, "hold", 0, &hold_count);
    if (USES_DISK) {
        if (solo5_block_acquire("disk", &disk, &disk_info) != SOLO5_R_OK) fail("no-disk");
        if (disk_info.block_size > BLOCK_MAX) fail("block-size");
    }
    if (USES_NET && solo5_net_acquire("net", &net, &net_info) != SOLO5_R_OK) fail("no-net");
    text("@boot " NODE_NAME);
    number(node);
    number(nodes);
    number((int64_t)disk_info.capacity);
    number((int64_t)disk_info.block_size);
    end();
    if (node_init(node, nodes, state, STATE_WIDTH) != STATE_WIDTH) fail("init");
    text("@init");
    numbers(state, STATE_WIDTH);
    end();
    if (EV_RESTART) {
        int64_t event[1] = {EV_RESTART};
        handle(event, 1);
    }
    for (;;) {
        if (queue_count) {                       /* a completion of the node's own effect */
            int64_t event[VECTOR];
            const int64_t *slot = queue[queue_head];
            const struct kind *kind = find(EVENTS, NEVENTS, slot[0]);
            for (int i = 0; i < VECTOR; ++i) event[i] = slot[i];
            queue_head = (queue_head + 1) % QUEUE;
            --queue_count;
            handle(event, 1 + kind->width);
            continue;
        }
        int timer = due_timer();
        solo5_time_t clock = solo5_clock_monotonic();
        if (timer >= 0 && (int64_t)(clock / TICK_NS) >= timers[timer].due) {
            int64_t event[2] = {EV_TIMER, timers[timer].id};
            timers[timer].armed = 0;
            handle(event, 2);
            continue;
        }
        size_t size = 0;
        if (USES_NET && solo5_net_read(net, frame, sizeof frame, &size) == SOLO5_R_OK) {
            receive(size);
            continue;
        }
        if (busy) {
            busy = 0;
            text("@idle");
            end();
        }
        /* Wake for the earliest timer when it is due within a second, else in a second.
           The comparison is in ticks: a due time in nanoseconds can exceed 64 bits. */
        solo5_time_t deadline = clock + UINT64_C(1000000000);
        if (timer >= 0 && (uint64_t)timers[timer].due < deadline / TICK_NS)
            deadline = (uint64_t)timers[timer].due * TICK_NS;
        solo5_yield(deadline, NULL);
    }
}
