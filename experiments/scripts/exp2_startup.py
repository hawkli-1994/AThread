#!/usr/bin/env python3
"""exp2: process startup cost — cold exec vs prefork/CoW warm runtime.

AThread claims agents burn huge cost on short-lived process churn
(python/node/bash cold start) and that prefork/warm-runtime could fix it.
Quantify both sides.
"""
import os, statistics, subprocess, sys, time

N = int(os.environ.get("EXP2_N", "150"))
PY = sys.executable


def bench(label, fn):
    ts = []
    for _ in range(N):
        t0 = time.monotonic()
        fn()
        ts.append((time.monotonic() - t0) * 1000)
    ts.sort()
    print(f"{label:42s} p50={ts[len(ts)//2]:7.2f}ms  p95={ts[int(len(ts)*.95)]:7.2f}ms")
    return ts[len(ts) // 2]


def cold(argv):
    subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


print(f"== cold exec (median of {N}) ==")
m_bash  = bench("bash -c true",                lambda: cold(["bash", "-c", "true"]))
m_true  = bench("/usr/bin/true (pure exec)",   lambda: cold(["/usr/bin/true"]))
m_git   = bench("git --version",               lambda: cold(["git", "--version"]))
m_rg    = bench("rg --version",                lambda: cold(["rg", "--version"]))
m_py    = bench("python3 -c pass",             lambda: cold([PY, "-c", "pass"]))
m_pyimp = bench("python3 -c 'import json,re'", lambda: cold([PY, "-c", "import json,re,subprocess,pathlib,argparse"]))
m_node  = bench("node -e 0",                   lambda: cold(["node", "-e", "0"]))

# warm fork from a pre-initialized parent (simulates AThread prefork/CoW runtime)
print(f"\n== warm fork+exit from pre-initialized parent (simulates prefork) ==")
import importlib
for m in ("json", "re", "subprocess", "pathlib", "argparse"):
    importlib.import_module(m)
parent_ready = time.monotonic()

def warm_fork():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)

m_warm = bench("python parent (imports loaded) fork+exit", warm_fork)

def warm_fork_exec_bash():
    pid = os.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "-c", "true"])
        os._exit(127)
    os.waitpid(pid, 0)

m_warm_bash = bench("warm fork + exec bash -c true", warm_fork_exec_bash)

print(f"\n== startup overhead breakdown ==")
print(f"python import cost (json,re,subprocess,pathlib,argparse): {m_pyimp - m_py:.2f}ms")
print(f"prefork saving per python-with-imports exec: {m_pyimp - m_warm:.2f}ms ({(m_pyimp-m_warm)/m_pyimp*100:.0f}%)")
print(f"prefork saving per bash exec (warm fork+exec vs cold): {m_bash - m_warm_bash:.2f}ms")

with open(os.path.join(os.path.dirname(__file__), "..", "results", "exp2_startup.txt"), "w") as f:
    pass  # stdout is captured by the runner
