// exec trace shim: logs start/end of every invocation as JSON lines.
// env: ATHREAD_TRACE_LOG (log file), ATHREAD_REAL_PATHS (colon-separated dirs to resolve real binary)
//
// Each record is built in memory and written with a SINGLE write() call:
// with O_APPEND one write is atomic, so concurrent shims never interleave.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <sys/wait.h>
#include <sys/stat.h>

static void jesc_f(FILE *f, const char *s) {
    for (const char *p = s; *p; p++) {
        switch (*p) {
            case '"':  fputs("\\\"", f); break;
            case '\\': fputs("\\\\", f); break;
            case '\n': fputs("\\n", f); break;
            default:
                if ((unsigned char)*p < 0x20) fprintf(f, "\\u%04x", *p);
                else fputc(*p, f);
        }
    }
}

static long long nowns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void log_line(int fd, long long t0, const char *ev, pid_t self,
                     const char *name, char **argv, int argc, const char *cwd,
                     double dur_ms, int rc) {
    char *buf = NULL;
    size_t sz = 0;
    FILE *f = open_memstream(&buf, &sz);
    if (!f) return;
    if (!strcmp(ev, "start")) {
        fprintf(f, "{\"ev\":\"start\",\"ts\":%lld,\"pid\":%d,\"bin\":\"", t0, self);
        jesc_f(f, name);
        fprintf(f, "\",\"argv\":[");
        for (int i = 0; i < argc; i++) {
            fprintf(f, "%s\"", i ? "," : "");
            jesc_f(f, argv[i]);
            fprintf(f, "\"");
        }
        fprintf(f, "],\"cwd\":\"");
        jesc_f(f, cwd);
        fprintf(f, "\",\"venv\":%d}\n", getenv("VIRTUAL_ENV") ? 1 : 0);
    } else {
        fprintf(f, "{\"ev\":\"end\",\"pid\":%d,\"ts\":%lld,\"dur_ms\":%.3f,\"rc\":%d}\n",
                self, nowns(), dur_ms, rc);
    }
    fclose(f);  // flushes into buf
    size_t off = 0;
    while (off < sz) {
        ssize_t n = write(fd, buf + off, sz - off);
        if (n < 0) { if (errno == EINTR) continue; break; }
        off += (size_t)n;
    }
    free(buf);
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

    log_line(fd, t0, "start", self, name, argv, argc, cwd, 0, 0);

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
    log_line(fd, t0, "end", self, name, NULL, 0, NULL, dur / 1e6, rc);
    close(fd);
    return rc;
}
