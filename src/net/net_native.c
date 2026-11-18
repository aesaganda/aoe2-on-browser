/*
 * net_native.c - the relay over TCP, for the native build: AOE2_RELAY=host:port
 * (default 127.0.0.1:8644). Each frame is preceded by its u32 length; a reader
 * thread hands whole frames to net_inbound.
 */
#ifndef __EMSCRIPTEN__
#include <arpa/inet.h>
#include <netdb.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "net.h"

static int g_fd = -1;
static pthread_mutex_t g_send_lock = PTHREAD_MUTEX_INITIALIZER;

static int read_all(int fd, uint8_t *b, size_t n) {
    for (size_t got = 0; got < n;) {
        ssize_t r = read(fd, b + got, n - got);
        if (r <= 0) return -1;
        got += (size_t)r;
    }
    return 0;
}

static void *reader(void *arg) {
    (void)arg;
    for (;;) {
        uint8_t h[4];
        if (read_all(g_fd, h, 4)) break;
        uint32_t n = (uint32_t)h[0] | (uint32_t)h[1] << 8 | (uint32_t)h[2] << 16 | (uint32_t)h[3] << 24;
        if (n > (1u << 22)) break;
        uint8_t *b = malloc(n ? n : 1);
        if (!b || read_all(g_fd, b, n)) { free(b); break; }
        net_inbound(b, n);
        free(b);
    }
    fprintf(stderr, "[net] the relay closed the connection\n");
    net_closed();
    return NULL;
}

int net_open(void) {
    if (g_fd >= 0) return 0;
    const char *e = getenv("AOE2_RELAY");
    char host[256] = "127.0.0.1", port[16] = "8644";
    if (e && *e) {
        const char *c = strrchr(e, ':');
        snprintf(host, sizeof host, "%.*s", c ? (int)(c - e) : (int)strlen(e), e);
        if (c) snprintf(port, sizeof port, "%s", c + 1);
    }
    struct addrinfo hints = { 0 }, *ai = NULL;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, port, &hints, &ai) != 0 || !ai) { fprintf(stderr, "[net] cannot resolve %s\n", host); return -1; }
    int fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (fd < 0 || connect(fd, ai->ai_addr, ai->ai_addrlen) != 0) {
        fprintf(stderr, "[net] no relay at %s:%s\n", host, port);
        if (fd >= 0) close(fd);
        freeaddrinfo(ai);
        return -1;
    }
    freeaddrinfo(ai);
    g_fd = fd;
    pthread_t t;
    pthread_create(&t, NULL, reader, NULL);
    pthread_detach(t);
    fprintf(stderr, "[net] connected to the relay at %s:%s\n", host, port);
    return 0;
}

void net_send(const uint8_t *b, size_t n) {
    if (g_fd < 0) return;
    uint8_t h[4] = { (uint8_t)n, (uint8_t)(n >> 8), (uint8_t)(n >> 16), (uint8_t)(n >> 24) };
    pthread_mutex_lock(&g_send_lock);
    if (write(g_fd, h, 4) != 4 || write(g_fd, b, n) != (ssize_t)n) net_closed();
    pthread_mutex_unlock(&g_send_lock);
}
#endif
