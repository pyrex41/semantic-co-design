/* The part of the Solo5 API that driver.c uses, as an ordinary host process.

       node.host [--block:disk=FILE] [--block-sector-size:disk=N] [--net:net=DIR] [--] ARGS...

   This is not Solo5 and nothing run through it is evidence about Solo5. It exists so
   that the same driver.c and the same node.c can be run and compared with the simulator
   where no Solo5 tender runs (macOS, for one). The options are spelled like the
   tender's, and each call does what solo5-spt does for it:

     block device   a file opened O_RDWR; one pread or pwrite of one block, nothing synced
     net device     a datagram socket bound to DIR/node; a frame is one datagram, sent to
                    DIR/client and dropped when nobody listens (a tap with no carrier)
     clock          CLOCK_MONOTONIC
     yield          poll on the socket until the deadline
     console        standard output */
#define _DARWIN_C_SOURCE
#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "solo5.h"

static struct { char name[64]; int fd; off_t capacity; off_t block_size; int acquired; } block =
    {"", -1, 0, 512, 0};
static struct { int fd; char name[64]; struct sockaddr_un peer; int acquired; } net = {.fd = -1};

static void die(const char *what, const char *detail) {
    fprintf(stderr, "host: %s: %s\n", what, detail);
    exit(2);
}

void solo5_exit(int status) { _exit(status); }

void solo5_abort(void) { _exit(SOLO5_EXIT_ABORT); }

void solo5_console_write(const char *buf, size_t size) {
    while (size) {
        ssize_t n = write(1, buf, size);
        if (n <= 0) return;                     /* best effort, as on Solo5 */
        buf += n;
        size -= (size_t)n;
    }
}

solo5_time_t solo5_clock_monotonic(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (solo5_time_t)ts.tv_sec * 1000000000u + (solo5_time_t)ts.tv_nsec;
}

void solo5_yield(solo5_time_t deadline, solo5_handle_set_t *ready_set) {
    solo5_time_t current = solo5_clock_monotonic();
    int timeout = deadline > current ? (int)((deadline - current + 999999u) / 1000000u) : 0;
    struct pollfd fds = {net.fd, POLLIN, 0};
    int ready = poll(&fds, net.fd >= 0 ? 1 : 0, timeout);
    if (ready_set != NULL) *ready_set = ready > 0 ? 1 : 0;
}

solo5_result_t solo5_block_acquire(const char *name, solo5_handle_t *handle,
        struct solo5_block_info *info) {
    if (block.fd < 0 || block.acquired || strcmp(name, block.name) != 0) return SOLO5_R_EINVAL;
    block.acquired = 1;
    *handle = 0;
    info->capacity = (solo5_off_t)block.capacity;
    info->block_size = (solo5_off_t)block.block_size;
    return SOLO5_R_OK;
}

static int block_ok(solo5_handle_t handle, solo5_off_t offset, size_t size) {
    return handle == 0 && block.acquired && size == (size_t)block.block_size
        && offset % (solo5_off_t)block.block_size == 0
        && offset <= (solo5_off_t)(block.capacity - block.block_size);
}

solo5_result_t solo5_block_write(solo5_handle_t handle, solo5_off_t offset, const uint8_t *buf,
        size_t size) {
    if (!block_ok(handle, offset, size)) return SOLO5_R_EINVAL;
    return pwrite(block.fd, buf, size, (off_t)offset) == (ssize_t)size ? SOLO5_R_OK : SOLO5_R_EUNSPEC;
}

solo5_result_t solo5_block_read(solo5_handle_t handle, solo5_off_t offset, uint8_t *buf,
        size_t size) {
    if (!block_ok(handle, offset, size)) return SOLO5_R_EINVAL;
    return pread(block.fd, buf, size, (off_t)offset) == (ssize_t)size ? SOLO5_R_OK : SOLO5_R_EUNSPEC;
}

