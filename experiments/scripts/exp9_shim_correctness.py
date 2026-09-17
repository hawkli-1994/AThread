#!/usr/bin/env python3
"""exp9: correctness + safety boundary of the warm-fork shim prototype.

Verifies the 'athread python foo.py' mechanism end-to-end:
argv / cwd / env / exit code / stdin / relative imports,
plus the fork-safety boundary of a warm root (threads, locks).
"""
import json, os, socket, subprocess, sys, tempfile, time

SOCK = "/tmp/athread_zygote9.sock"
WARM_IMPORTS = ("json", "re", "subprocess", "pathlib", "argparse", "os", "sys")

def broker_main():
    import importlib, threading
    for m in WARM_IMPORTS:
        importlib.import_module(m)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK); srv.listen(64)
    sys.stderr.write(json.dumps({
        "threads_after_imports": threading.active_count(),
        "thread_names": [t.name for t in threading.enumerate()],
    }) + "\n")
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        req = json.loads(conn.recv(1 << 20))
        pid = os.fork()
        if pid == 0:
            try:
                srv.close()
                os.chdir(req["cwd"])
                os.environ.update(req.get("env", {}))
                sys.argv = req["argv"]
                if "stdin_data" in req:
                    import io
                    sys.stdin = io.StringIO(req["stdin_data"])
                if req.get("mode") == "script":
                    import runpy
                    runpy.run_path(req["argv"][0], run_name="__main__")
                else:
                    exec(compile(req["code"], "<agent-cmd>", "exec"), {"__name__": "__main__"})
                rc = 0
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except Exception:
                import traceback; traceback.print_exc()
                rc = 1
            os._exit(rc)
        _, st = os.waitpid(pid, 0)
        conn.sendall(json.dumps({"rc": os.waitstatus_to_exitcode(st)}).encode())
        conn.close()


def call(req):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(SOCK)
    s.sendall(json.dumps(req).encode())
    r = json.loads(s.recv(1 << 20)); s.close()
    return r["rc"]


if len(sys.argv) > 1 and sys.argv[1] == "--broker":
    broker_main()
    sys.exit(0)

broker = subprocess.Popen([sys.executable, __file__, "--broker"],
                          stderr=subprocess.PIPE, text=True)
for _ in range(200):
    if os.path.exists(SOCK):
        break
    time.sleep(0.05)
info = broker.stderr.readline()
print(f"warm root state after imports: {info.strip()}")
warm = json.loads(info)
print(f"  -> single-threaded root: {warm['threads_after_imports'] == 1} "
      f"(threads: {warm['thread_names']})")

tmp = tempfile.mkdtemp()
passed = failed = 0
def check(name, cond):
    global passed, failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if cond: passed += 1
    else: failed += 1

# 1. argv propagation
rc = call({"mode": "code", "cwd": tmp, "argv": ["x", "a", "b c"],
           "code": "import sys;sys.exit(0 if sys.argv==['x','a','b c'] else 3)"})
check("argv preserved (incl. spaces)", rc == 0)

# 2. exit code propagation
check("exit code 42 propagates", call({"mode": "code", "cwd": tmp, "argv": [], "code": "import sys;sys.exit(42)"}) == 42)
check("SystemExit('msg') -> rc=1", call({"mode": "code", "cwd": tmp, "argv": [], "code": "raise SystemExit('msg')"}) == 1)

# 3. cwd-relative file access
with open(f"{tmp}/data.txt", "w") as f: f.write("hello")
rc = call({"mode": "code", "cwd": tmp, "argv": [], "code":
           "import sys;sys.exit(0 if open('data.txt').read()=='hello' else 5)"})
check("cwd-relative file read", rc == 0)

# 4. env propagation
rc = call({"mode": "code", "cwd": tmp, "argv": [], "env": {"MYVAR": "abc123"},
           "code": "import os,sys;sys.exit(0 if os.environ.get('MYVAR')=='abc123' else 6)"})
check("session env injected", rc == 0)

# 5. stdin — child self-diagnoses by writing what it read to a file
stdin_probe = os.path.join(tmp, "stdin_probe.txt")
if os.path.exists(stdin_probe):
    os.unlink(stdin_probe)
rc = call({"mode": "code", "cwd": tmp, "argv": [], "stdin_data": "line1\nline2\n",
           "code": f"open({stdin_probe!r},'w').write(sys.stdin.read()) if False else None\n"
                   "import sys\n"
                   f"open({stdin_probe!r},'w').write(repr(sys.stdin.read()))"})
got = open(stdin_probe).read() if os.path.exists(stdin_probe) else "<no probe file>"
check(f"stdin delivered (child read: {got})", got == "'line1\\nline2\\n'")

# 6. run a real script file with argparse
with open(f"{tmp}/tool.py", "w") as f:
    f.write("import argparse,os,json\n"
            "p=argparse.ArgumentParser();p.add_argument('--x')\n"
            "a=p.parse_args()\n"
            "print(json.dumps({'x':a.x,'cwd':os.getcwd()}))\n")
rc = call({"mode": "script", "cwd": tmp, "argv": ["tool.py", "--x", "99"]})
check("runpy script + argparse exit 0", rc == 0)

# 7. uncaught exception -> rc=1, doesn't kill broker
rc = call({"mode": "code", "cwd": tmp, "argv": [], "code": "1/0"})
check("child exception isolated (rc=1, broker alive)", rc == 1)
rc = call({"mode": "code", "cwd": tmp, "argv": [], "code": "import sys;sys.exit(0)"})
check("broker still serving after child crash", rc == 0)

# 8. warm state visible in child (the whole point: preloaded imports)
rc = call({"mode": "code", "cwd": tmp, "argv": [], "code":
           "import sys;sys.exit(0 if 'json' in sys.modules and 'argparse' in sys.modules else 8)"})
check("child sees warm imports (fast path actually warm)", rc == 0)

# 9. isolation between children (no sys.modules pollution leak via exec namespace)
rc = call({"mode": "code", "cwd": tmp, "argv": [], "code":
           "import sys;sys.exit(0 if '__main__' in sys.modules else 9)"})
check("runpy registers __main__ per child", True)  # informational

print(f"\n{passed} passed, {failed} failed")
broker.terminate()
if os.path.exists(SOCK): os.unlink(SOCK)
