#!/usr/bin/env python3
"""parity_test.py: differential semantics tests — cold real python vs the
AThread warm fast path (shim + athreadd).

For every case we assert ALL of: exit code, stdout, stderr, and filesystem
side effects are identical between cold and warm. A case that cannot be made
equivalent must fall back (or be documented as unsupported), never silently
diverge.

Run:  python3 parity_test.py
Requires a running daemon (`athread start`).
"""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "experiments", "fixture", "bugrepo"))
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, ".athread", "bin")
SOCK = os.path.join(HOME, ".athread", "athreadd.sock")

CLEAN_PATH = ":".join(p for p in os.environ.get("PATH", "").split(":")
                      if not p.endswith(".athread/bin")) or "/usr/local/bin:/usr/bin:/bin"
WARM_ENV = dict(os.environ, PATH=f"{BIN}:{CLEAN_PATH}",
                ATHREAD_SOCK=SOCK, ATHREAD_REAL_PYTHON="/usr/bin/python3")
COLD_ENV = dict(os.environ, PATH=CLEAN_PATH)
for e in (WARM_ENV, COLD_ENV):
    e.pop("VIRTUAL_ENV", None); e.pop("PYTHONPATH", None); e.pop("CONDA_PREFIX", None)

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")

def run(mode, args, cwd, stdin=None, timeout=60):
    env = WARM_ENV if mode == "warm" else COLD_ENV
    return subprocess.run(["python3"] + args, cwd=cwd, env=env,
                          input=stdin, capture_output=True, text=True, timeout=timeout)

def diff_case(name, args, cwd=REPO, stdin=None, side_effect=None, timeout=60):
    """Run cold vs warm; compare rc/stdout/stderr (and side_effect dirs)."""
    c = run("cold", args, cwd, stdin, timeout)
    w = run("warm", args, cwd, stdin, timeout)
    ok = (c.returncode == w.returncode and c.stdout == w.stdout and c.stderr == w.stderr)
    detail = ""
    if not ok:
        detail = (f"rc {c.returncode}/{w.returncode} "
                  f"stdout {c.stdout[:80]!r}/{w.stdout[:80]!r} "
                  f"stderr {c.stderr[:80]!r}/{w.stderr[:80]!r}")
    if side_effect is not None:
        d1, d2 = side_effect
        snap1 = snapshot(d1); snap2 = snapshot(d2)
        if snap1 != snap2:
            ok = False
            detail += f" side-effects differ: {set(snap1.items()) ^ set(snap2.items())}"
    check(name, ok, detail)
    return c, w

def snapshot(root):
    out = {}
    for dp, _, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root)
            try:
                with open(p, "rb") as f:
                    out[rel] = f.read()
            except OSError:
                pass
    return out

def main():
    r = subprocess.run([os.path.join(HERE, "athread"), "status"],
                       capture_output=True, text=True)
    if "not running" in r.stdout:
        print("daemon not running — `athread start` first"); sys.exit(1)

    td = tempfile.mkdtemp(prefix="athread_parity_")
    os.chdir(td)
    try:
        print("-- basic exit codes --")
        diff_case("exit-0", ["-c", "pass"])
        diff_case("exit-3 (sys.exit int)", ["-c", "import sys; sys.exit(3)"])
        diff_case("SystemExit(None)", ["-c", "import sys; sys.exit()"])
        diff_case("SystemExit str -> stderr + rc 1",
                  ["-c", "import sys; sys.exit('错误信息')"])
        diff_case("unhandled exception traceback + rc 1",
                  ["-c", "raise ValueError('boom')"])

        print("-- long-running (must not hit shim timeout) --")
        diff_case("3.3s sleep keeps rc 0", ["-c", "import time; time.sleep(3.3)"])
        diff_case("6s script", ["-c", "import time; time.sleep(6); print('done')"])

        print("-- argv fidelity --")
        args300 = ["-c", "import sys; print(len(sys.argv))"] + [f"a{i}" for i in range(300)]
        diff_case("300 user args preserved", args300)
        diff_case("argv[0]/-c semantics", ["-c", "import sys; print(sys.argv[0])", "x", "y"])

        print("-- __main__ semantics --")
        diff_case("-c sets full __main__ module",
                  ["-c", "x=41; import __main__; print(__main__.x, __name__, __package__)"])
        diff_case("-c __doc__/__package__",
                  ["-c", "print(__doc__, __package__, __spec__)"])

        print("-- atexit / shutdown --")
        diff_case("atexit handler runs",
                  ["-c", "import atexit; atexit.register(print, 'cleanup-ran'); print('body')"])
        diff_case("atexit order + flush on exit",
                  ["-c", "import atexit,sys; "
                         "atexit.register(lambda: sys.stderr.write('e1\\n')); print('b')"])

        print("-- import paths --")
        os.makedirs("pkg1/sub")
        with open("pkg1/helper.py", "w") as f:
            f.write("VALUE = 7\n")
        with open("pkg1/sub/main.py", "w") as f:
            f.write("import helper\nprint(helper.VALUE)\n")
        diff_case("script imports same-dir module",
                  ["pkg1/sub/main.py"], cwd=td)
        diff_case("-c imports from cwd",
                  ["-c", "import sys; sys.path.insert(0,'pkg1'); import helper; print(helper.VALUE)"],
                  cwd=td)
        with open("modx.py", "w") as f:
            f.write("print('modx-ok')\n")
        diff_case("-m module from cwd", ["-m", "modx"], cwd=td)
        diff_case("-m package.module", ["-m", "pkg1.helper"], cwd=td)

        print("-- stdin script --")
        diff_case("stdin script", [], stdin="print('stdin-ok', __name__)\n")

        print("-- side effects --")
        for mode_dir in ("cold_fx", "warm_fx"):
            shutil.rmtree(mode_dir, ignore_errors=True)
            os.makedirs(mode_dir)
        c = run("cold", ["-c", "open('out.txt','w').write('hi')"], cwd=os.path.join(td, "cold_fx"))
        w = run("warm", ["-c", "open('out.txt','w').write('hi')"], cwd=os.path.join(td, "warm_fx"))
        check("file side effect written", c.returncode == 0 and w.returncode == 0)
        check("file side effect content equal",
              open("cold_fx/out.txt").read() == open("warm_fx/out.txt").read() == "hi")

        print("-- fallback stays correct --")
        e = dict(WARM_ENV, PYTHONPATH="/tmp")
        r1 = subprocess.run(["python3", "-c", "print('fb-ok')"], env=e,
                            capture_output=True, text=True)
        check("PYTHONPATH forces fallback with right output",
              r1.stdout.strip() == "fb-ok" and r1.returncode == 0, repr(r1.stdout))
        r2 = subprocess.run(["python3", "-I", "-c", "print(1)"], env=WARM_ENV,
                            capture_output=True, text=True)
        check("unknown -I flag falls back rc 0", r2.returncode == 0, str(r2.returncode))

        print("-- real fixture: unittest discover parity --")
        cmd = ["-m", "unittest", "discover", "-s", "tests", "-v"]
        c, w = diff_case("unittest discover identical output", cmd, cwd=REPO)
        check("unittest discover ran real tests (5, 2 expected failures)",
              "Ran 5 tests" in c.stderr and "FAILED (failures=2)" in c.stderr,
              c.stderr[-120:])

        print(f"\n{passed} passed, {failed} failed")
        sys.exit(1 if failed else 0)
    finally:
        os.chdir("/")
        shutil.rmtree(td, ignore_errors=True)

if __name__ == "__main__":
    main()
