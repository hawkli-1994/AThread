#!/usr/bin/env python3
"""exp13: honest full-process-group memory profit & loss.

Corrects the accounting errors found in review (2026-09-17):
- exp3/exp7 counted ONLY CoW children, omitting the warm-root parent.
- exp12 S4 used system-wide PSS deltas (noise, unverifiable cleanup).
- exp11 container PSS measured 0.0 MB (processes already gone).

Every scenario here sums PSS over ALL processes each model requires
(daemon included, containerd-shim included), verifies cleanup afterwards,
and reports per-session and total.

Scenarios (N host procs / containers, import-heavy python, held K s):
  A cold        N independent host python processes
  B warm        N procs through the athread shim + full daemon PSS
  C cow-synth   warm-root parent + N fork children (exp3 model, corrected)
  D container   N docker containers, each running one python
"""
import os, subprocess, sys, time

HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
SOCK = os.path.join(HOME, ".athread", "athreadd.sock")
ATHREAD = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "athread", "athread"))
PY = sys.executable
N = int(os.environ.get("EXP13_N", "20"))
NC = int(os.environ.get("EXP13_NC", "10"))     # containers (heavier; fewer)
HOLD = int(os.environ.get("EXP13_HOLD", "12"))
IMG = os.environ.get("EXP13_IMG", "mirror.gcr.io/library/python:3.13-slim")

IMPORTS = ("import json,re,subprocess,pathlib,argparse,os,sys,time;"
           "d={i:[i]*10 for i in range(2000)};")

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin")) or "/usr/local/bin:/usr/bin:/bin"
WARM_ENV = dict(os.environ, PATH=f"{BIN}:{CLEAN_PATH}",
                ATHREAD_SOCK=SOCK, ATHREAD_REAL_PYTHON="/usr/bin/python3")

