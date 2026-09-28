// Test-only socket redirect for paired_unfurl_fetch.py. The applications still
// validate 1.1.1.1 as a public address; only its designated fixture port is
// connected to loopback. No production binary links to this library.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <sys/socket.h>

int connect(int fd, const struct sockaddr *address, socklen_t length) {
    static int (*real_connect)(int, const struct sockaddr *, socklen_t);
    if (!real_connect) real_connect = dlsym(RTLD_NEXT, "connect");
    if (!real_connect) { errno = ENOSYS; return -1; }

    const char *setting = getenv("RUSTFIRE_UNFURL_FIXTURE_PORT");
    if (setting && address && address->sa_family == AF_INET &&
        length >= sizeof(struct sockaddr_in)) {
        char *end;
        long port = strtol(setting, &end, 10);
        const struct sockaddr_in *original = (const struct sockaddr_in *)address;
        if (*end == '\0' && port > 0 && port <= 65535 &&
            ntohs(original->sin_port) == port &&
            original->sin_addr.s_addr == htonl(0x01010101)) {
            struct sockaddr_in fixture = *original;
            fixture.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            return real_connect(fd, (const struct sockaddr *)&fixture, sizeof(fixture));
        }
    }
    return real_connect(fd, address, length);
}
