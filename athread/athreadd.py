#!/usr/bin/env python3
"""athreadd — AThread warm runtime daemon (v0.1).

Keeps a pre-initialized CPython interpreter (the "warm root") alive and
serves `python` executions by fork+CoW: the child inherits the warm
interpreter and runs the requested code, instead of cold-starting a new
interpreter from scratch.

Protocol (unix socket, length-prefixed JSON + SCM_RIGHTS):
  client -> {"cmd":"run","argv":[...],"env":[...],"cwd":"..."} + fds 0,1,2
  daemon -> {"ev":"spawn","pid":N}
  daemon -> {"ev":"exit","rc":N}        (when the child is reaped)
  client -> {"cmd":"status"}
  daemon -> {"ev":"status",...}

Usage: athreadd.py run [--warm-module M ...]   (foreground; athread CLI uses this)
"""
import argparse, array, json, os, selectors, signal, socket, sys, time

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("ATHREAD_STATE_DIR", os.path.join(HOME, ".athread"))
SOCK_PATH = os.path.join(STATE_DIR, "athreadd.sock")
PID_PATH = os.path.join(STATE_DIR, "athreadd.pid")
LOG_PATH = os.path.join(STATE_DIR, "athreadd.log")

DEFAULT_WARM = [
    "os", "sys", "io", "re", "json", "math", "time", "datetime", "glob",
    "fnmatch", "shutil", "pathlib", "argparse", "subprocess", "tempfile",
    "hashlib", "collections", "itertools", "functools", "typing", "ast",
    "unittest", "unittest.mock", "pickle", "socket", "struct", "string",
    "textwrap", "copy", "warnings", "traceback", "importlib", "runpy",
]

started_at = time.time()
served = 0


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    with open(LOG_PATH, "a") as f:
        f.write(line)


def load_warm_modules():
    mods = list(DEFAULT_WARM)
    cfg = os.path.join(STATE_DIR, "warm_modules.txt")
    if os.path.exists(cfg):
        mods = [l.strip() for l in open(cfg) if l.strip() and not l.startswith("#")]
    import importlib
    failed = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            failed.append((m, str(e)))
    return mods, failed


def child_run(req, fds):
    """Runs in the forked child. Never returns."""
    try:
        for i, fd in enumerate(fds):
            os.dup2(fd, i)
            if fd > 2:
                os.close(fd)
        os.chdir(req["cwd"])
        os.environ.clear()
        for kv in req["env"]:
            k, _, v = kv.partition("=")
            os.environ[k] = v
        argv = req["argv"]
        import runpy
        a = argv[1:]
        if not a:                                    # python < script-on-stdin
            sys.argv = ["-"]
            code = sys.stdin.read()
            sys.path.insert(0, "")
            exec(compile(code, "<stdin>", "exec"), {"__name__": "__main__"})
        elif a[0] == "-c":
            sys.argv = ["-c"] + a[2:]                # match CPython semantics
            sys.path.insert(0, os.getcwd())
            g = {"__name__": "__main__"}
            exec(compile(a[1], "<string>", "exec"), g)
        elif a[0] == "-m":
            sys.argv = [a[1]] + a[2:]                # run_module fixes argv[0]
            sys.path.insert(0, os.getcwd())          # real python -m adds cwd
            runpy.run_module(a[1], run_name="__main__", alter_sys=True)
        else:                                        # script path
            sys.argv = [a[0]] + a[1:]
            runpy.run_path(os.path.abspath(a[0]), run_name="__main__")
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except KeyboardInterrupt:
        rc = 130
    except BaseException:
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        rc = 1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(rc)


