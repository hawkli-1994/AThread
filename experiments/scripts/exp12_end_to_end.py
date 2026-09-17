#!/usr/bin/env python3
"""exp12: end-to-end validation of the real AThread v0.1 prototype.

The agent's view is fully transparent: sessions just run `python3 ...` and
PATH decides whether that resolves to the athread shim or the real binary.

Scenarios:
  S1 test loop     — 15x `python -m unittest discover` (the 21x replay case)
  S2 heavy import  — 15x `python -c 'import json,re,...'`
  S3 sparse sim    — 30 sessions x 25s, think 0.4-2s, realistic command mix
  S4 memory        — 20 concurrent python procs via shim vs 20 cold
  S5 fallback      — PYTHONPATH set -> must fall back, correct output
"""
import os, random, statistics, subprocess, sys, time

HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
SOCK = os.path.join(HOME, ".athread", "athreadd.sock")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixture", "repo"))
BUGREPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixture", "bugrepo"))
ATHREAD = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "athread", "athread"))

BASE_ENV = {k: v for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "PYTHONPATH", "CONDA_PREFIX", "ATHREAD_SOCK",
                         "ATHREAD_REAL_PYTHON")}
# keep the invoking PATH (minus any athread shim dir) so rg/node/etc. resolve
CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin") and "shim_bin" not in p) \
             or "/usr/local/bin:/usr/bin:/bin"

def env_for(mode):
    e = dict(BASE_ENV)
    if mode == "athread":
        e["PATH"] = f"{BIN}:{CLEAN_PATH}"
        e["ATHREAD_SOCK"] = SOCK
        e["ATHREAD_REAL_PYTHON"] = "/usr/bin/python3"
    else:
        e["PATH"] = CLEAN_PATH
    return e

def p50(xs): return statistics.median(xs) if xs else 0.0
def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs)-1, int(len(xs)*q))] if xs else 0.0

def bench(cmd, cwd, mode, n=15):
    ts = []
    for _ in range(n):
        t0 = time.monotonic()
        subprocess.run(cmd, cwd=cwd, env=env_for(mode),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ts.append((time.monotonic() - t0) * 1000)
    return ts

def ensure_daemon():
    r = subprocess.run([ATHREAD, "status"], capture_output=True, text=True)
    if "not running" in r.stdout:
        subprocess.run([ATHREAD, "start"], capture_output=True)
    time.sleep(0.5)

print("== exp12: AThread v0.1 end-to-end (transparent PATH shim) ==")
ensure_daemon()

print("\n-- S1: test loop (bugrepo, 15 runs each) --")
cmd = ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
a = bench(cmd, BUGREPO, "athread")
b = bench(cmd, BUGREPO, "baseline")
print(f"  baseline p50 {p50(b):6.1f}ms  total {sum(b)/1000:5.2f}s")
print(f"  athread  p50 {p50(a):6.1f}ms  total {sum(a)/1000:5.2f}s   ({p50(b)/p50(a):.1f}x p50)")

print("\n-- S2: heavy-import -c (repo, 15 runs each) --")
cmd = ["python3", "-c",
       "import json,re,subprocess,pathlib,argparse;d={i:[i]*10 for i in range(2000)};json.dumps(d)"]
a = bench(cmd, REPO, "athread")
b = bench(cmd, REPO, "baseline")
print(f"  baseline p50 {p50(b):6.1f}ms")
print(f"  athread  p50 {p50(a):6.1f}ms   ({p50(b)/p50(a):.1f}x p50)")

print("\n-- S3: sparse multi-session sim (30 sessions x 25s, transparent PATH) --")
DUR = 25
MIX = [("py", 35), ("test", 10), ("git", 30), ("rg", 15), ("cat", 10)]
PYCODE = "import json,re,subprocess,pathlib,argparse;d={i:[i]*10 for i in range(2000)};json.dumps(d)"
def cmds_for(kind):
    return {"py": ["python3", "-c", PYCODE],
            "test": ["python3", "-m", "unittest", "discover", "-s", "tests"],
            "git": ["git", "status", "--porcelain"],
            "rg": ["rg", "-n", "def foo", "-t", "py", "."],
            "cat": ["cat", ".git/HEAD"]}[kind]

def session_main(out, seed, mode):
    random.seed(seed)
    deadline = time.monotonic() + DUR
    recs = []
    while time.monotonic() < deadline:
        time.sleep(random.uniform(0.4, 2.0))
        if time.monotonic() >= deadline:
            break
        kind = random.choices([k for k, _ in MIX], [w for _, w in MIX], k=1)[0]
        cwd = BUGREPO if kind == "test" else REPO
        t0 = time.monotonic()
        subprocess.run(cmds_for(kind), cwd=cwd, env=env_for(mode),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        recs.append((kind, (time.monotonic() - t0) * 1000))
    with open(out, "w") as f:
        for k, ms in recs:
            f.write(f"{k} {ms:.1f}\n")

def run_sim(mode):
    tmpdir = f"/tmp/exp12_{mode}"
    os.makedirs(tmpdir, exist_ok=True)
    for f in os.listdir(tmpdir):
        os.unlink(os.path.join(tmpdir, f))
    procs = [subprocess.Popen([sys.executable, __file__, "--session",
                               f"{tmpdir}/s{i}", str(i), mode],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for i in range(30)]
    for p in procs:
        p.wait()
    lat = {}
    for fn in os.listdir(tmpdir):
        for line in open(os.path.join(tmpdir, fn)):
            k, ms = line.split()
            lat.setdefault(k, []).append(float(ms))
    return lat

if len(sys.argv) > 1 and sys.argv[1] == "--session":
    session_main(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    sys.exit(0)
la = run_sim("athread")
lb = run_sim("baseline")
for kind in ("py", "test", "git", "rg", "cat"):
    xa, xb = la.get(kind, []), lb.get(kind, [])
    print(f"  {kind:5s}  baseline p50 {p50(xb):6.1f} p95 {pct(xb,.95):6.1f} | "
          f"athread p50 {p50(xa):6.1f} p95 {pct(xa,.95):6.1f}"
          + (f"  ({p50(xb)/max(p50(xa),0.01):.1f}x)" if kind in ("py", "test") else ""))

print("\n-- S4: memory, 20 concurrent python via shim vs 20 cold --")
def pss_total():
    tot = 0
    for x in os.listdir("/proc"):
        if x.isdigit():
            try:
                for l in open(f"/proc/{x}/smaps_rollup"):
                    if l.startswith("Pss:"):
                        tot += int(l.split()[1])
            except OSError:
                pass
    return tot / 1024

CODE = "import json,re,subprocess,pathlib,argparse;import time;d={i:[i]*10 for i in range(2000)};time.sleep(12)"
m0 = pss_total()
ps = [subprocess.Popen(["python3", "-c", CODE], env=env_for("athread"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(20)]
time.sleep(6)
m1 = pss_total()
for p in ps:
    p.terminate()
print(f"  20 shim python procs + daemon: +{m1-m0:.0f} MB total")
ps = [subprocess.Popen(["python3", "-c", CODE], env=env_for("baseline"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(20)]
time.sleep(6)
m2 = pss_total()
for p in ps:
    p.terminate()
print(f"  20 cold python procs:          +{m2-m1:.0f} MB total")

print("\n-- S5: fallback (PYTHONPATH set) --")
e = env_for("athread"); e["PYTHONPATH"] = "/tmp"
r = subprocess.run(["python3", "-c", "print('fallback-ok')"], env=e, capture_output=True, text=True)
print(f"  output={r.stdout.strip()} rc={r.returncode} (must be fallback-ok/0)")
print("\ndone.")
