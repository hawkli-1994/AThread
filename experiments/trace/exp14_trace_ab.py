#!/usr/bin/env python3
"""exp14: capacity A/B on a FIXED real-agent trace.

Replays the recovered command timelines from the 14 real agent sessions
(repaired traces) under two modes:
  A) baseline — clean PATH, real binaries
  B) athread  — shim on PATH (python eligible calls -> warm fork)

Every executed command is equivalence-checked across modes: same rc and
stdout, else the row is flagged DIVERGED. Node/npm and lost /tmp script
commands are skipped in both modes equally (they appear in neither).

Metrics: total wall, per-bin p50/p95 latency, throughput, CPU (proc/stat).

Usage: python3 exp14_trace_ab.py [--mode a|b]   (run each mode, then the
summary prints when both result files exist)
"""
import glob, json, os, shutil, subprocess, sys, tempfile, time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))
REPAIRDIR = os.path.join(HERE, "logs", "repaired")
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
SOCK = os.path.join(HOME, ".athread", "athreadd.sock")
RESULTS = os.path.join(HERE, "..", "results")
WS_SRC = {"default": os.path.join(FIX, "repo"), "tests": os.path.join(FIX, "bugrepo")}
SKIP_BINS = {"node", "nodejs", "npm", "npx", "yarn", "pnpm"}

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin") and "shim_bin" not in p) \
             or "/usr/local/bin:/usr/bin:/bin"

def build_timelines():
    """Per session: ordered list of {delay_ms, bin, argv, cwd_kind, venv}."""
    tls = {}
    for p in sorted(glob.glob(os.path.join(REPAIRDIR, "*.jsonl"))):
        label = os.path.basename(p)[:-6]
        starts = {}
        for line in open(p):
            e = json.loads(line)
            if e["ev"] == "start":
                starts[e["pid"]] = e
        evs = sorted(starts.values(), key=lambda e: e["ts"])
        tl = []
        prev = None
        for s in evs:
            delay = max(0, (s["ts"] - prev) / 1e6) if prev else 0
            prev = s["ts"]
            tl.append({"delay_ms": min(delay, 1500), "bin": s["bin"],
                       "argv": s["argv"], "venv": s.get("venv", 0)})
        tls[label] = tl
    return tls

def prepare_cmd(tl, wsdir, testsdir):
    """Return (argv, cwd) runnable now, or None to skip."""
    bin_ = tl["bin"]
    if bin_ in SKIP_BINS:
        return None
    argv = list(tl["argv"])
    if not argv:
        return None
    # script-form python referencing files that only existed in /tmp -> skip
    if bin_ in ("python3", "python") and len(argv) >= 2 and \
       not argv[1].startswith("-") and not os.path.isfile(argv[1]):
        return None
    # cwd mapping: original workspaces are gone; unittest needs tests/ (bugrepo)
    is_test = bin_.startswith("python") and any("unittest" in a or "pytest" in a for a in argv[1:])
    cwd = testsdir if is_test else wsdir
    # strip leading paths from script args that lived under old workspaces
    out = []
    for i, a in enumerate(argv):
        if i == 0:
            out.append(bin_)
        elif "/workspaces/" in a or a.startswith("/tmp/"):
            base = os.path.basename(a)
            cand = os.path.join(cwd, base)
            out.append(cand if os.path.exists(cand) else a)
        else:
            out.append(a)
    return out, cwd

