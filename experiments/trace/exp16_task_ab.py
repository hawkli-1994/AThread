#!/usr/bin/env python3
"""exp16 (issue #1 P1-B): task-level capacity A/B on a fixed, python-density
parameterized workload — three arms: cold / fork-only warm root / full AThread.

Design (compliance with issue #1 P1-B):
- FIXED TRACE, ADJUSTABLE PYTHON DENSITY: two synthetic task types run against
  frozen fixtures. "pydense" mimics a test-loop dev session (git status, file
  read, then N x [edit; python3 -m unittest discover]); N (density) is a
  parameter (1 and 10 reported separately). "shelldense" is the declared
  non-benefiting control (git/cat/grep/wc/find only, exactly 1 python call)
  so the report cannot be accused of cherry-picking tasks.
- THREE ARMS, ALTERNATING BATCHES: 6 batches; per batch the arm order rotates
  (cold,forkonly,full) -> (forkonly,full,cold) -> ... so drift affects all
  arms equally. Arms within a batch run SEQUENTIALLY (no cross-arm CPU
  interference; wall time per task is still recorded per task).
  - cold:      clean PATH, no shim, no daemon (true baseline).
  - forkonly:  athreadd with EMPTY warm_modules.txt — fork+CoW and IPC
               overhead only, no import preloading. Isolates how much of the
               gain is fork avoidance vs module prewarming (P2 ablation input).
  - full:      athreadd with DEFAULT_WARM (33 modules) — the AThread fast path.
  Warm arms go through the real shim (PATH + ATHREAD_SOCK), i.e. they include
  the product's own dispatch cost. Daemon is started fresh per arm-run via
  ATHREAD_STATE_DIR, so each arm pays its own warm-root startup.
- CPU: per-command wait4 rusage (user+system of the direct child), summed per
  task. cgroup v2 unavailable (no root on WSL2); wait4 already validated in
  exp15. Daemon CPU is read from /proc/<pid>/stat before stop and reported
  BOTH excluded and amortized per task (full resident cost).
- EQUIVALENCE: every step compared across arms by (task_id, step_id): rc +
  NORM(stdout) + NORM(stderr) (same NORM rules as exp15, declared below);
  final workspace tree byte-for-byte (sha256, __pycache__/.pyc/.git excluded).
- METRICS: per-successful-task CPU (s), task wall P50/P95/P99, suite
  throughput (tasks/s), success rate. Confidence intervals are computed over
  BATCH-LEVEL means (6 independent batches per arm), normal approx, declared.

Usage:
  python3 exp16_task_ab.py --run            (full rotation, writes raw JSON)
  python3 exp16_task_ab.py --summary        (aggregates raw JSON to text)
"""
import glob, json, math, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
REAL_PYTHON = "/usr/bin/python3"
ATHREAD_CLI = os.path.abspath(os.path.join(HERE, "..", "..", "athread", "athread"))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))
RAW = os.path.join(RESULTS, "exp16_raw.json")

WS_SRC = os.path.join(FIX, "repo")          # shell-dense workspace
TESTS_SRC = os.path.join(FIX, "bugrepo")    # python test-loop workspace

N_TASKS = 8           # tasks per suite arm-run, all run concurrently
BATCHES = 6
DENSITIES = [1, 10]
SUITES = ["pydense", "shelldense"]
ARMS = ["cold", "forkonly", "full"]
STEP_DELAY_MS = 80    # fixed think delay between steps (declared)

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin") and "shim_bin" not in p) \
             or "/usr/local/bin:/usr/bin:/bin"

# same declared normalization rules as exp15 (applied identically to all arms)
def NORM(s, ws=None):
    if s is None:
        return None
    if ws:
        s = s.replace(ws, "<WS>")
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", "DATE", s)
    s = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", "TIME", s)
    s = re.sub(r"\b20\d{2}\b", "YEAR", s)
    s = re.sub(r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2}", "DAY", s)
    s = re.sub(r"\b(CST|UTC|GMT|PST|EST)\b", "TZ", s)
    s = re.sub(r"in \d+\.\d+s", "in Ts", s)
    s = re.sub(r"/tmp/[A-Za-z0-9_.-]+", "TMP", s)
    s = re.sub(r"\b1[0-9]{9}\b", "EPOCH", s)
    return s

