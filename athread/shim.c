// athread python shim (v0.1)
// Transparent `python3` replacement: eligible invocations are served by
// the warm athreadd daemon (fork+CoW); everything else falls back to the
// real interpreter. Fail-open: any daemon problem -> exec real python.
//
// Eligible forms:  python3 [-c CODE | -m MOD | SCRIPT | (stdin)]
// Fallback:        VIRTUAL_ENV/PYTHONPATH/CONDA_PREFIX set, unknown flags,
//                  daemon unreachable, script file missing.
//
// env: ATHREAD_SOCK        daemon socket path (unset -> fallback)
//      ATHREAD_REAL_PYTHON real interpreter for fallback (default /usr/bin/python3)
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <sys/un.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>

#define MAX_ENV (1 << 20)
// MAX_ARGV is a protocol sanity bound only; exceeding it falls back to the
// real interpreter rather than silently truncating argv.
#define MAX_ARGV 4096

static const char *REAL_PYTHON = "/usr/bin/python3";
static const char *SOCK_PATH = NULL;
static pid_t child_pid = -1;

static void forward_signal(int sig) {
    if (child_pid > 0) kill(child_pid, sig);
}

static void jesc(int fd, const char *s) {
    char b[8];
    for (const char *p = s; *p; p++) {
        int n = 0;
        switch (*p) {
            case '"':  n = snprintf(b, 8, "\\\""); break;
            case '\\': n = snprintf(b, 8, "\\\\"); break;
            case '\n': n = snprintf(b, 8, "\\n"); break;
            default:
                if ((unsigned char)*p < 0x20) n = snprintf(b, 8, "\\u%04x", *p);
                else { if (write(fd, p, 1) < 0) {} continue; }
        }
        if (write(fd, b, n) < 0) return;
    }
}

static int eligible(int argc, char **argv) {
    if (getenv("VIRTUAL_ENV") || getenv("PYTHONPATH") || getenv("CONDA_PREFIX"))
        return 0;
    if (argc < 2) return 1;                       // stdin
    const char *a1 = argv[1];
    if (!strcmp(a1, "-c")) return argc >= 3;
    if (!strcmp(a1, "-m")) return argc >= 3;
    if (a1[0] == '-') return 0;                   // unknown flags -> fallback
    struct stat st;
    return stat(a1, &st) == 0 && S_ISREG(st.st_mode);
}

static void exec_real(int argc, char **argv) {
    argv[0] = (char *)REAL_PYTHON;
    execv(REAL_PYTHON, argv);
    perror("athread shim: execv fallback failed");
    _exit(127);
}

int main(int argc, char **argv) {
    const char *s = getenv("ATHREAD_SOCK");
    const char *rp = getenv("ATHREAD_REAL_PYTHON");
    if (rp) REAL_PYTHON = rp;
    if (s) SOCK_PATH = s;
    if (!SOCK_PATH || !eligible(argc, argv))
        exec_real(argc, argv);

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) exec_real(argc, argv);
    // 2s timeout for connect only; after spawn the daemon answers exactly
    // when the child exits, which may be arbitrarily far in the future.
    struct timeval tv = {2, 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    struct sockaddr_un sa = {0};
    sa.sun_family = AF_UNIX;
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", SOCK_PATH);
    if (connect(fd, (struct sockaddr *)&sa, sizeof sa) < 0)
        exec_real(argc, argv);

    if (argc > MAX_ARGV)                         // never truncate argv
        exec_real(argc, argv);

    // build request: {"cmd":"run","argv":[...],"env":[...],"cwd":"..."}
    int jfd = open("/dev/null", O_RDWR);
    (void)jfd;
    char tmpl[] = "/tmp/athread_shim_reqXXXXXX";
    int tfd = mkstemp(tmpl);
    if (tfd < 0) exec_real(argc, argv);
    unlink(tmpl);
    dprintf(tfd, "{\"cmd\":\"run\",\"argv\":[");
    for (int i = 0; i < argc && i < MAX_ARGV; i++) {
        dprintf(tfd, "%s\"", i ? "," : "");
        jesc(tfd, argv[i]);
        dprintf(tfd, "\"");
    }
    dprintf(tfd, "],\"env\":[");
    extern char **environ;
    int first = 1;
    for (char **e = environ; *e && first >= 0; e++) {
        // strip only the vars the shim itself consumes, never user env
        if (!strncmp(*e, "ATHREAD_SOCK=", 13) ||
            !strncmp(*e, "ATHREAD_REAL_PYTHON=", 20))
            continue;
        dprintf(tfd, "%s\"", first ? "" : ",");
        first = 0;
        jesc(tfd, *e);
        dprintf(tfd, "\"");
        if (lseek(tfd, 0, SEEK_CUR) > MAX_ENV) break;
    }
    dprintf(tfd, "],\"cwd\":\"");
    char cwd[4096];
    if (!getcwd(cwd, sizeof cwd)) snprintf(cwd, sizeof cwd, "/");
    jesc(tfd, cwd);
    dprintf(tfd, "\"}\n");

    // send file + fds 0,1,2 in one message
    off_t len = lseek(tfd, 0, SEEK_END);
    lseek(tfd, 0, SEEK_SET);
    if (len < 0 || len >= 60000)                // oversized request -> fallback
        exec_real(argc, argv);
    char buf[65536];
    ssize_t n = read(tfd, buf, sizeof buf - 1);
    close(tfd);
    if (n <= 0) exec_real(argc, argv);
    buf[n] = 0; (void)len;

    int fds[3] = {0, 1, 2};
    struct iovec iov = {buf, (size_t)n};
    char cbuf[CMSG_SPACE(sizeof fds)];
    struct msghdr mh = {0};
    mh.msg_iov = &iov;
    mh.msg_iovlen = 1;
    mh.msg_control = cbuf;
    mh.msg_controllen = sizeof cbuf;
    struct cmsghdr *cm = CMSG_FIRSTHDR(&mh);
    cm->cmsg_level = SOL_SOCKET;
    cm->cmsg_type = SCM_RIGHTS;
    cm->cmsg_len = CMSG_LEN(sizeof fds);
    memcpy(CMSG_DATA(cm), fds, sizeof fds);
    mh.msg_controllen = cm->cmsg_len;
    if (sendmsg(fd, &mh, 0) < 0)
        exec_real(argc, argv);

    signal(SIGINT, forward_signal);
    signal(SIGTERM, forward_signal);
    signal(SIGHUP, forward_signal);
    signal(SIGQUIT, forward_signal);

    // read lines until {"ev":"exit","rc":N}
    FILE *f = fdopen(fd, "r");
    if (!f) exec_real(argc, argv);
    char line[1024];
    int rc = 125;
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, "\"spawn\"")) {
            char *p = strstr(line, "\"pid\":");
            if (p) child_pid = atoi(p + 6);
        } else if (strstr(line, "\"exit\"")) {
            char *p = strstr(line, "\"rc\":");
            if (p) rc = atoi(p + 5);
            break;
        }
    }
    fclose(f);
    return rc;
}
