#!/usr/bin/env python3
"""exp7: how much of the CoW memory saving survives real work?

Children fork from a warm root, then dirty X MB of memory
(1 byte per 4KB page, mimicking scattered writes), hold, and we
measure total PSS. Independent processes = baseline.
"""
import os, subprocess, sys, time

PY = sys.executable
N = int(os.environ.get("EXP7_N", "25"))
HOLD = 12

def pss_total(pids):
    tot = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        tot += int(line.split()[1])
        except OSError:
            pass
    return tot / 1024

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

parent_tpl = (
    "import json,re,os,sys,time;"
    "d={i:[i]*10 for i in range(2000)};"
    "DIRTY={dirty};\n"
    "buf=bytearray(64*1024*1024);"
    "[(lambda pid: (buf.__setitem__(i*4096,1) for i in range(DIRTY*256)), time.sleep({hold}), os._exit(0)) if pid==0 else None)(os.fork()) "
    "for _ in range({n})];"
    "time.sleep({hold}+8)"
)
# NOTE: child must actually execute the writes; use explicit loop instead of generator trick
parent_tpl = (
    "import json,re,os,sys,time;"
    "d={{i:[i]*10 for i in range(2000)}};"
    "buf=bytearray(64*1024*1024);"
    "DIRTY={dirty};\n"
    "for _ in range({n}):\n"
    "    pid=os.fork()\n"
    "    if pid==0:\n"
    "        for i in range(DIRTY*256): buf[i*4096]=1\n"
    "        time.sleep({hold})\n"
    "        os._exit(0)\n"
    "time.sleep({hold}+10)"
)

def run_cow(dirty_mb, n=N):
    code = parent_tpl.format(dirty=dirty_mb, hold=HOLD, n=n)
    p = subprocess.Popen([PY, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(8)
    kids = kids_of(p.pid)
    err = ""
    if p.poll() is not None:
        err = p.stderr.read().strip()[:300]
    mb = pss_total(kids) if kids else 0
    p.terminate()
    return mb, len(kids), err

def run_independent(n=N):
    code = ("import json,re,os,sys,time;"
            "d={i:[i]*10 for i in range(2000)};"
            "buf=bytearray(64*1024*1024);time.sleep(%d)" % HOLD)
    ps = [subprocess.Popen([PY, "-c", code], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) for _ in range(n)]
    time.sleep(8)
    mb = pss_total([p.pid for p in ps])
    for p in ps:
        p.terminate()
    return mb

print(f"n={N} sessions, warm root pre-imported (json,re) + 8MB heap-ish structure")
base = run_independent()
print(f"independent processes total PSS: {base:.1f} MB  ({base/N:.2f} MB/session)")
for dirty in (0, 1, 4, 16):
    mb, nk, err = run_cow(dirty)
    saved = (base - mb) / base * 100 if mb else 0
    print(f"CoW children, dirty {dirty:2d} MB/session: total PSS {mb:6.1f} MB  "
          f"({mb/max(nk,1):.2f} MB/session)  saving vs independent: {saved:.0f}%"
          + (f"  [parent died: {err}]" if err else ""))
