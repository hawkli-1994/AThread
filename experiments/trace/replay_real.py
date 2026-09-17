#!/usr/bin/env python3
"""replay_real.py v2: replay the python commands observed in real agent
traces through the REAL AThread v0.1 prototype (PATH shim + athreadd),
cold vs warm, with strict equivalence checks.

Lessons applied from review (2026-09-17):
- The old replay executed `runpy.run_path("-m")` for -m commands: instant
  failure timed as "21x speedup". RETRACTED. Here the warm path is the real
  shim/daemon, and every run's exit code, stdout, stderr, and sandbox file
  side effects are compared against cold real python.
- `python3 -` (stdin) content was never captured: NON-REPLAYABLE, retracted.
- /tmp/*.py one-off agent scripts are lost: reported as observed-only.
- `-m unittest discover` is reconstructed against the bugrepo fixture
  (agent-written tests in the original workspace are gone); labeled as such.

Usage:  python3 replay_real.py
Requires: running daemon (`athread start`), fixtures generated.
"""
import json, os, shutil, statistics, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))
REPLAY_LIST = os.path.join(HERE, "logs", "replay_list.json")
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
SOCK = os.path.join(HOME, ".athread", "athreadd.sock")
REPS = int(os.environ.get("REPLAY_REPS", "7"))

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin")) or "/usr/local/bin:/usr/bin:/bin"
COLD_ENV = dict(os.environ, PATH=CLEAN_PATH, PYTHONHASHSEED="0")
# PYTHONHASHSEED=0 on both sides: the warm root must pin it (documented
# caveat), and without aligning it, hash-order-dependent output reprs
# (dict/set in assertion messages) would false-positive as divergence.
WARM_ENV = dict(os.environ, PATH=f"{BIN}:{CLEAN_PATH}",
                ATHREAD_SOCK=SOCK, ATHREAD_REAL_PYTHON="/usr/bin/python3",
                PYTHONHASHSEED="0")
for e in (WARM_ENV, COLD_ENV):
    for k in ("VIRTUAL_ENV", "PYTHONPATH", "CONDA_PREFIX"):
        e.pop(k, None)

# synthetic equivalents of the lost /tmp/*.py agent scripts, written against
# a workspace copy; each matches the observed role (import-heavy verify pass)
SYNTHETIC = {
    "verify_convention": r'''
import json, re, os, sys, pathlib
bad = []
for p in pathlib.Path(".").rglob("*.py"):
    if not re.match(r"^[a-z_][a-z0-9_]*\.py$", p.name):
        bad.append(str(p))
print(json.dumps({"checked": sum(1 for _ in pathlib.Path(".").rglob("*.py")),
                  "violations": len(bad)}))
''',
    "verify_fixture": r'''
import json, os, re, pathlib
files = list(pathlib.Path("src").rglob("*.py"))
mods = set()
for f in files:
    src = f.read_text()
    mods.update(re.findall(r"^(?:from|import)\s+([a-zA-Z_][\w.]*)", src, re.M))
print(json.dumps({"files": len(files), "imports": sorted(mods)[:10]}))
''',
    "loc_stats": r'''
import pathlib, json, collections
c = collections.Counter()
for p in pathlib.Path(".").rglob("*.py"):
    c[p.suffix] += sum(1 for _ in p.open())
print(json.dumps(dict(c)))
''',
    "check_modules": r'''
import json, pathlib, ast, sys
mods = set()
for p in pathlib.Path("src").rglob("*.py"):
    tree = ast.parse(p.read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
print(json.dumps(sorted(mods)))
''',
    "analyze_even": r'''
# compute-heavy on purpose: honest lower bound (startup tax is small share)
import pathlib
total = 0
for p in pathlib.Path("src").rglob("*.py"):
    src = p.read_text()
    total += sum(1 for i in range(len(src)) if src[:i].count("(") % 2 == 0)
print("even-paren-prefix count:", total)
''',
    "verify_burst1": r'''
import pathlib, json, re
n = 0
for p in pathlib.Path(".").rglob("*"):
    if p.is_file() and re.search(r"\d", p.name):
        n += 1
print(json.dumps({"numeric_names": n}))
''',
}

def snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            if fn.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root)
            try:
                with open(p, "rb") as f:
                    out[rel] = f.read()
            except OSError:
                pass
    return out

def one_run(mode, argv, sandbox):
    env = WARM_ENV if mode == "warm" else COLD_ENV
    before = snapshot(sandbox)
    t0 = time.monotonic()
    p = subprocess.run(argv, cwd=sandbox, env=env, capture_output=True, text=True, timeout=120)
    dt = (time.monotonic() - t0) * 1000
    after = snapshot(sandbox)
    side_effects = {k: v for k, v in after.items() if k not in before} != {}
    # sandboxes differ per mode by construction; path reprs inside output
    # (tracebacks, pwd) are test artifacts, not behavior differences
    norm = lambda s: s.replace(sandbox, "<SB>")
    return {"rc": p.returncode, "out": norm(p.stdout), "err": norm(p.stderr),
            "ms": dt, "side_effects": side_effects}

