#!/usr/bin/env python3
"""replay_real.py: replay the ACTUAL python/node commands observed in real
agent traces — cold exec vs AThread zygote fast path.

Usage:
  python3 replay_real.py --broker        (starts the warm zygote, internal)
  python3 replay_real.py                 (runs the replay + report)
"""
import json, os, socket, statistics, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
SOCK = "/tmp/athread_replay_zygote.sock"
WARM_IMPORTS = ("json", "re", "subprocess", "pathlib", "argparse", "os", "sys",
                "collections", "itertools", "functools", "typing", "ast")


def broker_main():
    import importlib
    for m in WARM_IMPORTS:
        importlib.import_module(m)
    _ = {i: [i] * 10 for i in range(2000)}
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(64)
    print("broker ready", flush=True)
    while True:
        conn, _ = srv.accept()
        req = json.loads(conn.recv(1 << 20))
        pid = os.fork()
        if pid == 0:
            try:
                srv.close()
                os.chdir(req["cwd"])
                sys.argv = req["argv"]
                a = req["argv"]
                if len(a) >= 3 and a[1] == "-c":
                    g = {"__name__": "__main__"}
                    exec(compile(a[2], "<cmd>", "exec"), g)
                else:
                    import runpy
                    runpy.run_path(a[1], run_name="__main__")
                rc = 0
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except Exception:
                rc = 1
            os._exit(rc)
        _, st = os.waitpid(pid, 0)
        conn.sendall(json.dumps({"rc": os.waitstatus_to_exitcode(st)}).encode())
        conn.close()


def zygote_exec(argv, cwd):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall(json.dumps({"argv": argv, "cwd": cwd}).encode())
    s.recv(4096)
    s.close()


def cold_exec(argv, cwd):
    subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def p50(fn, reps=3):
    ts = []
    for _ in range(reps):
        t0 = time.monotonic()
        fn()
        ts.append((time.monotonic() - t0) * 1000)
    return statistics.median(ts)


def main():
    replay = json.load(open(os.path.join(LOGDIR, "replay_list.json")))
    # safety: only python/node with plain args, cap count
    cands = []
    for r in replay:
        if any("\x00" in a for a in r["argv"]):
            continue
        if len(cands) >= 40:
            break
        cands.append(r)

    broker = subprocess.Popen([sys.executable, __file__, "--broker"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        if os.path.exists(SOCK):
            break
        time.sleep(0.05)

    real_bin = {"python3": sys.executable, "python": sys.executable,
                "node": "node", "nodejs": "node"}
    print(f"{'bin':8s} {'argv (truncated)':52s} {'cold ms':>8s} {'warm ms':>8s} {'speedup':>8s}")
    rows = []
    tot_observed = tot_cold = tot_warm = 0.0
    for r in cands:
        argv = [real_bin.get(r["bin"], r["bin"])] + r["argv"][1:]
        is_py = r["bin"] in ("python3", "python")
        try:
            cold = p50(lambda: cold_exec(argv, r["cwd"]))
        except Exception as e:
            print(f"{r['bin']:8s} {str(r['argv'])[:50]:52s} SKIP ({e})")
            continue
        warm = p50(lambda: zygote_exec(argv, r["cwd"])) if is_py else float("nan")
        sp = cold / warm if is_py else float("nan")
        rows.append((r, cold, warm))
        tot_observed += r["observed_ms"] or 0
        tot_cold += cold
        tot_warm += warm if is_py else cold
        desc = " ".join(r["argv"][1:])[:50]
        print(f"{r['bin']:8s} {desc:52s} {cold:8.1f} {warm if is_py else float('nan'):8.1f} "
              f"{sp if is_py else float('nan'):7.1f}x")

    broker.terminate()
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    n_py = sum(1 for r, c, w in rows if r["bin"] in ("python3", "python"))
    print(f"\nreplayed {len(rows)} unique commands ({n_py} python)")
    print(f"observed in-trace python wall: {tot_observed:.0f}ms "
          f"(sum of observed durations of these python execs)")
    print(f"re-measured cold total:        {tot_cold:.0f}ms")
    print(f"projected warm total:          {tot_warm:.0f}ms "
          f"(-> {(1 - tot_warm / max(tot_cold, 1e-9)) * 100:.0f}% startup time saved on python calls)")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--broker":
        broker_main()
    else:
        main()
