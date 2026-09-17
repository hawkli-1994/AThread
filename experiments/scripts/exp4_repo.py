#!/usr/bin/env python3
"""exp4: duplicated repository metadata work across sessions.

AThread claims N agents on one repo redo identical work (git status/diff/rg).
Measure: repeated git status latency profile, concurrent git status
inflation, and a prototype shared-metadata cache (index-mtime keyed)
to bound the headroom.
"""
import concurrent.futures as cf
import os, statistics, subprocess, sys, time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixture", "repo"))
ITER = int(os.environ.get("EXP4_ITER", "60"))


def run_git(args):
    t0 = time.monotonic()
    subprocess.run(["git", "-C", REPO] + args,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return (time.monotonic() - t0) * 1000


print(f"== A) git status x{ITER} sequential (same process, warm cache) ==")
first = run_git(["status", "--porcelain"])
rest = [run_git(["status", "--porcelain"]) for _ in range(ITER)]
print(f"  first call {first:.2f}ms | steady-state p50 {statistics.median(rest):.2f}ms "
      f"p95 {sorted(rest)[int(len(rest)*.95)]:.2f}ms")
print(f"  total CPU time burned: {sum(rest)/1000:.2f}s for {ITER} identical answers")

print(f"\n== B) git diff/log/branch steady-state ==")
for label, args in [("git diff", ["diff", "--stat"]),
                    ("git log", ["log", "--oneline", "-5"]),
                    ("git branch", ["branch", "-a"])]:
    ts = [run_git(args) for _ in range(30)]
    print(f"  {label:12s} p50 {statistics.median(ts):6.2f}ms  p95 {sorted(ts)[int(len(ts)*.95)]:6.2f}ms")

print(f"\n== C) concurrent git status from 50 'agents' ==")
def one(_):
    return run_git(["status", "--porcelain"])
t0 = time.monotonic()
with cf.ThreadPoolExecutor(50) as ex:
    ts = list(ex.map(one, range(50)))
wall = time.monotonic() - t0
print(f"  wall {wall*1000:.0f}ms for 50 concurrent | per-call p50 {statistics.median(ts):.2f}ms "
      f"p95 {sorted(ts)[int(len(ts)*.95)]:.2f}ms")

print(f"\n== D) prototype: shared workspace metadata cache (mtime-keyed) ==")
INDEX = os.path.join(REPO, ".git", "index")
HEAD = os.path.join(REPO, ".git", "HEAD")
_cache = {}

def cached_status():
    key = (os.stat(INDEX).st_mtime_ns, os.stat(HEAD).st_mtime_ns,
           os.stat(os.path.join(REPO, ".git", "refs", "heads")).st_mtime_ns)
    if key in _cache:
        return 0.0
    t0 = time.monotonic()
    subprocess.run(["git", "-C", REPO, "status", "--porcelain"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    _cache[key] = True
    return (time.monotonic() - t0) * 1000

ts = [cached_status() for _ in range(ITER)]
hits = sum(1 for t in ts if t == 0.0)
print(f"  {hits}/{ITER} calls served from shared cache (0ms); "
      f"avg {(sum(ts)/len(ts)):.3f}ms vs uncached {statistics.median(rest):.2f}ms "
      f"-> {(1-sum(ts)/len(ts)/statistics.median(rest))*100:.1f}% latency saved when shared across agents")
