# AThread v0.1 — Warm Runtime Prototype

`athreadd` keeps a pre-initialized CPython interpreter (the "warm root")
alive and serves `python3` invocations via fork+CoW. A transparent PATH
shim decides per invocation: eligible calls go to the daemon (warm fast
path), everything else falls back to the real interpreter unchanged.

## Quick start

```bash
./athread install     # build shim, install ~/.athread/bin/{python3,python}
./athread start       # start athreadd (warm root + fork broker)
./athread status      # uptime / served count / warm modules

# enable in a shell (agents need no modification — PATH does everything)
export PATH="$HOME/.athread/bin:$PATH"
export ATHREAD_SOCK="$HOME/.athread/athreadd.sock"
export ATHREAD_REAL_PYTHON="/usr/bin/python3"

python3 -m unittest discover -s tests   # 4.8x faster (34.2ms -> 7.1ms, output-identical)
```

## What takes the fast path

`python3` with: `-c CODE` | `-m MOD` | `SCRIPT` | stdin script,
and none of `VIRTUAL_ENV` / `PYTHONPATH` / `CONDA_PREFIX` set.

## What falls back (unchanged behavior)

- `VIRTUAL_ENV` / `PYTHONPATH` / `CONDA_PREFIX` set (per-session env isolation)
- unknown flags (`-V`, `-X ...`, `-O`, isolated mode, etc.)
- script file missing
- daemon unreachable (2s connect timeout, fail-open)

## Semantics notes / known divergences

The fast path is held to a differential standard: `parity_test.py` runs 24
cases (exit codes, long-running commands, atexit, SystemExit messages, argv
fidelity, `__main__` semantics, import paths, stdin scripts, file side
effects, tracebacks) asserting rc/stdout/stderr byte-identical to cold
CPython. All pass. Remaining deliberate divergences:

- Children inherit the daemon's preloaded `sys.modules` (~35 stdlib modules,
  configurable via `~/.athread/warm_modules.txt`). Code that inspects
  `sys.modules` at runtime may observe them.
- Daemon runs with `PYTHONHASHSEED=0`: children get deterministic hash
  ordering (fresh CPython is randomized). Scripts relying on hash order are
  broken anyway; noted for completeness.
- Warm root must stay single-threaded (fork safety). Do not warm modules
  that spawn threads or async event loops.
- `os._exit` still skips full `Py_Finalize` (unsafe in forked children);
  atexit handlers registered by user code DO run, stdio IS flushed.
- Signals: shim forwards SIGINT/SIGTERM/SIGHUP/SIGQUIT to the child.
- `python -` (explicit stdin dash) and >4096 argv fall back to the real
  interpreter rather than risk divergence.

## Measured (exp12, this machine, transparent PATH shim)

| scenario | baseline | athread | speedup |
|---|---|---|---|
| `python -m unittest discover` loop | 34.2ms | 7.1ms | 4.8x (rc+output identical) |
| heavy-import `python -c` | 21.5ms | 6.2ms | 3.4x |
| 30-session sparse sim, python p50 | 22.5ms | 6.8ms | 3.3x |
| same, git/rg/cat | — | identical | no regression |
| 20 concurrent import-heavy procs, full process group | 129.5MB | 111.6MB (incl. daemon) | -14% (exp13) |

## Architecture

```text
agent shell:  python3 foo.py
      │
  ~/.athread/bin/python3        (C shim: eligibility check, fail-open)
      │ unix socket + SCM_RIGHTS (argv/env/cwd + fds 0,1,2)
      ▼
  athreadd                      (single-threaded warm CPython root)
      │ os.fork()  →  child: dup2 fds, chdir, replace env, runpy/exec
      ▼
  reply exit status to shim
```

## Files

- `athreadd.py` — daemon: warm root, fork broker, event loop (SIGCHLD wake pipe)
- `shim.c` — the `python3` replacement (fallback exec on any doubt)
- `athread` — CLI: start / stop / status / install / doctor
