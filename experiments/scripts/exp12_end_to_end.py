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

# worker dispatch MUST come before any experiment code: each S3 worker
# re-executes this file and must not rerun S1/S2
if len(sys.argv) > 1 and sys.argv[1] == "--session":
    _out, _seed, _mode = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    _random, _time = random, time
    _deadline = _time.monotonic() + 25
    _recs = []
    _MIX = [("py", 35), ("test", 10), ("git", 30), ("rg", 15), ("cat", 10)]
    _PYCODE = "import json,re,subprocess,pathlib,argparse;d={i:[i]*10 for i in range(2000)};json.dumps(d)"
    def _cmds_for(kind):
        return {"py": ["python3", "-c", _PYCODE],
                "test": ["python3", "-m", "unittest", "discover", "-s", "tests"],
                "git": ["git", "status", "--porcelain"],
                "rg": ["rg", "-n", "def foo", "-t", "py", "."],
                "cat": ["cat", ".git/HEAD"]}[kind]
    _random.seed(_seed)
    while _time.monotonic() < _deadline:
        _time.sleep(_random.uniform(0.4, 2.0))
        if _time.monotonic() >= _deadline:
            break
        kind = _random.choices([k for k, _ in _MIX], [w for _, w in _MIX], k=1)[0]
        cwd = BUGREPO if kind == "test" else REPO
        t0 = _time.monotonic()
        p = subprocess.run(_cmds_for(kind), cwd=cwd, env=env_for(_mode),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dt = (_time.monotonic() - t0) * 1000
        _recs.append((kind, dt, p.returncode, p.stdout.decode(errors="replace")[:40]))
    with open(_out, "w") as f:
        for k, ms, rc, out in _recs:
            f.write(f"{k} {ms:.1f} {rc} {out.replace(chr(10), '\\\\n')}\n")
    sys.exit(0)

def p50(xs): return statistics.median(xs) if xs else 0.0
def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs)-1, int(len(xs)*q))] if xs else 0.0

def bench(cmd, cwd, mode, n=15):
    ts, rcs, outs = [], [], []
    for _ in range(n):
        t0 = time.monotonic()
        p = subprocess.run(cmd, cwd=cwd, env=env_for(mode),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ts.append((time.monotonic() - t0) * 1000)
        rcs.append(p.returncode)
        import re as _re
        norm = lambda b: _re.sub(rb"in \d+\.\d+s", b"in Ts", b)
        outs.append(hash(norm(p.stdout) + b"\0" + norm(p.stderr)))
    return ts, rcs, outs

def ensure_daemon():
    r = subprocess.run([ATHREAD, "status"], capture_output=True, text=True)
    if "not running" in r.stdout:
        subprocess.run([ATHREAD, "start"], capture_output=True)
    time.sleep(0.5)

print("== exp12: AThread v0.1 end-to-end (transparent PATH shim) ==")
ensure_daemon()

print("\n-- S1: test loop (bugrepo, 15 runs each) --")
cmd = ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
a, arc, aout = bench(cmd, BUGREPO, "athread")
b, brc, bout = bench(cmd, BUGREPO, "baseline")
eq = "OK" if (set(arc) == set(brc) and set(aout) == set(bout)) else "DIVERGED"
print(f"  baseline p50 {p50(b):6.1f}ms  total {sum(b)/1000:5.2f}s  rc={sorted(set(brc))}")
print(f"  athread  p50 {p50(a):6.1f}ms  total {sum(a)/1000:5.2f}s  rc={sorted(set(arc))}   ({p50(b)/p50(a):.1f}x p50, output+rc {eq})")

print("\n-- S2: heavy-import -c (repo, 15 runs each) --")
cmd = ["python3", "-c",
       "import json,re,subprocess,pathlib,argparse;d={i:[i]*10 for i in range(2000)};json.dumps(d)"]
a, arc, aout = bench(cmd, REPO, "athread")
b, brc, bout = bench(cmd, REPO, "baseline")
eq = "OK" if (set(arc) == set(brc) and set(aout) == set(bout)) else "DIVERGED"
print(f"  baseline p50 {p50(b):6.1f}ms  rc={sorted(set(brc))}")
print(f"  athread  p50 {p50(a):6.1f}ms  rc={sorted(set(arc))}   ({p50(b)/p50(a):.1f}x p50, output+rc {eq})")

print("\n-- S3: sparse multi-session sim (30 sessions x 25s, transparent PATH) --")
# fast-path verification: the daemon's served counter must advance by
# exactly the number of python calls in the athread run
def served():
    import re
    r = subprocess.run([ATHREAD, "status"], capture_output=True, text=True)
    m = re.search(r"served (\d+)", r.stdout)
    return int(m.group(1)) if m else -1

def run_sim(mode):
    tmpdir = f"/tmp/exp12_{mode}"
    os.makedirs(tmpdir, exist_ok=True)
    for f in os.listdir(tmpdir):
        os.unlink(os.path.join(tmpdir, f))
    s0 = served() if mode == "athread" else None
    procs = [subprocess.Popen([sys.executable, __file__, "--session",
                               f"{tmpdir}/s{i}", str(i), mode],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for i in range(30)]
    for p in procs:
        p.wait()
    s1 = served() if mode == "athread" else None
    lat, rcs, outs = {}, {}, {}
    n_py = 0
    for fn in os.listdir(tmpdir):
        for line in open(os.path.join(tmpdir, fn)):
            parts = line.split(None, 3)
            k, ms, rc = parts[0], float(parts[1]), int(parts[2])
            lat.setdefault(k, []).append(ms)
            rcs.setdefault(k, set()).add(rc)
            if k in ("py", "test"):
                n_py += 1
    fastpath = None
    if mode == "athread":
        fastpath = (s1 - s0, n_py, s1 - s0 == n_py)
    return lat, rcs, fastpath

la, rca, fp = run_sim("athread")
lb, rcb, _ = run_sim("baseline")
for kind in ("py", "test", "git", "rg", "cat"):
    xa, xb = la.get(kind, []), lb.get(kind, [])
    same_rc = rca.get(kind) == rcb.get(kind)
    print(f"  {kind:5s}  baseline p50 {p50(xb):6.1f} p95 {pct(xb,.95):6.1f} | "
          f"athread p50 {p50(xa):6.1f} p95 {pct(xa,.95):6.1f}"
          + (f"  ({p50(xb)/max(p50(xa),0.01):.1f}x)" if kind in ("py", "test") else "")
          + f"  rc {'same' if same_rc else 'DIFFER'}")
if fp:
    print(f"  fast-path verification: daemon served +{fp[0]} forks for {fp[1]} "
          f"python calls -> {'ALL via fast path' if fp[2] else 'MISMATCH (some fell back)'}")

print("\n-- S4: memory — SUPERSEDED by exp13_memory_pl.py --")
print("  system-wide PSS deltas are unverifiable (daemon CoW dilution + noise);")
print("  full-process-group accounting: see results/exp13_memory_pl.txt")

print("\n-- S5: fallback (PYTHONPATH set) --")
e = env_for("athread"); e["PYTHONPATH"] = "/tmp"
r = subprocess.run(["python3", "-c", "print('fallback-ok')"], env=e, capture_output=True, text=True)
print(f"  output={r.stdout.strip()} rc={r.returncode} (must be fallback-ok/0)")
print("\ndone.")
