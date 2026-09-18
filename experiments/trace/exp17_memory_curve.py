#!/usr/bin/env python3
"""exp17 (issue #1 P1-C): memory break-even curve for the warm-root model.

Questions (from issue #1 P1-C):
- How does total memory scale with concurrently-ALIVE sessions (1/5/20/100)?
- How much of the saving survives when workloads span MULTIPLE runtime
  environments (2/4 independent warm roots, modeling multiple venvs)?
- Where is the break-even point: how many sharing children does one idle
  warm root need before fork+CoW beats independent cold processes?
- How do docker containers compare on a unified per-instance accounting?

Method:
- Warm arms use the REAL daemon+shim: athreadd preloads the warm module set
  and forks each child from the warm root (true CoW sharing, the same
  mechanism as production). Children execute warmup code ending in a sleep
  so they stay alive for measurement; PSS is summed over daemon + live
  children (roots INCLUDED — full resident cost, nothing hidden).
- Cold arm: the same child code run as N independent /usr/bin/python3.
- Every child (cold or warm) runs IDENTICAL code: real imports
  (unittest/json/argparse/tempfile), a JSON round-trip on 3k records, a
  2 MB touched buffer (page dirtying), then sleeps ALIVE_S. Warm children
  skip the import cost via the preloaded root.
- Multi-env: k daemons with the same config; children round-robin across
  their sockets (models k venvs, each needing its own warm root).
- docker arm: N containers each running the same code; memory via
  `docker stats` (cgroup usage). DECLARED: cgroup usage != PSS; the docker
  column is its own accounting and not directly comparable.
- Measurement at MEASURE_AT seconds into child life (GC has run, pages aged).

Usage: python3 exp17_memory_curve.py   (writes results/exp17_memory_curve.{txt,json})
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))
OUT = os.path.join(RESULTS, "exp17_memory_curve.txt")
RAW = os.path.join(RESULTS, "exp17_memory_curve.json")
PY = "/usr/bin/python3"
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
ATHREAD_CLI = os.path.abspath(os.path.join(HERE, "..", "..", "athread", "athread"))
ALIVE_S = 12
MEASURE_AT = 6
TIERS = [1, 5, 20, 100]
ENVS = [1, 2, 4]
DOCKER_TIERS = [1, 5, 20, 100]
DOCKER_IMG = "mirror.gcr.io/library/python:3.13-slim"

# the warm root preloads exactly what the children import
WARM_MODS = ["unittest", "json", "argparse", "tempfile"]

CHILD_CODE = r'''
import unittest, json, argparse, tempfile, os, time, sys
data = [{"id": i, "v": list(range(50))} for i in range(3000)]
s = json.dumps(data)
_ = json.loads(s)
buf = bytearray(2 * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
del buf
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 12)
'''

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin") and "shim_bin" not in p) \
             or "/usr/local/bin:/usr/bin:/bin"

def pss_kb(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:
        return 0
    return 0

def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(") ", 1)[1].split()[1])
    except (OSError, IndexError):
        return -1

def measure_pids(pids):
    return sum(pss_kb(p) for p in pids) / 1024.0

def daemon_children(daemon_pids):
    out = []
    for pid in daemon_pids:
        try:
            for name in os.listdir("/proc"):
                if name.isdigit() and ppid_of(int(name)) == pid:
                    out.append(int(name))
        except OSError:
            pass
    return out

def start_daemon(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "warm_modules.txt"), "w") as f:
        f.write("\n".join(WARM_MODS) + "\n")
    env = dict(os.environ, ATHREAD_STATE_DIR=state_dir)
    r = subprocess.run([PY, ATHREAD_CLI, "start"], env=env,
                       capture_output=True, text=True, timeout=60)
    sock = os.path.join(state_dir, "athreadd.sock")
    for _ in range(200):
        if os.path.exists(sock):
            pid = int(open(os.path.join(state_dir, "athreadd.pid")).read().strip())
            return pid
        time.sleep(0.05)
    raise RuntimeError(f"daemon failed to start: {r.stdout} {r.stderr}")

def stop_daemon(state_dir):
    env = dict(os.environ, ATHREAD_STATE_DIR=state_dir)
    subprocess.run([PY, ATHREAD_CLI, "stop"], env=env,
                   capture_output=True, timeout=30)
    try:
        pid = int(open(os.path.join(state_dir, "athreadd.pid")).read().strip())
        os.kill(pid, 9)
    except (OSError, ValueError):
        pass
    shutil.rmtree(state_dir, ignore_errors=True)

def spawn_children(n, socks):
    """n shim-dispatched children round-robined across daemon sockets."""
    procs = []
    for i in range(n):
        env = dict(os.environ,
                   PATH=f"{BIN}:{CLEAN_PATH}",
                   ATHREAD_SOCK=socks[i % len(socks)],
                   ATHREAD_REAL_PYTHON=PY)
        procs.append(subprocess.Popen(
            ["python3", "-c", CHILD_CODE, str(ALIVE_S)],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return procs

def arm_host(tier, nroots=None):
    """nroots=None -> cold independent processes; else k warm daemons."""
    if nroots is None:
        procs = [subprocess.Popen([PY, "-c", CHILD_CODE, str(ALIVE_S)],
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
                 for _ in range(tier)]
        time.sleep(MEASURE_AT)
        mb = measure_pids([p.pid for p in procs])
        for p in procs:
            p.terminate()
        for p in procs:
            try: p.wait(timeout=10)
            except Exception: p.kill()
        return mb
    tmp = tempfile.mkdtemp(prefix="exp17d_")
    dpids, socks = [], []
    try:
        for k in range(nroots):
            sd = os.path.join(tmp, f"s{k}")
            dpids.append(start_daemon(sd))
            socks.append(os.path.join(sd, "athreadd.sock"))
        procs = spawn_children(tier, socks)
        time.sleep(2)                     # let all children fork from roots
        kids = daemon_children(dpids)
        time.sleep(max(0, MEASURE_AT - 2))
        mb = measure_pids(dpids + kids)
        for p in procs:
            p.terminate()
        for p in procs:
            try: p.wait(timeout=10)
            except Exception: p.kill()
        return mb
    finally:
        for k in range(nroots):
            stop_daemon(os.path.join(tmp, f"s{k}"))
        shutil.rmtree(tmp, ignore_errors=True)

def parse_memusage(s):
    m = re.match(r"([\d.]+)(B|KiB|MiB|GiB)", s)
    if not m:
        return 0.0
    v, unit = float(m.group(1)), m.group(2)
    return v * {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[unit]

def arm_docker(tier):
    names = []
    # large tiers: container startup skew (serial docker run, ~0.3s each on
    # WSL2) can exceed the child's own lifetime, so late containers exit
    # before measurement. Give big tiers a long sleep and measure promptly
    # once all are running.
    alive = 120 if tier >= 50 else ALIVE_S
    code = CHILD_CODE.replace(f"else {ALIVE_S}", f"else {alive}")
    for i in range(tier):
        name = f"exp17m_{os.getpid()}_{i}"
        r = subprocess.run(["docker", "run", "-d", "--name", name,
                            DOCKER_IMG, "python", "-c", code, str(alive)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"  docker run {name} failed: {r.stderr.strip()[:120]}")
        names.append(name)
    # wait until every container is actually running (avoids 0B stats)
    for _ in range(240):
        rr = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                            capture_output=True, text=True, timeout=60)
        n_running = sum(1 for l in rr.stdout.splitlines()
                        if l.startswith(f"exp17m_{os.getpid()}_"))
        if n_running >= tier:
            break
        time.sleep(2)
    time.sleep(MEASURE_AT)
    total = 0.0
    r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                        "{{.Name}}\t{{.MemUsage}}"] + names,
                       capture_output=True, text=True, timeout=180)
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            total += parse_memusage(parts[1].strip())
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=60)
    return total

def main():
    have_docker = subprocess.run(
        ["docker", "image", "inspect", DOCKER_IMG],
        capture_output=True).returncode == 0
    rows = []
    print("== exp17 memory break-even curve (v2: children forked from warm roots) ==",
          flush=True)
    for tier in TIERS:
        cold = arm_host(tier)
        row = {"tier": tier, "cold_mb": round(cold, 1)}
        print(f"tier {tier:3d}: cold {cold:7.1f} MB", flush=True)
        for k in ENVS:
            warm = arm_host(tier, k)
            row[f"warm_{k}env_mb"] = round(warm, 1)
            row[f"save_{k}env_mb"] = round(cold - warm, 1)
            row[f"save_{k}env_pct"] = round((cold - warm) / max(cold, 1e-9) * 100, 1)
            print(f"          warm/{k}env {warm:7.1f} MB  "
                  f"save {cold-warm:6.1f} MB ({row[f'save_{k}env_pct']:6.1f}%)",
                  flush=True)
        if have_docker:
            dm = arm_docker(tier)
            row["docker_mb"] = round(dm, 1)
            print(f"          docker {dm:7.1f} MB (cgroup usage, != PSS)", flush=True)
        rows.append(row)
        json.dump(rows, open(RAW, "w"), indent=1)
    json.dump(rows, open(RAW, "w"), indent=1)

    # idle full root cost for the break-even formula (33-module default set)
    full_mods = WARM_MODS + [
        "os", "sys", "io", "re", "math", "time", "datetime", "glob",
        "fnmatch", "shutil", "pathlib", "subprocess", "hashlib",
        "collections", "itertools", "functools", "typing", "ast",
        "unittest.mock", "pickle", "socket", "struct", "string",
        "textwrap", "copy", "warnings", "traceback", "importlib", "runpy"]
    code = "import importlib, time\n" + \
           "\n".join(f"importlib.import_module({m!r})" for m in full_mods) + \
           "\ntime.sleep(4)\n"
    p = subprocess.Popen([PY, "-c", code], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    daemon_mb = pss_kb(p.pid) / 1024.0
    p.terminate(); p.wait(timeout=10)
    pc = [r for r in rows if r["tier"] == 5][0]
    per_child = pc["save_1env_mb"] / 5 if pc["save_1env_mb"] > 0 else 0
    be = daemon_mb / per_child if per_child > 0 else float("inf")

    with open(OUT, "w") as f:
        f.write("== exp17: memory break-even curve v2 ==\n")
        f.write("children forked from warm roots via real daemon+shim; "
                "PSS summed over roots+children (roots INCLUDED)\n")
        f.write(f"children held alive {ALIVE_S}s, measured at {MEASURE_AT}s; "
                f"each child: unittest/json/argparse/tempfile imports + "
                f"3k-record JSON round-trip + 2MB touched buffer\n\n")
        hdr = "tier |   cold | warm/1env | warm/2env | warm/4env"
        if have_docker:
            hdr += " | docker(cgroup)"
        f.write(hdr + "\n")
        for r in rows:
            line = (f"{r['tier']:4d} | {r['cold_mb']:6.1f} "
                    f"| {r['warm_1env_mb']:9.1f} | {r['warm_2env_mb']:9.1f} "
                    f"| {r['warm_4env_mb']:9.1f}")
            if have_docker:
                line += f" | {r.get('docker_mb', 0):9.1f}"
            f.write(line + "\n")
        f.write("\n-- saving vs cold --\n")
        for r in rows:
            f.write(f"tier {r['tier']:3d}: 1env {r['save_1env_mb']:6.1f}MB "
                    f"({r['save_1env_pct']:6.1f}%)  "
                    f"2env {r['save_2env_mb']:6.1f}MB ({r['save_2env_pct']:6.1f}%)  "
                    f"4env {r['save_4env_mb']:6.1f}MB ({r['save_4env_pct']:6.1f}%)\n")
        f.write(f"\nfull 33-module warm root idle PSS: {daemon_mb:.1f} MB\n")
        f.write(f"per-child saving at tier 5 (1 env): {per_child:.2f} MB\n")
        f.write(f"memory break-even (1 env): one warm root pays for itself at "
                f"~{be:.1f} concurrently-sharing children\n")
        f.write("multi-env: cost scales linearly with #roots (k x idle root); "
                "children only share within their own root's pool\n")
        f.write("docker: cgroup usage incl. container overhead; NOT PSS-comparable\n")
    print("->", OUT)

if __name__ == "__main__":
    main()
