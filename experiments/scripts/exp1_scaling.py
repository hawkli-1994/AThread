#!/usr/bin/env python3
"""exp1: validate the AThread diagnosis by scaling simulated agent sessions.

A session = a loop of tool-call-like commands (git status / rg / find /
python / node / bash) for a fixed duration. Measure per-command latency,
session throughput, fork/exec rate, context switches, PSI, memory.
"""
import json, os, random, subprocess, sys, tempfile, time

REPO = os.path.join(os.path.dirname(__file__), "..", "fixture", "repo")
REPO = os.path.abspath(REPO)
DURATION = float(os.environ.get("EXP1_DURATION", "15"))

COMMANDS = [
    ("git",    ["git", "status", "--porcelain"], 3),
    ("rg",     ["rg", "-n", "def foo", "--type", "py"], 3),
    ("find",   ["find", ".", "-name", "*.py"], 2),
    ("python", [sys.executable, "-c", "import json,os,pathlib;json.dumps({i:i for i in range(1000)})"], 2),
    ("node",   ["node", "-e", "0"], 1),
    ("bash",   ["bash", "-c", "true"], 4),
    ("cat",    ["cat", ".git/HEAD"], 2),
]
WEIGHTS = [w for _, _, w in COMMANDS]


def session_main(out_path, seed, deadline):
    random.seed(seed)
    results = []
    while time.monotonic() < deadline:
        (name, argv, _) = random.choices(COMMANDS, weights=WEIGHTS, k=1)[0]
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, cwd=REPO, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
            rc = p.returncode
        except Exception as e:
            rc = str(e)
        dt = (time.monotonic() - t0) * 1000
        results.append((name, dt, rc))
    with open(out_path, "w") as f:
        for name, dt, rc in results:
            f.write(json.dumps({"cmd": name, "ms": dt, "rc": rc}) + "\n")


def read_sys():
    s = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("processes"):
                s["forks"] = int(line.split()[1])
            elif line.startswith("ctxt"):
                s["ctxt"] = int(line.split()[1])
            elif line.startswith("cpu "):
                s["cpu_jiffies"] = sum(map(int, line.split()[1:]))
    for p in ("cpu", "io", "memory"):
        try:
            with open(f"/proc/pressure/{p}") as f:
                s[f"psi_{p}"] = int(f.readline().split("total=")[1])
        except Exception:
            s[f"psi_{p}"] = -1
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable"):
                s["mem_avail_kb"] = int(line.split()[1])
    return s


def percentile(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def run_level(n_sessions):
    tmpdir = tempfile.mkdtemp(prefix=f"exp1_n{n}_")
    a = read_sys()
    t_start = time.monotonic()
    procs = []
    deadline = time.monotonic() + DURATION
    for i in range(n_sessions):
        out = os.path.join(tmpdir, f"s{i}.jsonl")
        p = subprocess.Popen([sys.executable, __file__, "--session", out, str(i)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
    for p in procs:
        p.wait()
    wall = time.monotonic() - t_start
    b = read_sys()

    lat = {}   # cmd -> [ms]
    cmds_total = 0
    for fn in os.listdir(tmpdir):
        with open(os.path.join(tmpdir, fn)) as f:
            for line in f:
                r = json.loads(line)
                lat.setdefault(r["cmd"], []).append(r["ms"])
                cmds_total += 1

    agg = {}
    for name, xs in lat.items():
        agg[name] = {"n": len(xs), "p50": round(percentile(xs, .5), 2),
                     "p95": round(percentile(xs, .95), 2)}
    summary = {
        "sessions": n_sessions,
        "wall_s": round(wall, 2),
        "cmds_per_session": round(cmds_total / n_sessions, 1),
        "fork_rate_per_s": round((b["forks"] - a["forks"]) / wall, 1),
        "ctxt_per_s": round((b["ctxt"] - a["ctxt"]) / wall, 1),
        "cpu_busy_pct": round((b["cpu_jiffies"] - a["cpu_jiffies"]) / os.sysconf("SC_CLK_TCK") / wall / os.cpu_count() * 100, 1),
        "psi_cpu_us_per_s": round((b["psi_cpu"] - a["psi_cpu"]) / wall, 1),
        "psi_io_us_per_s": round((b["psi_io"] - a["psi_io"]) / wall, 1),
        "psi_mem_us_per_s": round((b["psi_memory"] - a["psi_memory"]) / wall, 1),
        "mem_avail_drop_mb": round((a["mem_avail_kb"] - b["mem_avail_kb"]) / 1024, 1),
        "latency_by_cmd_ms": agg,
    }
    return summary


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--session":
        session_main(sys.argv[2], int(sys.argv[3]), time.monotonic() + DURATION + 5)
        sys.exit(0)
    out_path = os.path.join(os.path.dirname(__file__), "..", "results", "exp1_scaling.json")
    all_results = []
    for n in (1, 10, 50, 100):
        r = run_level(n)
        all_results.append(r)
        print(json.dumps(r, indent=1))
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=1)
    print("saved:", out_path)