solo5_result_t solo5_net_acquire(const char *name, solo5_handle_t *handle,
        struct solo5_net_info *info) {
    static const uint8_t mac[SOLO5_NET_ALEN] = {0x02, 0, 0, 0, 0, 0x01};
    if (net.fd < 0 || net.acquired || strcmp(name, net.name) != 0) return SOLO5_R_EINVAL;
    net.acquired = 1;
    *handle = 1;
    memcpy(info->mac_address, mac, sizeof mac);
    info->mtu = 1500;
    return SOLO5_R_OK;
}

solo5_result_t solo5_net_write(solo5_handle_t handle, const uint8_t *buf, size_t size) {
    if (handle != 1 || !net.acquired) return SOLO5_R_EINVAL;
    ssize_t n = sendto(net.fd, buf, size, 0, (const struct sockaddr *)&net.peer, sizeof net.peer);
    return n == (ssize_t)size ? SOLO5_R_OK : SOLO5_R_EUNSPEC;
}

solo5_result_t solo5_net_read(solo5_handle_t handle, uint8_t *buf, size_t size,
        size_t *read_size) {
    if (handle != 1 || !net.acquired) return SOLO5_R_EINVAL;
    ssize_t n = recv(net.fd, buf, size, 0);
    if (n < 0) return errno == EAGAIN || errno == EWOULDBLOCK ? SOLO5_R_AGAIN : SOLO5_R_EUNSPEC;
    *read_size = (size_t)n;
    return SOLO5_R_OK;
}

static const char *option(const char *arg, const char *prefix, char *name, size_t size) {
    size_t n = strlen(prefix);
    const char *equals = strchr(arg, '=');
    if (strncmp(arg, prefix, n) != 0 || equals == NULL || (size_t)(equals - arg - n) >= size)
        return NULL;
    memcpy(name, arg + n, (size_t)(equals - arg - n));
    name[equals - arg - n] = 0;
    return equals + 1;
}

static void address(struct sockaddr_un *out, const char *directory, const char *leaf) {
    memset(out, 0, sizeof *out);
    out->sun_family = AF_UNIX;
    if (snprintf(out->sun_path, sizeof out->sun_path, "%s/%s", directory, leaf)
            >= (int)sizeof out->sun_path)
        die("socket path too long", directory);
}

int main(int argc, char **argv) {
    static char cmdline[4096];
    char name[64];
    const char *value;
    int at = 1;
    for (; at < argc && argv[at][0] == '-'; ++at) {
        if (strcmp(argv[at], "--") == 0) {
            ++at;
            break;
        }
        if ((value = option(argv[at], "--block:", block.name, sizeof block.name)) != NULL) {
            if ((block.fd = open(value, O_RDWR)) < 0) die("cannot open block file", value);
            block.capacity = lseek(block.fd, 0, SEEK_END);
        } else if ((value = option(argv[at], "--block-sector-size:", name, sizeof name)) != NULL) {
            block.block_size = atol(value);
        } else if ((value = option(argv[at], "--net:", net.name, sizeof net.name)) != NULL) {
            struct sockaddr_un self;
            address(&self, value, "node");
            address(&net.peer, value, "client");
            unlink(self.sun_path);
            net.fd = socket(AF_UNIX, SOCK_DGRAM, 0);
            if (net.fd < 0 || bind(net.fd, (struct sockaddr *)&self, sizeof self) != 0
                    || fcntl(net.fd, F_SETFL, O_NONBLOCK) != 0)
                die("cannot bind", self.sun_path);
        } else {
            die("unknown option", argv[at]);
        }
    }
    if (block.fd >= 0 && (block.block_size < 512 || block.block_size & (block.block_size - 1)
                          || block.capacity < block.block_size
                          || block.capacity % block.block_size))
        die("block file", "size must be a positive multiple of the sector size");
    for (; at < argc; ++at) {
        if (strlen(cmdline) + strlen(argv[at]) + 2 > sizeof cmdline) die("arguments", "too long");
        if (cmdline[0]) strcat(cmdline, " ");
        strcat(cmdline, argv[at]);
    }
    struct solo5_start_info info = {cmdline, 0, 0};
    solo5_exit(solo5_app_main(&info));
}