def task_steps(suite, density, task_idx):
    """Fixed step sequence. step_id is stable across arms/batches."""
    steps = []
    def add(kind, argv, cwd_kind):
        steps.append({"step_id": f"s{len(steps):02d}", "kind": kind,
                      "argv": argv, "cwd_kind": cwd_kind})
    if suite == "pydense":
        add("git",   ["git", "status", "--short"], "ws")
        add("git",   ["git", "log", "--oneline", "-3"], "ws")
        add("cat",   ["cat", "src/module00/file000.py"], "ws")
        add("grep",  ["grep", "-r", "def ", "src/module05"], "ws")
        for i in range(1, density + 1):
            # deterministic no-op "fix attempt": appends a comment line;
            # tests stay failing -> classified expected-test-failure
            add("edit", ["sed", "-i", f"$a# fix attempt {i} (task {task_idx})",
                          "calc/ops.py"], "tests")
            add("python", ["python3", "-m", "unittest", "discover", "-s", "tests"],
                "tests")
        add("git",   ["git", "diff", "--stat"], "tests")
    else:  # shelldense — declared non-benefiting control
        add("git",  ["git", "status", "--short"], "ws")
        add("git",  ["git", "log", "--oneline", "-5"], "ws")
        add("cat",  ["cat", "src/module01/file000.py"], "ws")
        add("cat",  ["cat", "src/module02/file001.py"], "ws")
        add("grep", ["grep", "-rn", "import", "src/module03"], "ws")
        add("wc",   ["wc", "-l", "src/module04/file002.py"], "ws")
        add("find", ["find", "src/module06", "-name", "file003.py"], "ws")
        add("sort", ["sort", "src/module07/file000.py"], "ws")
        add("sed",  ["sed", "-n", "1,5p", "src/module08/file004.py"], "ws")
        add("ls",   ["ls", "src/module09"], "ws")
        add("python", ["python3", "-c", "print('loop done')"], "ws")
    return steps

def run4(argv, cwd, env, timeout=30):
    """fork/exec with tempfile capture + wait4 rusage (same as exp15)."""
    import tempfile as _tf
    out_f = _tf.TemporaryFile()
    err_f = _tf.TemporaryFile()
    devnull = os.open(os.devnull, os.O_RDONLY)
    t0 = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(out_f.fileno(), 1); os.dup2(err_f.fileno(), 2)
            os.dup2(devnull, 0)
            os.close(devnull)
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)
    os.close(devnull)
    deadline = t0 + timeout
    rc, ru = None, None
    while time.monotonic() < deadline:
        done, status, r = os.wait4(pid, os.WNOHANG)
        if done == pid:
            rc = os.waitstatus_to_exitcode(status)
            ru = r
            break
        time.sleep(0.005)
    if rc is None:
        try: os.kill(pid, 9)
        except OSError: pass
        _, status, ru = os.wait4(pid, 0)
        rc = os.waitstatus_to_exitcode(status)
    out_f.seek(0); stdout = out_f.read().decode(errors="replace")
    err_f.seek(0); stderr = err_f.read().decode(errors="replace")
    out_f.close(); err_f.close()
    wall_ms = (time.monotonic() - t0) * 1000
    cpu_ms = ((ru.ru_utime + ru.ru_stime) * 1000) if ru else 0.0
    return {"rc": rc, "out": stdout, "err": stderr,
            "wall_ms": wall_ms, "cpu_ms": cpu_ms}

def tree(root):
    import hashlib
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "__pycache__" and d != ".git"]
        for fn in fns:
            if fn.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, "rb") as f:
                    data = f.read()
                out[os.path.relpath(p, root)] = [len(data),
                                                 hashlib.sha256(data).hexdigest()]
            except OSError:
                pass
    return out