def bench_pair(name, argv_builder, src, label):
    """Run cold & warm in fresh sandbox copies; check equivalence every rep."""
    cold_ms, warm_ms = [], []
    equiv = True
    detail = ""
    for rep in range(REPS):
        results = {}
        for mode in ("cold", "warm"):
            sandbox = tempfile.mkdtemp(prefix=f"replay_{mode}_")
            try:
                shutil.rmtree(sandbox)
                shutil.copytree(src, sandbox)
                r = one_run(mode, argv_builder(sandbox), sandbox)
                results[mode] = r
                (cold_ms if mode == "cold" else warm_ms).append(r["ms"])
            finally:
                shutil.rmtree(sandbox, ignore_errors=True)
        c, w = results["cold"], results["warm"]
        if not (c["rc"] == w["rc"] and c["out"] == w["out"] and c["err"] == w["err"]):
            equiv = False
            detail = (f"rep{rep}: rc {c['rc']}/{w['rc']} "
                      f"out {c['out'][:60]!r}/{w['out'][:60]!r} "
                      f"err {c['err'][:60]!r}/{w['err'][:60]!r}")
            break
        if c["side_effects"] != w["side_effects"]:
            equiv = False
            detail = f"rep{rep}: side-effect presence differs cold={c['side_effects']} warm={w['side_effects']}"
            break
    pc, pw = statistics.median(cold_ms), statistics.median(warm_ms)
    sp = pc / pw if pw > 0 else float("nan")
    status = "EQUIV-OK" if equiv else f"DIVERGED {detail}"
    print(f"{label:34s} cold {pc:7.1f}ms  warm {pw:7.1f}ms  {sp:5.1f}x   [{status}]")
    return {"name": name, "cold_ms": pc, "warm_ms": pw, "speedup": sp, "equiv": equiv}

def main():
    replay = json.load(open(REPLAY_LIST))
    # group observed commands
    observed = {}
    for r in replay:
        key = " ".join(r["argv"][1:]) or "(stdin)"
        observed.setdefault(key, []).append(r)

    print("== classification of the 13 observed python/node commands ==\n")
    real, synthetic, dead = [], [], []
    for key, rs in sorted(observed.items()):
        obs = max(x["observed_ms"] or 0 for x in rs)
        if key == "-m unittest discover -s tests -v":
            real.append((key, rs, obs))
        elif key == "-" or key.startswith("/tmp/"):
            dead.append((key, rs, obs))
        else:
            print(f"  skip (node/npm, not AThread v0.1 target): {key[:60]}")
    for key, rs, obs in dead:
        print(f"  NON-REPLAYABLE (content lost, retracting old claim): {key[:50]}"
              f"  observed {obs:.0f}ms in trace")
    print()

    rows = []
    print(f"== real replay through shim+athreadd ({REPS} reps each, fresh sandbox, equivalence-checked) ==")
    for key, rs, obs in real:
        # reconstructed workspace: agent-written tests are gone; bugrepo has
        # the same shape (calc package + tests/ with seeded failures)
        src = os.path.join(FIX, "bugrepo")
        b = bench_pair(key, lambda sb: ["python3", "-m", "unittest", "discover",
                                        "-s", "tests", "-v"], src,
                       "unittest discover (reconstructed ws)")
        rows.append(b)

    print("\n== synthetic equivalents of the lost /tmp scripts (LABELED SYNTHETIC) ==")
    for key, rs in sorted(observed.items()):
        obs = max(x["observed_ms"] or 0 for x in rs)
        base = os.path.basename(key.split()[0]) if key.startswith("/tmp/") else None
        if base and base[:-3] in SYNTHETIC:
            code = SYNTHETIC[base[:-3]]
            src_ws = tempfile.mkdtemp(prefix="replay_ws_")
            shutil.rmtree(src_ws)
            shutil.copytree(os.path.join(FIX, "repo"), src_ws)
            script_path = os.path.join(src_ws, base)
            with open(script_path, "w") as f:
                f.write(code)
            b = bench_pair(base, lambda sb, sp=script_path: ["python3", sp],
                           src_ws, f"{base} (synthetic equiv, obs {obs:.0f}ms)")
            shutil.rmtree(src_ws, ignore_errors=True)
            rows.append(b)

    tot_c = sum(r["cold_ms"] for r in rows if r["equiv"])
    tot_w = sum(r["warm_ms"] for r in rows if r["equiv"])
    print(f"\n== summary ==")
    print(f"equivalence-checked rows: {sum(1 for r in rows if r['equiv'])}/{len(rows)}")
    if tot_w:
        print(f"cold total p50: {tot_c:.0f}ms  warm total p50: {tot_w:.0f}ms  "
              f"-> {tot_c/tot_w:.1f}x on this reconstructed mix")
    print("\nNOTE: the old '21x' (-m mishandled) and '5.3x' (stdin) numbers are "
          "retracted; this table is the supported replacement.")

if __name__ == "__main__":
    main()