def worker(label, tl, mode, wsdir, testsdir, outpath):
    env = dict(os.environ, PATH=CLEAN_PATH)
    if mode == "b":
        env.update(PATH=f"{BIN}:{CLEAN_PATH}", ATHREAD_SOCK=SOCK,
                   ATHREAD_REAL_PYTHON="/usr/bin/python3")
    recs = []
    for tl_i in tl:
        time.sleep(tl_i["delay_ms"] / 1000)
        prep = prepare_cmd(tl_i, wsdir, testsdir)
        if prep is None:
            continue
        argv, cwd = prep
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                               text=True, timeout=60)
            rec = {"bin": tl_i["bin"], "rc": p.returncode, "out": p.stdout,
                   "ms": (time.monotonic() - t0) * 1000}
        except Exception as ex:
            rec = {"bin": tl_i["bin"], "rc": None, "out": None,
                   "ms": (time.monotonic() - t0) * 1000, "err": str(ex)[:80]}
        recs.append(rec)
    with open(outpath, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

def cpu_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    return vals[0] + vals[1] + vals[2]  # user+nice+system busy only

def run_mode(mode):
    tls = build_timelines()
    tmp = tempfile.mkdtemp(prefix=f"exp14_{mode}_")
    cpu0 = cpu_jiffies()
    t0 = time.monotonic()
    procs = []
    for label, tl in tls.items():
        wsdir = os.path.join(tmp, f"ws_{label}")
        testsdir = os.path.join(tmp, f"tests_{label}")
        shutil.copytree(WS_SRC["default"], wsdir)
        shutil.copytree(WS_SRC["tests"], testsdir)
        # git commands expect a repo; fixture/repo is one. tests_ws too.
        procs.append(subprocess.Popen(
            [sys.executable, __file__, "--worker", label, mode, wsdir, testsdir,
             os.path.join(tmp, f"{label}.jsonl")]))
    for p in procs:
        p.wait()
    wall = time.monotonic() - t0
    cpu1 = cpu_jiffies()
    # aggregate
    all_recs = []
    for label in tls:
        fp = os.path.join(tmp, f"{label}.jsonl")
        if os.path.exists(fp):
            all_recs += [json.loads(l) for l in open(fp)]
    shutil.rmtree(tmp, ignore_errors=True)
    by = defaultdict(list)
    for r in all_recs:
        by[r["bin"]].append(r["ms"])
    def pctl(xs, q):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0
    summary = {"mode": mode, "wall_s": round(wall, 1),
               "cpu_jiffies": cpu1 - cpu0,
               "n_cmds": len(all_recs),
               "bins": {b: {"n": len(v), "p50": round(pctl(v, .5), 1),
                            "p95": round(pctl(v, .95), 1)}
                        for b, v in by.items()}}
    with open(os.path.join(RESULTS, f"exp14_{mode}.json"), "w") as f:
        json.dump({"summary": summary, "recs": all_recs}, f)
    print(f"[mode {mode}] wall {wall:.1f}s cmds {len(all_recs)} "
          f"cpu_jiffies {cpu1-cpu0} -> {os.path.join(RESULTS, f'exp14_{mode}.json')}")

def summary():
    A = json.load(open(os.path.join(RESULTS, "exp14_a.json")))
    B = json.load(open(os.path.join(RESULTS, "exp14_b.json")))
    sa, sb = A["summary"], B["summary"]
    print("\n== exp14: fixed-trace capacity A/B ==")
    print(f"executed commands (after equal skips): A={sa['n_cmds']} B={sb['n_cmds']}")
    print(f"wall:      A {sa['wall_s']}s   B {sb['wall_s']}s")
    print(f"cpu ticks: A {sa['cpu_jiffies']}   B {sb['cpu_jiffies']} "
          f"({(1 - sb['cpu_jiffies']/max(sa['cpu_jiffies'],1))*100:.0f}% delta)")
    # equivalence: python commands must match on rc and stdout
    def key(recs, bins):
        return [(r["rc"], r["out"]) for r in recs if r["bin"] in bins]
    pa = key(A["recs"], ("python3", "python"))
    pb = key(B["recs"], ("python3", "python"))
    print(f"python equivalence (rc+stdout): {'MATCH' if pa == pb else 'DIVERGED'} "
          f"({len(pa)} python cmds)")
    for b in ("python3", "git", "cat", "tr", "wc"):
        if b in sa["bins"] and b in sb["bins"]:
            print(f"  {b:8s} p50 A {sa['bins'][b]['p50']:7.1f}ms  B {sb['bins'][b]['p50']:7.1f}ms   "
                  f"p95 A {sa['bins'][b]['p95']:7.1f}  B {sb['bins'][b]['p95']:7.1f}  "
                  f"n={sa['bins'][b]['n']}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _, _, label, mode, wsdir, testsdir, outpath = sys.argv
        tls = build_timelines()
        worker(label, tls[label], mode, wsdir, testsdir, outpath)
    elif len(sys.argv) > 1 and sys.argv[1] == "--summary":
        summary()
    else:
        mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "a"
        run_mode(mode)