def classify(rec):
    if rec["rc"] == 0:
        return "ok"
    if rec["argv"][0].startswith("python"):
        m = re.search(r"Ran (\d+) tests.*?FAILED \(failures=(\d+)", rec["err"] or "", re.S)
        if m:
            return "expected-test-failure"
    return "failure"

def worker(task_id, suite, density, wsdir, testsdir, outpath):
    env = dict(os.environ)
    cwdmap = {"ws": wsdir, "tests": testsdir}
    recs = []
    for st in task_steps(suite, density, int(task_id.rsplit("_", 1)[1][1:])):
        time.sleep(STEP_DELAY_MS / 1000.0)
        r = run4(st["argv"], cwdmap[st["cwd_kind"]], env)
        r.update({"step_id": st["step_id"], "kind": st["kind"], "argv": st["argv"]})
        recs.append(r)
    json.dump({"task_id": task_id, "recs": recs,
               "trees": {"ws": tree(wsdir), "tests": tree(testsdir)}},
              open(outpath, "w"))

# ---- daemon lifecycle (fresh state dir per arm-run; strays killed) ----
def daemon_pid(state_dir):
    try:
        return int(open(os.path.join(state_dir, "athreadd.pid")).read().strip())
    except (OSError, ValueError):
        return None

def daemon_cpu_ms(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        hz = os.sysconf("SC_CLK_TCK")
        return (int(fields[11]) + int(fields[12])) * 1000.0 / hz  # utime+stime
    except (OSError, IndexError):
        return None

def start_daemon(state_dir, arm):
    os.makedirs(state_dir, exist_ok=True)
    cfg = os.path.join(state_dir, "warm_modules.txt")
    if arm == "forkonly":
        open(cfg, "w").close()            # exists-but-empty -> no warm modules
    elif os.path.exists(cfg):
        os.unlink(cfg)                    # full -> daemon DEFAULT_WARM
    env = dict(os.environ, ATHREAD_STATE_DIR=state_dir)
    r = subprocess.run(["python3", ATHREAD_CLI, "start"], env=env,
                       capture_output=True, text=True, timeout=60)
    sock = os.path.join(state_dir, "athreadd.sock")
    for _ in range(200):
        if os.path.exists(sock):
            return daemon_pid(state_dir)
        time.sleep(0.05)
    raise RuntimeError(f"daemon failed to start: {r.stdout} {r.stderr}")

def stop_daemon(state_dir):
    env = dict(os.environ, ATHREAD_STATE_DIR=state_dir)
    subprocess.run(["python3", ATHREAD_CLI, "stop"], env=env,
                   capture_output=True, text=True, timeout=30)
    pid = daemon_pid(state_dir)
    if pid:
        try: os.kill(pid, 9)
        except OSError: pass
    shutil.rmtree(state_dir, ignore_errors=True)

def run_suite_arm(batch, suite, density, arm):
    """Run one suite (N_TASKS concurrent tasks) under one arm. Returns record."""
    tmp = tempfile.mkdtemp(prefix=f"exp16_{arm}_")
    dcpu = None
    env = dict(os.environ)
    if arm == "cold":
        env["PATH"] = CLEAN_PATH
        env.pop("ATHREAD_SOCK", None)
    else:
        state_dir = os.path.join(tmp, "state")
        start_daemon(state_dir, arm)
        env.update(PATH=f"{BIN}:{CLEAN_PATH}",
                   ATHREAD_SOCK=os.path.join(state_dir, "athreadd.sock"),
                   ATHREAD_REAL_PYTHON=REAL_PYTHON)
    tasks, procs = {}, []
    t0 = time.monotonic()
    for i in range(N_TASKS):
        wsdir = os.path.join(tmp, f"ws_{i}")
        testsdir = os.path.join(tmp, f"tests_{i}")
        shutil.copytree(WS_SRC, wsdir)
        shutil.copytree(TESTS_SRC, testsdir)
        tid = f"{suite}_d{density}_t{i}"
        outpath = os.path.join(tmp, f"{tid}.json")
        procs.append((tid, outpath, subprocess.Popen(
            [sys.executable, __file__, "--worker", tid, suite, str(density),
             wsdir, testsdir, outpath], env=env)))
    for tid, outpath, p in procs:
        p.wait()
        tasks[tid] = json.load(open(outpath))
    wall = time.monotonic() - t0
    if arm != "cold":
        pid = daemon_pid(os.path.join(tmp, "state"))
        if pid:
            dcpu = daemon_cpu_ms(pid)
        stop_daemon(os.path.join(tmp, "state"))
    for i in range(N_TASKS):
        shutil.rmtree(os.path.join(tmp, f"ws_{i}"), ignore_errors=True)
        shutil.rmtree(os.path.join(tmp, f"tests_{i}"), ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"batch": batch, "suite": suite, "density": density, "arm": arm,
            "wall_s": round(wall, 3), "daemon_cpu_ms": dcpu, "tasks": tasks}

def run_all():
    raw = []
    rotations = [ARMS[i:] + ARMS[:i] for i in range(len(ARMS))]
    for b in range(BATCHES):
        for density in DENSITIES:
            for suite in SUITES:
                for arm in rotations[b % len(rotations)]:
                    rec = run_suite_arm(b, suite, density, arm)
                    raw.append(rec)
                    cpu = sum(s["cpu_ms"] for t in rec["tasks"].values() for s in t["recs"])
                    print(f"[b{b} {suite} d{density} {arm:8s}] wall {rec['wall_s']:6.2f}s "
                          f"cmd-cpu {cpu:7.0f}ms daemon-cpu {rec['daemon_cpu_ms']}", flush=True)
        json.dump(raw, open(RAW, "w"))     # checkpoint after each batch
    print("raw ->", RAW)

# ---------------- summary ----------------
def mean_ci(vals):
    n = len(vals)
    if n == 0:
        return float("nan"), float("nan"), 0
    m = sum(vals) / n
    if n < 2:
        return m, float("nan"), n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    return m, 1.96 * sd / math.sqrt(n), n

def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, max(0, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]

def summary():
    raw = json.load(open(RAW))
    print("== exp16 task-level A/B (P1-B) ==")
    print(f"batches {BATCHES}  tasks/suite-run {N_TASKS}  concurrency {N_TASKS} "
          f"(sequential arms within a batch; arm order rotates per batch)")
    # ---- equivalence across arms ----
    print("\n-- equivalence (cold vs forkonly vs full) --")
    total_steps, bad = 0, []
    bykey = defaultdict(dict)
    for rec in raw:
        for tid, task in rec["tasks"].items():
            for st in task["recs"]:
                bykey[(rec["suite"], rec["density"], tid, st["step_id"])][rec["arm"]] = st
    for (suite, density, tid, sid), arms in sorted(bykey.items()):
        if set(arms) != set(ARMS):
            continue
        total_steps += 1
        base = arms["cold"]
        for arm in ("forkonly", "full"):
            o = arms[arm]
            same = (classify(base) == classify(o)
                    and NORM(base["out"], "") == NORM(o["out"], "")
                    and NORM(base["err"], "") == NORM(o["err"], ""))
            if not same:
                bad.append((suite, density, tid, sid, arm))
    print(f"steps compared across all 3 arms: {total_steps}  DIVERGENT: {len(bad)}")
    for x in bad[:10]:
        print("  DIVERGENT", x)
    tree_bad = 0
    # task-level tree check: for each (batch,suite,density,task) compare trees
    treemap = defaultdict(dict)
    for rec in raw:
        for tid, task in rec["tasks"].items():
            treemap[(rec["batch"], rec["suite"], rec["density"], tid)][rec["arm"]] = task["trees"]
    n_tree = 0
    for k, arms in treemap.items():
        if set(arms) != set(ARMS):
            continue
        n_tree += 1
        for arm in ("forkonly", "full"):
            if arms[arm] != arms["cold"]:
                tree_bad += 1
    print(f"task workspace trees compared: {n_tree}  MISMATCHED: {tree_bad}")

    # ---- metrics per (suite, density, arm) ----
    print("\n-- metrics (CI over batch-level means, n=%d batches) --" % BATCHES)
    groups = defaultdict(list)
    for rec in raw:
        groups[(rec["suite"], rec["density"], rec["arm"])].append(rec)
    for suite in SUITES:
        for density in DENSITIES:
            print(f"\n[{suite} density={density}]")
            rows = {}
            for arm in ARMS:
                recs = groups[(suite, density, arm)]
                # per-task cpu/wall pooled; batch means for CI
                per_task_cpu, per_task_wall = [], []
                batch_cpu, batch_wall, batch_thr = [], [], []
                step_wall = defaultdict(list)
                n_fail = 0
                n_tasks = 0
                for rec in recs:
                    bt_cpu = bt_wall = 0.0
                    for tid, task in rec["tasks"].items():
                        n_tasks += 1
                        cls = [classify(s) for s in task["recs"]]
                        ok = all(c in ("ok", "expected-test-failure") for c in cls)
                        if not ok:
                            n_fail += 1
                        tw = sum(s["wall_ms"] for s in task["recs"])
                        tc = sum(s["cpu_ms"] for s in task["recs"])
                        per_task_cpu.append(tc)
                        per_task_wall.append(tw)
                        bt_cpu += tc; bt_wall += tw
                        for s in task["recs"]:
                            step_wall[s["kind"]].append(s["wall_ms"])
                    batch_cpu.append(bt_cpu / max(len(rec["tasks"]), 1))
                    batch_wall.append(bt_wall / max(len(rec["tasks"]), 1))
                    batch_thr.append(len(rec["tasks"]) / max(rec["wall_s"], 1e-9))
                cpu_m, cpu_ci, _ = mean_ci(batch_cpu)
                dcpu = [r["daemon_cpu_ms"] or 0 for r in recs]
                dcpu_m = sum(dcpu) / len(dcpu) if dcpu else 0.0
                dcpu_task = dcpu_m / N_TASKS
                wall_sorted = sorted(per_task_wall)
                rows[arm] = (cpu_m, cpu_ci, dcpu_task, wall_sorted,
                             mean_ci(batch_thr), n_fail, n_tasks, step_wall,
                             per_task_cpu)
                p50, p95, p99 = (pct(wall_sorted, 50), pct(wall_sorted, 95),
                                 pct(wall_sorted, 99))
                thr_m, thr_ci, _ = mean_ci(batch_thr)
                print(f"  {arm:8s} cpu/task {cpu_m/1000:6.3f}s ±{cpu_ci/1000:.3f}s"
                      f"(+daemon {dcpu_task/1000:.3f}s)"
                      f"  wall P50 {p50:6.0f} P95 {p95:6.0f} P99 {p99:6.0f}ms"
                      f"  thr {thr_m:.3f}±{thr_ci:.3f} t/s"
                      f"  fail {n_fail}/{n_tasks}")
            base_cpu = rows["cold"][0]
            base_thr = rows["cold"][4][0]
            for arm in ("forkonly", "full"):
                c = rows[arm]
                cpu_all = c[0] + c[2]      # commands + amortized daemon
                d_cpu = (1 - cpu_all / max(base_cpu, 1e-9)) * 100
                d_thr = (c[4][0] / max(base_thr, 1e-9) - 1) * 100
                print(f"  -> {arm:8s} vs cold: task CPU saving {d_cpu:+.1f}% (incl daemon) "
                      f" throughput {d_thr:+.1f}%")
            for arm in ARMS:
                sw = rows[arm][7]
                if "python" in sw and sw["python"]:
                    x = sorted(sw["python"])
                    print(f"     {arm:8s} python step wall p50 {pct(x,50):6.1f}ms "
                          f"p95 {pct(x,95):6.1f}ms (n={len(x)})")

if __name__ == "__main__":
    if "--summary" in sys.argv:
        summary()
    elif len(sys.argv) > 1 and sys.argv[1] == "--worker":
        task_id, suite, density, wsdir, testsdir, outpath = sys.argv[2:8]
        worker(task_id, suite, int(density), wsdir, testsdir, outpath)
    else:
        run_all()
