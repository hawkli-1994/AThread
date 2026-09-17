#!/usr/bin/env python3
"""expA: what fraction of REAL agent invocations would the AThread v0.1
shim actually serve (hit rate)?

Applies the shim's exact eligibility rules to every exec recovered from the
14 real agent sessions (repaired traces), and reports:
  - how many execs are python at all (v0.1 only accelerates python)
  - of those, how many are eligible (no venv/PYTHONPATH, known form)
  - which forms (-c / -m / script / stdin / absolute-path interpreter)
  - observed wall time of eligible vs ineligible python calls
"""
import glob, json, os
from collections import Counter

REPAIRDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "repaired")

def classify(argv):
    """Mirror athread/shim.c eligible() for python* binaries."""
    if len(argv) < 2:
        return "stdin", True
    a1 = argv[1]
    if a1 == "-c":
        return "-c", len(argv) >= 3
    if a1 == "-m":
        return "-m", len(argv) >= 3
    if a1.startswith("-"):
        return f"flag {a1}", False
    return "script", os.path.isfile(argv[1])

tot = Counter(); py = Counter()
py_forms = Counter(); py_dur = Counter()
venv_blocked = 0
n_py = n_py_eligible = 0
eligible_dur = ineligible_dur = 0.0
for p in sorted(glob.glob(os.path.join(REPAIRDIR, "*.jsonl"))):
    starts, ends = {}, {}
    for line in open(p):
        e = json.loads(line)
        if e["ev"] == "start":
            starts[e["pid"]] = e
        else:
            ends.setdefault(e["pid"], e)
    for pid, s in starts.items():
        e = ends.get(pid)
        dur = e["dur_ms"] if e else 0
        if dur and dur > 120000:
            continue  # agent runtime itself
        b = s["bin"]
        tot[b] += 1
        if b in ("node", "nodejs"):
            continue                      # v0.1 does NOT accelerate node
        if b not in ("python3", "python"):
            continue
        n_py += 1
        form, elig = classify(s["argv"])
        if s.get("venv"):
            elig = False
            venv_blocked += 1
        py_forms[form] += 1
        if elig:
            n_py_eligible += 1
            eligible_dur += dur or 0
        else:
            ineligible_dur += dur or 0
        py_dur["elig" if elig else "inelig"] += dur or 0

n_tot = sum(tot.values())
print(f"total recoverable execs:        {n_tot}")
print(f"python* execs:                  {n_py} ({n_py/n_tot*100:.1f}%)")
print(f"  of which shim-eligible:       {n_py_eligible}")
print(f"  blocked by venv:              {venv_blocked}")
print(f"  forms: {dict(py_forms)}")
print(f"  observed wall: eligible {eligible_dur:.0f}ms | ineligible {ineligible_dur:.0f}ms")
print(f"  node* execs (v0.1 does NOT accelerate): {tot.get('node',0)+tot.get('nodejs',0)}")
print(f"\nall bins: {tot.most_common(10)}")
print(f"\nHIT RATE for v0.1 (python-only scope): {n_py_eligible}/{n_py} "
      f"python calls = {n_py_eligible/max(n_py,1)*100:.0f}% of python calls, "
      f"but python is only {n_py/n_tot*100:.0f}% of all calls")
