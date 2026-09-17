#!/usr/bin/env python3
"""exp8: does repo/filesystem cost grow enough in a big repo to matter?

2018-file repo showed git status ~2ms. Real agent repos are 10-100x bigger.
Build a ~50k-file repo, measure git/rg/find steady-state, concurrency
inflation, and shared-cache headroom.
"""
import concurrent.futures as cf
import os, random, statistics, string, subprocess, sys, time

BIG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixture", "bigrepo"))


def build(n_files=50000):
    if os.path.exists(os.path.join(BIG, ".git")):
        print("bigrepo exists, reusing")
        return
    os.makedirs(BIG, exist_ok=True)
    random.seed(7)
    for d in range(500):
        os.makedirs(f"{BIG}/pkg{d:03d}", exist_ok=True)
        for f in range(n_files // 500):
            p = f"{BIG}/pkg{d:03d}/mod{f:03d}.py"
            with open(p, "w") as fh:
                fh.write("# " + "".join(random.choices(string.ascii_letters, k=150)) + "\n" * 20)
                fh.write(f"def fn_{d}_{f}():\n    return {f}\n")
    subprocess.run(["git", "-C", BIG, "init", "-q"])
    subprocess.run(["git", "-C", BIG, "add", "-A"])
    subprocess.run(["git", "-C", BIG, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"])
    print("built", n_files, "files")


def t_git(args):
    t0 = time.monotonic()
    subprocess.run(["git", "-C", BIG] + args, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    return (time.monotonic() - t0) * 1000


def t_sh(argv):
    t0 = time.monotonic()
    subprocess.run(argv, cwd=BIG, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    return (time.monotonic() - t0) * 1000


def med(xs):
    return round(statistics.median(xs), 2)


build()

print("== steady state (after warm-up) ==")
for label, fn in [
    ("git status --porcelain", lambda: t_git(["status", "--porcelain"])),
    ("git status",             lambda: t_git(["status"])),
    ("git diff --stat",        lambda: t_git(["diff", "--stat"])),
    ("rg -l def .",            lambda: t_sh(["rg", "-l", "def", "."])),
    ("rg -l def -t py .",      lambda: t_sh(["rg", "-l", "def", "-t", "py", "."])),
    ("find . -name '*.py'",    lambda: t_sh(["find", ".", "-name", "*.py"])),
]:
    fn(); fn()
    ts = [fn() for _ in range(15)]
    print(f"  {label:26s} p50 {med(ts):8.2f}ms  p95 {sorted(ts)[int(len(ts)*.95)]:8.2f}ms")

print("\n== concurrent from 30 'agents' ==")
def conc(fn):
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(30) as ex:
        ts = list(ex.map(lambda _: fn(), range(30)))
    return (time.monotonic() - t0) * 1000, med(ts)
w, m = conc(lambda: t_git(["status", "--porcelain"]))
print(f"  30x concurrent git status --porcelain: wall {w:.0f}ms, per-call p50 {m:.2f}ms")
w, m = conc(lambda: t_sh(["rg", "-l", "def", "-t", "py", "."]))
print(f"  30x concurrent rg -l def:              wall {w:.0f}ms, per-call p50 {m:.2f}ms")

print("\n== first-touch cost (drop caches is unavailable; approximate with fresh clone) ==")
FRESH = BIG + "_fresh"
subprocess.run(["rm", "-rf", FRESH])
subprocess.run(["git", "clone", "-q", BIG, FRESH])
t0 = time.monotonic()
subprocess.run(["git", "-C", FRESH, "status", "--porcelain"],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"  first git status on fresh clone: {(time.monotonic()-t0)*1000:.0f}ms")
t0 = time.monotonic()
subprocess.run(["rg", "-l", "zzz_nomatch", FRESH], stdout=subprocess.DEVNULL)
print(f"  first rg on fresh clone:         {(time.monotonic()-t0)*1000:.0f}ms")
subprocess.run(["rm", "-rf", FRESH])
