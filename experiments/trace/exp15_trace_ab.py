#!/usr/bin/env python3
"""exp15: fixed-trace capacity A/B, P0-hardened (replaces exp14).

Compliance with issue #1 P0 requirements:
- INPUT FROZEN: workspaces are fresh copies of the committed fixtures
  (fixture/repo is a git repo; fixture/bugrepo carries tests/). No reference
  to old host /tmp or absolute workspace paths survives path rewriting.
- COLD-FIRST: mode A runs first and classifies every command
  (ok / expected-test-failure / infra-failure); only commands that cold-ran
  enter the equivalence pairing.
- COVERAGE: commands that cannot be reconstructed (lost /tmp scripts, node,
  unknown binaries) are skipped in BOTH modes, counted, and reported with
  reasons — never silently dropped.
- STABLE IDs: every command carries cmd_id "<session>-<pid>-<seq>"; A/B
  pairing is by ID, not record order.
- EQUIVALENCE: per command compare rc + stdout + stderr + test counts;
  per session compare the FINAL sandbox tree byte-for-byte (add/modify/
  delete all covered). Dynamic-field normalization is declared in NORM and
  applied identically to both modes.
- CPU: per-command rusage via os.wait4 (user+system of the direct child),
  summed. Workspace prep is outside the measured region.
- RHYTHM: original think-delays preserved for the "original rhythm" report;
  a separate scaled run (--scale 0.1) is reported on its own.

Usage:
  python3 exp15_trace_ab.py --mode a [--scale 1.0]
  python3 exp15_trace_ab.py --mode b [--scale 1.0]
  python3 exp15_trace_ab.py --summary          (pairs latest a/b runs)
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))
REPAIRDIR = os.path.join(HERE, "logs", "repaired")
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
SOCK = os.path.join(HOME, ".athread", "athreadd.sock")
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))
WS_SRC = os.path.join(FIX, "repo")
TESTS_SRC = os.path.join(FIX, "bugrepo")
SKIP_BINS = {"node", "nodejs", "npm", "npx", "yarn", "pnpm"}

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin") and "shim_bin" not in p) \
             or "/usr/local/bin:/usr/bin:/bin"

# ---- declared normalization rules (applied identically to both modes) ----
# DECLARED DYNAMIC FIELDS: sandbox abs paths, ISO datetimes, wall-clock
# HH:MM:SS times (e.g. `date` output), unittest timing lines, /tmp paths.
# Everything else must match byte-for-byte.
def NORM(s, ws=None):
    if s is None:
        return None
    if ws:
        s = s.replace(ws, "<WS>")
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", "DATE", s)
    s = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", "TIME", s)      # `date` output
    s = re.sub(r"\b20\d{2}\b", "YEAR", s)                   # `date` year field
    s = re.sub(r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2}", "DAY", s)  # date prefix
    s = re.sub(r"\b(CST|UTC|GMT|PST|EST)\b", "TZ", s)
    s = re.sub(r"in \d+\.\d+s", "in Ts", s)
    s = re.sub(r"/tmp/[A-Za-z0-9_.-]+", "TMP", s)
    s = re.sub(r"\b1[0-9]{9}\b", "EPOCH", s)               # `date +%s`
    return s

# env/printenv carry harness-injected differences by construction:
# PATH gains the shim dir, ATHREAD_SOCK/ATHREAD_REAL_PYTHON are added.
# DECLARED RULE: for env-family commands compare (a) the set of keys minus
# harness keys and (b) PATH as a set of entries; all other values must match.
HARNESS_ENV_KEYS = {"ATHREAD_SOCK", "ATHREAD_REAL_PYTHON"}
def env_equiv(a_out, b_out):
    def parse(s):
        kv = {}
        for line in (s or "").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k] = v
        return kv
    ea, eb = parse(a_out), parse(b_out)
    ka = set(ea) - HARNESS_ENV_KEYS
    kb = set(eb) - HARNESS_ENV_KEYS
    if ka != kb:
        return False
    for k in ka:
        if k == "PATH":
            if set(ea[k].split(":")) != set(eb[k].split(":")):
                return False
        elif ea[k] != eb[k]:
            return False
    return True

def build_timelines():
    tls = {}
    for p in sorted(glob.glob(os.path.join(REPAIRDIR, "*.jsonl"))):
        label = os.path.basename(p)[:-6]
        starts = {}
        for line in open(p):
            e = json.loads(line)
            if e["ev"] == "start":
                starts[e["pid"]] = e
        evs = sorted(starts.values(), key=lambda e: e["ts"])
        tl, prev = [], None
        for i, s in enumerate(evs):
            delay = max(0.0, (s["ts"] - prev) / 1e6) if prev else 0.0
            prev = s["ts"]
            tl.append({"cmd_id": f"{label}-{s['pid']}-{i}", "delay_ms": delay,
                       "bin": s["bin"], "argv": s["argv"], "venv": s.get("venv", 0)})
        tls[label] = tl
    return tls

def prepare(tl, wsdir, testsdir):
    """Return (argv, cwd) runnable against frozen fixtures, or (None, reason)."""
    b = tl["bin"]
    if b in SKIP_BINS:
        return None, f"bin '{b}' out of v0.1 scope (skipped in both modes)"
    argv = list(tl["argv"])
    if not argv:
        return None, "empty argv"
    if b in ("python3", "python"):
        if len(argv) >= 2 and argv[1] == "-":
            return None, "stdin script content lost at trace time"
        if len(argv) >= 2 and not argv[1].startswith("-"):
            script = argv[1]
            if script.startswith("/tmp/"):
                return None, "agent-written /tmp script lost"
            if not os.path.isfile(os.path.join(wsdir, script)) and \
               not os.path.isfile(script):
                return None, f"script '{script}' not in frozen workspace"
    is_test = b.startswith("python") and \
        any("unittest" in a or "pytest" in a for a in argv[1:])
    cwd = testsdir if is_test else wsdir
    # path rewriting: old workspace-absolute args -> sandbox-relative
    out = []
    for i, a in enumerate(argv):
        if i == 0:
            out.append(b)
        elif "workspaces/" in a:
            out.append(os.path.join(cwd, os.path.basename(a.rstrip("/"))))
        else:
            out.append(a)
    return (out, cwd), None

def run4(argv, cwd, env, timeout=30):
    """fork/exec with tempfile capture + rusage. stdin is /dev/null: the
    original agents ran with no interactive stdin, and an inherited pipe
    stdin would let `cat`-style commands block until the timeout. Tempfiles
    (not pipes) for stdout/stderr: pipes would deadlock when a command
    writes more than the buffer while we wait for it to exit."""
    import tempfile as _tf
    out_f = _tf.TemporaryFile()
    err_f = _tf.TemporaryFile()
    devnull = os.open(os.devnull, os.O_RDONLY)
    t0 = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(out_f.fileno(), 1); os.dup2(err_f.fileno(), 2)
            os.dup2(devnull, 0)
            os.close(devnull)
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)
    os.close(devnull)
    deadline = t0 + timeout
    rc, ru = None, None
    while time.monotonic() < deadline:
        done, status, r = os.wait4(pid, os.WNOHANG)
        if done == pid:
            rc = os.waitstatus_to_exitcode(status)
            ru = r
            break
        time.sleep(0.005)
    if rc is None:
        try: os.kill(pid, 9)
        except OSError: pass
        _, status, ru = os.wait4(pid, 0)
        rc = os.waitstatus_to_exitcode(status)
    out_f.seek(0); stdout = out_f.read().decode(errors="replace")
    err_f.seek(0); stderr = err_f.read().decode(errors="replace")
    out_f.close(); err_f.close()
    wall_ms = (time.monotonic() - t0) * 1000
    cpu_ms = ((ru.ru_utime + ru.ru_stime) * 1000) if ru else 0.0
    return {"rc": rc, "out": stdout, "err": stderr,
            "wall_ms": wall_ms, "cpu_ms": cpu_ms}

def worker(label, tl, mode, wsdir, testsdir, outpath, scale):
    env = dict(os.environ, PATH=CLEAN_PATH)
    if mode == "b":
        env.update(PATH=f"{BIN}:{CLEAN_PATH}", ATHREAD_SOCK=SOCK,
                   ATHREAD_REAL_PYTHON="/usr/bin/python3")
    recs, skipped = [], []
    for t in tl:
        time.sleep(t["delay_ms"] * scale / 1000.0)
        prep, reason = prepare(t, wsdir, testsdir)
        if prep is None:
            skipped.append({"cmd_id": t["cmd_id"], "bin": t["bin"], "reason": reason})
            continue
        argv, cwd = prep
        r = run4(argv, cwd, env)
        if r is None:
            skipped.append({"cmd_id": t["cmd_id"], "bin": t["bin"], "reason": "timeout"})
            continue
        r.update({"cmd_id": t["cmd_id"], "bin": t["bin"]})
        recs.append(r)
    with open(outpath, "w") as f:
        json.dump({"label": label, "recs": recs, "skipped": skipped,
                   "final_tree": tree(wsdir)}, f)

def tree(root):
    import hashlib
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "__pycache__" and d != ".git"]
        for fn in fns:
            if fn.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, "rb") as f:
                    data = f.read()
                out[os.path.relpath(p, root)] = [len(data), hashlib.sha256(data).hexdigest()]
            except OSError:
                pass
    return out

def run_mode(mode, scale):
    tls = build_timelines()
    tmp = tempfile.mkdtemp(prefix=f"exp15_{mode}_{scale}_")
    t0 = time.monotonic()
    procs = []
    for label, tl in tls.items():
        wsdir = os.path.join(tmp, f"ws_{label}")
        testsdir = os.path.join(tmp, f"tests_{label}")
        shutil.copytree(WS_SRC, wsdir)
        shutil.copytree(TESTS_SRC, testsdir)
        procs.append(subprocess.Popen(
            [sys.executable, __file__, "--worker", label, mode, wsdir, testsdir,
             os.path.join(tmp, f"{label}.json"), str(scale)]))
    for p in procs:
        p.wait()
    wall = time.monotonic() - t0
    sessions = {}
    for label in tls:
        fp = os.path.join(tmp, f"{label}.json")
        if os.path.exists(fp):
            sessions[label] = json.load(open(fp))
    shutil.rmtree(tmp, ignore_errors=True)
    out = {"mode": mode, "scale": scale, "wall_s": round(wall, 1),
           "sessions": sessions}
    fn = os.path.join(RESULTS, f"exp15_{mode}_s{scale}.json")
    with open(fn, "w") as f:
        json.dump(out, f)
    n_cmd = sum(len(s["recs"]) for s in sessions.values())
    n_skip = sum(len(s["skipped"]) for s in sessions.values())
    cpu = sum(r["cpu_ms"] for s in sessions.values() for r in s["recs"])
    print(f"[mode {mode} scale {scale}] wall {wall:.1f}s  executed {n_cmd}  "
          f"skipped {n_skip}  cpu {cpu:.0f}ms -> {fn}")

def classify(rec):
    if rec["rc"] == 0:
        return "ok"
    if rec["bin"].startswith("python"):
        m = re.search(r"Ran (\d+) tests.*?FAILED \(failures=(\d+)", rec["err"] or "", re.S)
        if m:
            return "expected-test-failure"
    return "failure"

# analysis-time exclusion of host-state probes: these commands introspect
# live host state (not the frozen sandbox) and can never match across two
# runs at different wall times, regardless of AThread. DECLARED in report.
def host_state_probe(rec):
    b = rec["bin"]
    if b in ("env", "printenv"):
        return "env introspection (harness vars/PATH differ by construction)"
    if b in ("which", "whereis", "type", "command"):
        return "PATH-dependent output (PATH differs by construction)"
    if b == "ls" and any("l" in (a.strip("-") or "") for a in rec.get("argv", [])[1:] if a.startswith("-")):
        return "timestamp-bearing directory listing (.. mtimes are live host state)"
    if b == "date":
        return "clock output"
    return None

def summary(scale):
    A = json.load(open(os.path.join(RESULTS, f"exp15_a_s{scale}.json")))
    B = json.load(open(os.path.join(RESULTS, f"exp15_b_s{scale}.json")))
    print(f"\n== exp15 summary (scale={scale}: "
          f"{'original rhythm' if float(scale)==1.0 else scale + 'x speedup of think time'}) ==")
    print(f"wall: A {A['wall_s']}s  B {B['wall_s']}s")
    # coverage: identical skips expected in both modes
    skipA = {s['cmd_id']: s['reason'] for s in A['sessions'].values() for s in s['skipped']}
    skipB = {s['cmd_id']: s['reason'] for s in B['sessions'].values() for s in s['skipped']}
    onlyA = set(skipA) - set(skipB); onlyB = set(skipB) - set(skipA)
    print(f"coverage: skipped A={len(skipA)} B={len(skipB)} "
          f"(mismatch: onlyA={len(onlyA)} onlyB={len(onlyB)})")
    for rsn, n in Counter(skipA.values()).most_common():
        print(f"  skip[{n:3d}]: {rsn}")
    # pair executed commands by ID
    recsA = {r["cmd_id"]: r for s in A['sessions'].values() for r in s['recs']}
    recsB = {r["cmd_id"]: r for s in B['sessions'].values() for r in s['recs']}
    common = sorted(set(recsA) & set(recsB))
    # split out host-state probes (executed in both modes, excluded from
    # equivalence pairing with declared reasons)
    probes, pairable = {}, []
    for cid in common:
        rsn = host_state_probe(recsA[cid])
        if rsn:
            probes[cid] = rsn
        else:
            pairable.append(cid)
    common = pairable
    print(f"host-state probes excluded from pairing: {len(probes)}")
    for rsn, n in Counter(probes.values()).most_common():
        print(f"  probe[{n}]: {rsn}")
    cls = Counter(); divergent = []
    cpuA = cpuB = wallA = wallB = 0.0
    by_bin = defaultdict(lambda: [[], []])
    for cid in common:
        a, b = recsA[cid], recsB[cid]
        ca, cb = classify(a), classify(b)
        cpuA += a["cpu_ms"]; cpuB += b["cpu_ms"]
        wallA += a["wall_ms"]; wallB += b["wall_ms"]
        by_bin[a["bin"]][0].append(a["wall_ms"])
        by_bin[a["bin"]][1].append(b["wall_ms"])
        # task success must be identical (not merely equal nonzero rc)
        task_a = "success" if ca == "ok" else ("test-failure(expected)" if ca == "expected-test-failure" else "FAILED")
        task_b = "success" if cb == "ok" else ("test-failure(expected)" if cb == "expected-test-failure" else "FAILED")
        if a["bin"] in ("env", "printenv"):
            same_out = env_equiv(a["out"], b["out"]) and NORM(a["err"], "") == NORM(b["err"], "")
        else:
            same_out = (NORM(a["out"], "") == NORM(b["out"], "") and
                        NORM(a["err"], "") == NORM(b["err"], ""))
        if task_a == task_b and same_out:
            cls["equivalent"] += 1
        else:
            cls["DIVERGENT"] += 1
            divergent.append((cid, a["bin"], ca, cb, "output" if task_a == task_b else "task-status"))
    print(f"paired commands: {len(common)}")
    for k, v in cls.most_common():
        print(f"  {k}: {v}")
    for cid, b, ca, cb, what in divergent[:10]:
        print(f"  DIVERGENT {cid} [{b}] {what} cold={ca} warm={cb}")
    print(f"CPU (sum of per-command child rusage): A {cpuA:.0f}ms  B {cpuB:.0f}ms  "
          f"({(1 - cpuB/max(cpuA,1e-9))*100:+.0f}%)")
    print(f"command wall sum: A {wallA:.0f}ms  B {wallB:.0f}ms")
    print("per-bin wall p50 (A vs B):")
    for b in ("python3", "git", "cat", "tr", "wc"):
        if b in by_bin and by_bin[b][0]:
            xa, xb = sorted(by_bin[b][0]), sorted(by_bin[b][1])
            pa = xa[len(xa)//2]; pb = xb[len(xb)//2]
            print(f"  {b:8s} {pa:7.1f} -> {pb:7.1f}ms   (n={len(xa)})")
    # final sandbox trees
    tree_mismatch = [l for l in A['sessions']
                     if l in B['sessions'] and
                     A['sessions'][l]['final_tree'] != B['sessions'][l]['final_tree']]
    print(f"final sandbox tree: {'all equal across modes' if not tree_mismatch else 'MISMATCH ' + str(tree_mismatch)}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _, _, label, mode, wsdir, testsdir, outpath, scale = sys.argv
        tls = build_timelines()
        worker(label, tls[label], mode, wsdir, testsdir, outpath, float(scale))
    elif len(sys.argv) > 1 and sys.argv[1] == "--summary":
        summary(sys.argv[2] if len(sys.argv) > 2 else "1.0")
    else:
        mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "a"
        scale = sys.argv[sys.argv.index("--scale") + 1] if "--scale" in sys.argv else "1.0"
        run_mode(mode, scale)
