// exec trace shim: logs start/end of every invocation as JSON lines.
// env: ATHREAD_TRACE_LOG (log file), ATHREAD_REAL_PATHS (colon-separated dirs to resolve real binary)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <sys/wait.h>
#include <sys/stat.h>

static void jesc(int fd, const char *s) {
    for (const char *p = s; *p; p++) {
        char b[8];
        int n = 0;
        switch (*p) {
            case '"':  n = snprintf(b, 8, "\\\""); break;
            case '\\': n = snprintf(b, 8, "\\\\"); break;
            case '\n': n = snprintf(b, 8, "\\n"); break;
            default:
                if ((unsigned char)*p < 0x20) n = snprintf(b, 8, "\\u%04x", *p);
                else { write(fd, p, 1); continue; }
        }
        write(fd, b, n);
    }
}

static long long nowns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

int main(int argc, char **argv) {
    const char *log = getenv("ATHREAD_TRACE_LOG");
    if (!log) { fprintf(stderr, "shim: ATHREAD_TRACE_LOG unset\n"); return 127; }
    const char *realdirs = getenv("ATHREAD_REAL_PATHS");
    if (!realdirs) realdirs = "/usr/bin:/bin:/usr/local/bin";

    char *name = strrchr(argv[0], '/');
    name = name ? name + 1 : argv[0];

    char real[4096] = "";
    char dirs[4096];
    snprintf(dirs, sizeof dirs, "%s", realdirs);
    for (char *d = strtok(dirs, ":"); d; d = strtok(NULL, ":")) {
        char cand[4096];
        snprintf(cand, sizeof cand, "%s/%s", d, name);
        struct stat st;
        if (stat(cand, &st) == 0 && access(cand, X_OK) == 0 &&
            !strstr(cand, "/trace/shim")) {
            snprintf(real, sizeof real, "%s", cand);
            break;
        }
    }
    if (!real[0]) { fprintf(stderr, "shim: real %s not found\n", name); return 127; }

    int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) { perror("open log"); return 127; }

    long long t0 = nowns();
    char cwd[4096];
    if (!getcwd(cwd, sizeof cwd)) snprintf(cwd, sizeof cwd, "?");
    pid_t self = getpid();

    dprintf(fd, "{\"ev\":\"start\",\"ts\":%lld,\"pid\":%d,\"bin\":\"", t0, self);
    jesc(fd, name);
    dprintf(fd, "\",\"argv\":[");
    for (int i = 0; i < argc; i++) {
        dprintf(fd, "%s\"", i ? "," : "");
        jesc(fd, argv[i]);
        dprintf(fd, "\"");
    }
    dprintf(fd, "],\"cwd\":\"");
    jesc(fd, cwd);
    dprintf(fd, "\",\"venv\":%d}\n", getenv("VIRTUAL_ENV") ? 1 : 0);

    pid_t pid = fork();
    if (pid == 0) {
        execv(real, argv);
        perror("execv");
        _exit(127);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    long long dur = nowns() - t0;
    int rc = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
    dprintf(fd, "{\"ev\":\"end\",\"pid\":%d,\"ts\":%lld,\"dur_ms\":%.3f,\"rc\":%d}\n",
            self, nowns(), dur / 1e6, rc);
    close(fd);
    return rc;
}
