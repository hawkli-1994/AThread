#!/usr/bin/env python3
"""exp18 (issue #1 P2): per-phase latency breakdown of the AThread fast path.

Where does the remaining warm-path time go? The daemon is instrumented
(athreadd.py, ATHREAD_TIMING=<log>) with monotonic-ns marks:
  req        request JSON fully parsed (daemon side)
  fork       dt = fork() syscall time (parent)
  setup_done dt = child: fd dup + close_fds + signals + cwd/env restore
  pre_exec   dt = child: + fresh __main__/linecache scaffolding
  user_done  dt = child: + user code execution (imports NOT preloaded run here)
  exit_done  dt = child: + atexit handlers + stdio flush
So per child: user_code = user_done - pre_exec, exit = exit_done - user_done.
ipc_read is approximated per request as t(fork mark) - t(req mark) on the
daemon clock (includes remaining socket read + parse + dispatch).

Arms: forkonly (empty warm_modules.txt) vs full (33-module DEFAULT_WARM).
full.user_code < forkonly.user_code quantifies the preload benefit per
command type; fork+setup+ipc is the fixed fast-path overhead. A cold arm
(direct exec, wait4) gives the baseline each phase is compared against.

Run AFTER the timing-instrumented athreadd.py is in place; needs the shim
installed (`athread install`). Writes results/exp18_phase_breakdown.txt.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))
OUT = os.path.join(RESULTS, "exp18_phase_breakdown.txt")
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
ATHREAD_CLI = os.path.abspath(os.path.join(HERE, "..", "..", "athread", "athread"))
DAEMON = os.path.abspath(os.path.join(HERE, "..", "..", "athread", "athreadd.py"))
REAL = "/usr/bin/python3"
TESTS_SRC = os.path.join(FIX, "bugrepo")
N = 50

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin") and "shim_bin" not in p) \
             or "/usr/local/bin:/usr/bin:/bin"

BATTERY = [
    ("trivial",   ["python3", "-c", "pass"], None),
    ("small-import", ["python3", "-c",
                       "import json, tempfile; json.dumps({'a': 1})"], None),
    ("unittest-discover", ["python3", "-m", "unittest", "discover", "-s", "tests"],
     "tests"),
    ("script",    ["python3", "sample_script.py"], "script"),
    ("import-heavy", ["python3", "-c",
                      "import argparse, unittest, json, tempfile, pathlib, "
                      "collections, itertools, functools, typing, ast, pickle"],
     None),
]

SCRIPT_SRC = "import unittest, json\nprint('script ok')\n"

def run4(argv, cwd, env):
    import tempfile as _tf
    out_f = _tf.TemporaryFile(); err_f = _tf.TemporaryFile()
    devnull = os.open(os.devnull, os.O_RDONLY)
    t0 = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(out_f.fileno(), 1); os.dup2(err_f.fileno(), 2)
            os.dup2(devnull, 0); os.close(devnull)
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)
    os.close(devnull)
    while True:
        done, status, r = os.wait4(pid, 0)
        if done == pid:
            break
    out_f.close(); err_f.close()
    return {"wall_ms": (time.monotonic() - t0) * 1000,
            "cpu_ms": (r.ru_utime + r.ru_stime) * 1000}

def start_daemon(state_dir, arm, timing_log):
    os.makedirs(state_dir, exist_ok=True)
    cfg = os.path.join(state_dir, "warm_modules.txt")
    if arm == "forkonly":
        open(cfg, "w").close()
    elif os.path.exists(cfg):
        os.unlink(cfg)
    env = dict(os.environ, ATHREAD_STATE_DIR=state_dir,
               ATHREAD_TIMING=timing_log, PYTHONHASHSEED="0")
    env.pop("VIRTUAL_ENV", None); env.pop("PYTHONPATH", None)
    logf = open(os.path.join(state_dir, "athreadd.log"), "ab")
    p = subprocess.Popen([REAL, DAEMON, "run"], env=env, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=logf, start_new_session=True)
    sock = os.path.join(state_dir, "athreadd.sock")
    for _ in range(200):
        if os.path.exists(sock):
            return p
        time.sleep(0.05)
    raise RuntimeError("daemon start failed")

def stop_daemon(p, state_dir):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    shutil.rmtree(state_dir, ignore_errors=True)

def read_timing(path):
    recs = []
    if os.path.exists(path):
        for line in open(path):
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return recs

def pct(v, p):
    v = sorted(v)
    if not v:
        return float("nan")
    import math
    k = min(len(v) - 1, max(0, math.ceil(p / 100 * len(v)) - 1))
    return v[k]

def main():
    tmp = tempfile.mkdtemp(prefix="exp18_")
    testsdir = os.path.join(tmp, "tests")
    scriptdir = os.path.join(tmp, "scriptws")
    shutil.copytree(TESTS_SRC, testsdir)
    os.makedirs(scriptdir)
    with open(os.path.join(scriptdir, "sample_script.py"), "w") as f:
        f.write(SCRIPT_SRC)

    cwdmap = {"tests": testsdir, "script": scriptdir, None: tmp}
    report = {}
    for arm in ("forkonly", "full"):
        state = os.path.join(tmp, f"state_{arm}")
        tlog = os.path.join(tmp, f"timing_{arm}.jsonl")
        proc = start_daemon(state, arm, tlog)
        env = dict(os.environ,
                   PATH=f"{BIN}:{CLEAN_PATH}",
                   ATHREAD_SOCK=os.path.join(state, "athreadd.sock"),
                   ATHREAD_REAL_PYTHON=REAL)
        walls = defaultdict(list)
        for name, argv, cwdk in BATTERY:
            for _ in range(N):
                r = run4(argv, cwdmap[cwdk], env)
                walls[name].append(r["wall_ms"])
        time.sleep(0.3)
        stop_daemon(proc, state)
        # aggregate phases from the timing log. The battery runs serially, so
        # the n-th "req"/fork pair in the log belongs to the n-th battery run;
        # pair by ORDER, not by timestamp equality.
        recs = read_timing(tlog)
        by_pid = defaultdict(dict)
        forks = []                       # [(t_before_fork, child_pid, fork_dt)]
        for r in recs:
            if r["ev"] == "fork":
                forks.append((r["ts"] - r["dt"], r["child"], r["dt"]))
            elif r["ev"] != "req":
                by_pid[r["pid"]][r["ev"]] = r["dt"]
        forks.sort()
        phases = defaultdict(lambda: defaultdict(list))
        idx = 0
        for name, argv, cwdk in BATTERY:
            for _ in range(N):
                if idx >= len(forks):
                    break
                _t_req, pid, fork_dt = forks[idx]
                d = by_pid.get(pid, {})
                if d.get("exit_done"):
                    phases[name]["ipc_fork"].append(fork_dt / 1e6)
                    phases[name]["setup"].append(d.get("setup_done", 0) / 1e6)
                    phases[name]["dispatch"].append(
                        (d.get("pre_exec", 0) - d.get("setup_done", 0)) / 1e6)
                    phases[name]["user_code"].append(
                        (d.get("user_done", 0) - d.get("pre_exec", 0)) / 1e6)
                    phases[name]["exit"].append(
                        (d.get("exit_done", 0) - d.get("user_done", 0)) / 1e6)
                    phases[name]["child_total"].append(
                        d.get("exit_done", 0) / 1e6)
                idx += 1
        report[arm] = {"walls": {k: v for k, v in walls.items()},
                       "phases": {k: dict(v) for k, v in phases.items()}}
    # cold baseline
    cold_walls = defaultdict(list)
    for name, argv, cwdk in BATTERY:
        for _ in range(N):
            r = run4(argv, cwdmap[cwdk], dict(os.environ, PATH=CLEAN_PATH))
            cold_walls[name].append(r["wall_ms"])
    shutil.rmtree(tmp, ignore_errors=True)

    lines = ["== exp18: warm fast-path phase breakdown (p50 ms, n=%d per cell) ==" % N,
             "phases: ipc_fork (daemon recv->fork done) | setup (fd/signal/cwd/env) |",
             "        dispatch (fresh __main__ scaffolding) | user_code | exit (atexit+flush)",
             ""]
    hdr = f"{'command':20s} {'arm':8s} {'ipc+fork':>9s} {'setup':>7s} {'dispatch':>8s} " \
          f"{'user':>8s} {'exit':>6s} {'child_tot':>9s} {'wall':>8s}"
    for name, argv, cwdk in BATTERY:
        for arm in ("forkonly", "full"):
            ph = report[arm]["phases"].get(name, {})
            w = report[arm]["walls"][name]
            cells = []
            for k in ("ipc_fork", "setup", "dispatch", "user_code", "exit",
                      "child_total"):
                cells.append(pct(ph.get(k, [0]), 50))
            lines.append(hdr if name == BATTERY[0][0] and arm == "forkonly" else "")
            lines.append(f"{name:20s} {arm:8s} " +
                         " ".join(f"{c:9.2f}" if i == 0 else
                                  f"{c:7.2f}" if i == 1 else
                                  f"{c:8.2f}" if i in (2, 3) else
                                  f"{c:6.2f}" if i == 4 else f"{c:9.2f}"
                                  for i, c in enumerate(cells)) +
                         f" {pct(w, 50):8.2f}")
        cw = cold_walls[name]
        fw = pct(report["full"]["walls"][name], 50)
        fo = pct(report["forkonly"]["walls"][name], 50)
        lines.append(f"{'':20s} {'cold':8s} {'':42s} {pct(cw, 50):8.2f}   "
                     f"-> warm/cold wall: forkonly {fo/pct(cw,50):.2f}x "
                     f"full {fw/pct(cw,50):.2f}x")
        lines.append("")
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("->", OUT)

if __name__ == "__main__":
    main()