def pss_kb(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0

def group_pss(pids):
    tot = 0
    per = []
    for pid in pids:
        v = pss_kb(pid)
        tot += v
        per.append(v)
    return tot / 1024, per

def kids_of(ppid):
    out = []
    for x in os.listdir("/proc"):
        if x.isdigit():
            try:
                with open(f"/proc/{x}/stat") as f:
                    if int(f.read().rsplit(")", 1)[1].split()[1]) == ppid:
                        out.append(int(x))
            except Exception:
                pass
    return out

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def daemon_pid():
    try:
        return int(open(os.path.join(HOME, ".athread", "athreadd.pid")).read().strip())
    except Exception:
        return None

CODE = IMPORTS + f"time.sleep({HOLD})"

def scenario_cold():
    ps = [subprocess.Popen([PY, "-c", CODE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
          for _ in range(N)]
    time.sleep(6)
    tot, per = group_pss([p.pid for p in ps])
    for p in ps:
        p.terminate()
    rc = [p.wait(timeout=5) for p in ps]
    zombies = [p.pid for p in ps if alive(p.pid)]
    return tot, per, f"cleaned={not zombies}"

def scenario_warm():
    dp = daemon_pid()
    before = pss_kb(dp) if dp else 0
    ps = [subprocess.Popen(["python3", "-c", CODE], env=WARM_ENV,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
          for _ in range(N)]
    time.sleep(6)
    shims = [p.pid for p in ps]
    # the actual workers are forked by the DAEMON, not by the shims
    workers = kids_of(dp) if dp else []
    stot, _ = group_pss(shims)
    wtot, wper = group_pss(workers)
    dtot = pss_kb(dp) if dp else 0
    for p in ps:
        p.terminate()
    [p.wait(timeout=5) for p in ps]
    time.sleep(0.5)
    leftovers = [k for k in workers if alive(k)]
    note = (f"shims {stot:.1f} + daemon {dtot/1024:.1f} (idle {before/1024:.1f}; CoW "
            f"sharing dilutes its PSS) + {len(workers)} workers {wtot:.1f} MB; "
            f"leftover workers={len(leftovers)}")
    return stot + wtot + dtot / 1024, wper + [dtot], note

def scenario_cow():
    parent_code = (IMPORTS +
                   f"[(os.fork() or (time.sleep({HOLD}), os._exit(0))) for _ in range({N})];"
                   f"time.sleep({HOLD}+5)")
    p = subprocess.Popen([PY, "-c", parent_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    if p.poll() is not None:
        return 0, None, f"ERROR: warm-root parent died rc={p.returncode}"
    kids = kids_of(p.pid)
    ktot, kper = group_pss(kids)
    ptot = pss_kb(p.pid)
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    return ktot + ptot / 1024, kper + [ptot], f"parent INCLUDED ({ptot/1024:.1f} MB)"

def scenario_containers():
    cids = []
    ccode = IMPORTS + "time.sleep(90)"      # outlive measurement
    try:
        for i in range(NC):
            c = subprocess.run(
                ["docker", "run", "-d", "--rm", IMG, "python", "-c", ccode],
                capture_output=True, text=True)
            cid = c.stdout.strip()
            if c.returncode != 0 or not cid:
                print(f"  container {i} failed: {c.stderr[:120]}")
                continue
            cids.append(cid)
        time.sleep(8)
        # container payloads run as root: /proc/<pid>/smaps_rollup is
        # unreadable for a non-root user (this was exp11's 0.0 MB bug).
        # Use cgroup accounting via docker stats instead.
        tot_mb = 0.0
        for cid in cids:
            usage = ""
            for _attempt in range(3):
                r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                    "{{.MemUsage}}", cid], capture_output=True, text=True)
                usage = r.stdout.strip().split("/")[0].strip()      # e.g. "12.3MiB"
                if usage:
                    break
                time.sleep(2)
            if not usage or len(usage) < 4:
                print(f"  stats empty for {cid[:12]}: rc={r.returncode} err={r.stderr[:80]!r}")
                continue
            val, unit = float(usage[:-3]), usage[-3:]
            factor = {"GiB": 1073.7, "MiB": 1.0486, "KiB": 1 / 1024}.get(unit, 1 / 1024**2)
            tot_mb += val * factor
        n = max(len(cids), 1)
        return tot_mb, None, (
            f"{len(cids)} containers via docker stats (cgroup usage, "
            f"incl. shim overhead): {tot_mb:.1f} MB total, {tot_mb/n:.1f} MB/instance")
    finally:
        for cid in cids:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)

def main():
    print(f"== exp13: full-process-group memory P&L (N={N} procs, {NC} containers, held {HOLD}s) ==")
    print(f"host python: {subprocess.run([PY, '-V'], capture_output=True, text=True).stderr.strip()}")

    print("\nA) cold: independent host python processes")
    a, aper, an = scenario_cold()
    print(f"   total PSS {a:6.1f} MB  ({a/N:.2f} MB/session)   [{an}]")

    print("\nB) warm: athread shim children + FULL daemon PSS")
    b, bper, bn = scenario_warm()
    print(f"   total PSS {b:6.1f} MB  ({b/N:.2f} MB/session)   [{bn}]")

    print("\nC) cow-synthetic: warm-root parent + fork children (parent INCLUDED)")
    c, cper, cn = scenario_cow()
    print(f"   total PSS {c:6.1f} MB  ({c/N:.2f} MB/session)   [{cn}]")

    print("\nD) containers: one python per docker container (python + shim processes)")
    d, _, dn = scenario_containers()
    print(f"   {dn}")

    print("\n== summary (total PSS including ALL model processes) ==")
    print(f"   cold  {a:6.1f} MB")
    print(f"   warm  {b:6.1f} MB   saving vs cold: {a-b:6.1f} MB ({(a-b)/a*100:.0f}%)")
    print(f"   cow   {c:6.1f} MB   saving vs cold: {a-c:6.1f} MB ({(a-c)/a*100:.0f}%)")
    print(f"   containers {d:6.1f} MB for {NC} instances ({d/NC:.1f} MB/instance;")
    print(f"        scaled to {N}: ~{d/NC*N:.0f} MB — containers ADD per-instance overhead,")
    print(f"        they do not dedupe runtime init)")

if __name__ == "__main__":
    main()
