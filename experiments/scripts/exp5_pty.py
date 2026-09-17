#!/usr/bin/env python3
"""exp5: PTY vs lightweight pipe for agent command execution.

AThread claims most agent commands don't need a full PTY and that
lightweight streams would be cheaper. Measure per-exec latency and
idle-session resource cost.
"""
import os, pty, statistics, subprocess, sys, time

N = int(os.environ.get("EXP5_N", "100"))
HOLD = int(os.environ.get("EXP5_HOLD", "25"))


def bench_pipe(n):
    ts = []
    for _ in range(n):
        t0 = time.monotonic()
        subprocess.run(["bash", "-c", "true"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        ts.append((time.monotonic() - t0) * 1000)
    return ts


def bench_pty(n):
    ts = []
    for _ in range(n):
        t0 = time.monotonic()
        master, slave = pty.openpty()
        p = subprocess.Popen(["bash", "-c", "true"], stdin=slave, stdout=slave,
                             stderr=slave, close_fds=True)
        os.close(slave)
        p.wait()
        os.close(master)
        ts.append((time.monotonic() - t0) * 1000)
    return ts


print(f"== A) per-exec latency, pipe vs PTY (n={N}) ==")
tp = bench_pipe(N)
tt = bench_pty(N)
print(f"  pipe p50 {statistics.median(tp):6.2f}ms p95 {sorted(tp)[int(N*.95)]:6.2f}ms")
print(f"  pty  p50 {statistics.median(tt):6.2f}ms p95 {sorted(tt)[int(N*.95)]:6.2f}ms "
      f"(+{statistics.median(tt)-statistics.median(tp):.2f}ms, "
      f"{(statistics.median(tt)/statistics.median(tp)-1)*100:.0f}% slower)")

print(f"\n== B) idle-session cost: {N} held-open sessions, pipe vs PTY ==")
def mem_avail_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) / 1024

pipe_procs = []
for i in range(N):
    pipe_procs.append(subprocess.Popen(
        ["bash", "-c", f"exec {N+10}< <(:); sleep {HOLD}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
time.sleep(2)
m0 = mem_avail_mb()

pty_procs, fds = [], []
for i in range(N):
    master, slave = pty.openpty()
    p = subprocess.Popen(["bash", "-c", f"sleep {HOLD}"], stdin=slave, stdout=slave,
                         stderr=slave, close_fds=False)
    os.close(slave)
    pty_procs.append(p)
    fds.append(master)
time.sleep(2)
m1 = mem_avail_mb()

n_ctxt0 = int(open("/proc/stat").read().split("ctxt ")[1].split()[0])
time.sleep(5)
n_ctxt1 = int(open("/proc/stat").read().split("ctxt ")[1].split()[0])

print(f"  {N} pipe-held bash sessions: MemAvailable drop {m0-m1:.1f} MB (baseline measured first)")
print(f"  {N} pty-held bash sessions:  MemAvailable drop {m1-m0:.1f} MB vs baseline")
print(f"  context switches while {N} pty sessions idle: {(n_ctxt1-n_ctxt0)/5:.0f}/s")

for p in pipe_procs + pty_procs:
    p.terminate()
for fd in fds:
    os.close(fd)
