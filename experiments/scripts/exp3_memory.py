#!/usr/bin/env python3
"""exp3: memory duplication across identical runtimes vs CoW snapshot.

AThread claims concurrent agents duplicate runtime memory and that
prefork/CoW snapshots could dedupe it. Measure real numbers.
"""
import os, subprocess, sys, time

PY = sys.executable
N_PY = int(os.environ.get("EXP3_N", "25"))
HOLD = int(os.environ.get("EXP3_HOLD", "20"))


def pss_kb(pids):
    total, per = 0, []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        v = int(line.split()[1])
                        per.append(v)
                        total += v
        except OSError:
            pass
    return total, per


def rss_kb(pids):
    total, per = 0, []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS"):
                        v = int(line.split()[1])
                        per.append(v)
                        total += v
        except FileNotFoundError:
            pass
    return total, per


def spawn_independent_python():
    return subprocess.Popen(
        [PY, "-c",
         "import json,re,subprocess,pathlib,argparse,os,sys,time;"
         "d={i:[i]*10 for i in range(2000)};time.sleep(%d)" % HOLD],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


print(f"== A) {N_PY} independent python processes (each imports common agent-stack libs) ==")
procs = [spawn_independent_python() for _ in range(N_PY)]
time.sleep(6)  # let them finish importing and enter sleep
pids = [p.pid for p in procs]
tot_rss, per_rss = rss_kb(pids)
tot_pss, per_pss = pss_kb(pids)
print(f"  total RSS {tot_rss/1024:.1f} MB | total PSS {tot_pss/1024:.1f} MB")
print(f"  per-proc PSS min/avg/max: {min(per_pss)/1024:.1f}/{tot_pss/N_PY/1024:.1f}/{max(per_pss)/1024:.1f} MB")
dup_mb = (tot_rss - tot_pss) / 1024
print(f"  already-shared (RSS-PSS, file-backed text pages): {dup_mb:.1f} MB "
      f"({dup_mb/(tot_rss/1024)*100:.0f}% of RSS — kernel already dedupes code)")

print(f"\n== B) {N_PY} children forked from one pre-imported parent (AThread CoW snapshot model) ==")
parent_code = (
    "import json,re,subprocess,pathlib,argparse,os,sys,time,importlib;"
    "d={i:[i]*10 for i in range(2000)};"
    f"[(lambda pid: (time.sleep({HOLD}), os._exit(0)) if pid==0 else None)(os.fork()) for _ in range({N_PY})];"
    f"time.sleep({HOLD})"
)
parent = subprocess.Popen([PY, "-c", parent_code],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(8)
children = [int(x) for x in os.listdir("/proc") if x.isdigit()]
# find children of parent via /proc/<pid>/stat ppid
ppid = parent.pid
kids = []
for x in children:
    try:
        with open(f"/proc/{x}/stat") as f:
            if int(f.read().rsplit(")", 1)[1].split()[1]) == ppid:
                kids.append(int(x))
    except Exception:
        pass
print(f"  found {len(kids)} CoW children of pid {ppid}")
tot_rss2, _ = rss_kb(kids)
tot_pss2, per_pss2 = pss_kb(kids)
print(f"  total RSS {tot_rss2/1024:.1f} MB | total PSS {tot_pss2/1024:.1f} MB")
if len(kids) >= N_PY // 2 and tot_pss2 > 0:
    saved = (tot_pss - tot_pss2) / 1024
    print(f"\n== result ==")
    print(f"  independent total PSS: {tot_pss/1024:.1f} MB")
    print(f"  CoW snapshot total PSS: {tot_pss2/1024:.1f} MB")
    print(f"  saving: {saved:.1f} MB ({saved/(tot_pss/1024)*100:.0f}%) "
          f"~ {saved/N_PY:.2f} MB per session")

parent.terminate()
for p in procs:
    p.terminate()
