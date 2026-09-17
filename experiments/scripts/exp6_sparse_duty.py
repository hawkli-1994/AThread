#!/usr/bin/env python3
"""exp6: sparse duty-cycle — the realistic agent workload.

100 sessions, each thinks 0.5–3s (uniform) between tool calls.
Condition A (baseline): every command is a cold exec.
Condition B (AThread-sim): python commands go through a real
fork-broker zygote (warm, pre-imported, CoW); native tools unchanged.

Measures P50/P95/P99 per command, CPU busy cores, ctxt/s, throughput.
"""
import json, os, random, socket, statistics, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "fixture", "repo"))
DURATION = float(os.environ.get("EXP6_DURATION", "40"))
N_SESSIONS = int(os.environ.get("EXP6_N", "100"))
SOCK = "/tmp/athread_zygote.sock"

PYCODE = "import json,os,sys,pathlib,re,argparse;d={i:[i]*10 for i in range(2000)};json.dumps(d)"

PROFILE = [  # (kind, weight) — guessed real-agent command profile
    ("git", 28), ("rg", 22), ("python", 18), ("node", 12), ("bash", 8), ("cat", 12),
]

def pick():
    ks = [k for k, _ in PROFILE]
    ws = [w for _, w in PROFILE]
    return random.choices(ks, weights=ws, k=1)[0]


def argv_for(kind):
    return {
        "git":    ["git", "status", "--porcelain"],
        "rg":     ["rg", "-n", "def foo", "--type", "py", "."],
        "python": [sys.executable, "-c", PYCODE],
        "node":   ["node", "-e", "0"],
        "bash":   ["bash", "-c", "true"],
        "cat":    ["cat", ".git/HEAD"],
    }[kind]


# ---------------- zygote broker ----------------
WARM_IMPORTS = ("json", "re", "subprocess", "pathlib", "argparse", "os", "sys",
                "collections", "itertools", "functools", "typing", "dataclasses")

def broker_main():
    import importlib
    for m in WARM_IMPORTS:
        importlib.import_module(m)
    _ = {i: [i] * 10 for i in range(2000)}  # warm heap, like a real session root
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(128)
    sys.stderr.write("zygote ready\n"); sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        req = json.loads(conn.recv(65536))
        pid = os.fork()
        if pid == 0:
            try:
                srv.close()
                os.chdir(req["cwd"])
                os.environ.update(req.get("env", {}))
                sys.argv = ["python", "-c", req["code"]]
                g = {"__name__": "__main__"}
                exec(compile(req["code"], "<agent-cmd>", "exec"), g)
                rc = 0
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except Exception:
                rc = 1
            os._exit(rc)
        _, status = os.waitpid(pid, 0)
        conn.sendall(json.dumps({"rc": os.waitstatus_to_exitcode(status)}).encode())
        conn.close()


def run_python_zygote(code):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall(json.dumps({"cwd": REPO, "code": code}).encode())
    s.recv(65536)
    s.close()


# ---------------- session worker ----------------
def session_main(out_path, seed, use_zygote):
    random.seed(seed)
    deadline = time.monotonic() + DURATION
    results = []
    while time.monotonic() < deadline:
        time.sleep(random.uniform(0.5, 3.0))
        if time.monotonic() >= deadline:
            break
        kind = pick()
        t0 = time.monotonic()
        if kind == "python" and use_zygote:
            run_python_zygote(PYCODE)
        else:
            subprocess.run(argv_for(kind), cwd=REPO, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        results.append((kind, (time.monotonic() - t0) * 1000))
    with open(out_path, "w") as f:
        for k, ms in results:
            f.write(json.dumps({"cmd": k, "ms": ms}) + "\n")


def cpu_busy_cores(dt):
    def snap():
        busy = 0
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu") and not line.startswith("cpu "):
                    v = list(map(int, line.split()[1:]))
                    busy += sum(v) - v[3] - (v[4] if len(v) > 4 else 0)
        return busy
    a, t0 = snap(), time.monotonic()
    time.sleep(dt)
    b, t1 = snap(), time.monotonic()
    return (b - a) / 100 / (t1 - t0)


def percentile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0.0


def run_condition(label, use_zygote):
    tmpdir = f"/tmp/exp6_{label}"
    os.makedirs(tmpdir, exist_ok=True)
    broker = None
    if use_zygote:
        broker = subprocess.Popen([sys.executable, __file__, "--broker"],
                                  stderr=subprocess.DEVNULL)
        for _ in range(200):
            if os.path.exists(SOCK):
                break
            time.sleep(0.05)
    n_ctxt0 = int(open("/proc/stat").read().split("ctxt ")[1].split()[0])
    forks0 = int(open("/proc/stat").read().split("processes ")[1].split()[0])
    t_start = time.monotonic()
    procs = [subprocess.Popen([sys.executable, __file__, "--session",
                               f"{tmpdir}/s{i}.jsonl", str(i), str(int(use_zygote))],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for i in range(N_SESSIONS)]
    cpu_samples = []
    while any(p.poll() is None for p in procs):
        cpu_samples.append(cpu_busy_cores(1.0))
    wall = time.monotonic() - t_start
    if broker:
        broker.terminate()
        if os.path.exists(SOCK):
            os.unlink(SOCK)
    stat = open("/proc/stat").read()
    n_ctxt1 = int(stat.split("ctxt ")[1].split()[0])
    forks1 = int(stat.split("processes ")[1].split()[0])

    lat = {}
    total = 0
    for fn in os.listdir(tmpdir):
        with open(os.path.join(tmpdir, fn)) as f:
            for line in f:
                r = json.loads(line)
                lat.setdefault(r["cmd"], []).append(r["ms"])
                total += 1
    all_ms = [m for xs in lat.values() for m in xs]
    summary = {
        "condition": label, "sessions": N_SESSIONS, "wall_s": round(wall, 1),
        "total_cmds": total,
        "cmds_per_session": round(total / N_SESSIONS, 1),
        "python_cmds": len(lat.get("python", [])),
        "fork_rate_per_s": round((forks1 - forks0) / wall, 1),
        "ctxt_per_s": round((n_ctxt1 - n_ctxt0) / wall, 1),
        "cpu_busy_cores_avg": round(sum(cpu_samples) / len(cpu_samples), 2),
        "p50_all": round(percentile(all_ms, .5), 2),
        "p95_all": round(percentile(all_ms, .95), 2),
        "p99_all": round(percentile(all_ms, .99), 2),
        "python_p50": round(percentile(lat.get("python", []), .5), 2),
        "python_p95": round(percentile(lat.get("python", []), .95), 2),
        "python_p99": round(percentile(lat.get("python", []), .99), 2),
        "latency_by_cmd_p50": {k: round(percentile(v, .5), 2) for k, v in sorted(lat.items())},
    }
    print(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--broker":
        broker_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "--session":
        session_main(sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1")
    else:
        out = os.path.join(HERE, "..", "results", "exp6_sparse_duty.json")
        res = [run_condition("A_baseline", False),
               run_condition("B_athread_sim", True)]
        with open(out, "w") as f:
            json.dump(res, f, indent=1)
        print("saved:", os.path.abspath(out))