def serve(warm_mods):
    global served
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(128)
    srv.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(srv, selectors.EVENT_READ, "listen")
    # wake the selector immediately when a child exits (SIGCHLD -> pipe)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    sel.register(wake_r, selectors.EVENT_READ, "wake")
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGCHLD, lambda *_: None)

    bufs = {}      # conn -> bytearray of received bytes
    fds_got = {}   # conn -> [in, out, err]
    pending = {}   # child pid -> conn
    log(f"ready: {len(warm_mods)} warm modules, socket {SOCK_PATH}")

    def reap():
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            conn = pending.pop(pid, None)
            if conn is None:
                continue
            rc = os.waitstatus_to_exitcode(status)
            try:
                conn.sendall(json.dumps({"ev": "exit", "rc": rc}).encode() + b"\n")
            except OSError:
                pass
            try:
                sel.unregister(conn)
            except Exception:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def read_request(conn):
        try:
            msg, anc, _flags, _addr = conn.recvmsg(1 << 16, socket.CMSG_LEN(16))
        except (BlockingIOError, InterruptedError):
            return None
        if not msg:
            raise ConnectionResetError
        if conn not in fds_got and anc:
            for level, ctype, cdata in anc:
                if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                    arr = array.array("i")
                    arr.frombytes(cdata[: len(cdata) - (len(cdata) % arr.itemsize)])
                    fds_got[conn] = list(arr)
        return msg

    while True:
        reap()
        for key, _mask in sel.select(timeout=1.0):
            conn = key.fileobj
            if key.data == "wake":
                try:
                    while os.read(wake_r, 4096):
                        pass
                except BlockingIOError:
                    pass
                reap()
                continue
            if key.data == "listen":
                c, _ = srv.accept()
                c.setblocking(False)
                sel.register(c, selectors.EVENT_READ, "conn")
                bufs[c] = bytearray()
                continue
            try:
                chunk = read_request(conn)
            except (ConnectionResetError, BrokenPipeError, OSError):
                for op in (lambda: sel.unregister(conn), conn.close):
                    try:
                        op()
                    except Exception:
                        pass
                bufs.pop(conn, None); fds_got.pop(conn, None)
                continue
            if chunk is None:
                continue
            bufs[conn] += chunk
            raw = bytes(bufs[conn])
            nl = raw.find(b"\n")
            if nl < 0:
                continue
            line, rest = raw[:nl], raw[nl + 1:]
            bufs[conn] = bytearray(rest)
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                for op in (lambda: sel.unregister(conn), conn.close):
                    try:
                        op()
                    except Exception:
                        pass
                continue

            if req.get("cmd") == "status":
                conn.sendall((json.dumps({
                    "ev": "status", "pid": os.getpid(),
                    "uptime_s": int(time.time() - started_at),
                    "served": served,
                    "warm_modules": len(warm_mods),
                    "python": sys.version.split()[0],
                    "hash_seed": os.environ.get("PYTHONHASHSEED"),
                }) + "\n").encode())
                for op in (lambda: sel.unregister(conn), conn.close):
                    try:
                        op()
                    except Exception:
                        pass
                continue

            if req.get("cmd") != "run":
                for op in (lambda: sel.unregister(conn), conn.close):
                    try:
                        op()
                    except Exception:
                        pass
                continue

            fds = fds_got.pop(conn, [])
            if len(fds) != 3:
                try:
                    conn.sendall(b'{"ev":"exit","rc":125,"error":"no fds"}\n')
                except OSError:
                    pass
                for op in (lambda: sel.unregister(conn), conn.close):
                    try:
                        op()
                    except Exception:
                        pass
                continue

            pid = os.fork()
            if pid == 0:
                srv.close()
                child_run(req, fds)
                os._exit(125)
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            served += 1
            pending[pid] = conn
            try:
                conn.sendall(json.dumps({"ev": "spawn", "pid": pid}).encode() + b"\n")
            except OSError:
                pending.pop(pid, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["run"])
    ap.add_argument("--warm-module", action="append", default=[])
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    log(f"starting, pid {os.getpid()}")
    mods, failed = load_warm_modules()
    if args.warm_module:
        import importlib
        for m in args.warm_module:
            try:
                importlib.import_module(m)
                mods.append(m)
            except Exception as e:
                failed.append((m, str(e)))
    if failed:
        log(f"warm modules failed: {failed}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    try:
        serve(mods)
    finally:
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
