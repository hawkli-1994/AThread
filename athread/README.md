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

python3 -m unittest discover -s tests   # 4.8x faster (33.7ms -> 7.0ms)
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

- Children inherit the daemon's preloaded `sys.modules` (~35 stdlib modules,
  configurable via `~/.athread/warm_modules.txt`). Code that inspects
  `sys.modules` at runtime may observe them.
- Daemon runs with `PYTHONHASHSEED=0`: children get deterministic hash
  ordering (fresh CPython is randomized). Scripts relying on hash order are
  broken anyway; noted for completeness.
- Warm root must stay single-threaded (fork safety). Do not warm modules
  that spawn threads or async event loops.
- Signals: shim forwards SIGINT/SIGTERM/SIGHUP/SIGQUIT to the child.

## Measured (exp12, this machine, transparent PATH shim)

| scenario | baseline | athread | speedup |
|---|---|---|---|
| `python -m unittest discover` loop | 33.7ms | 7.0ms | 4.8x |
| heavy-import `python -c` | 21.5ms | 6.2ms | 3.4x |
| 30-session sparse sim, python p50 | 22.5ms | 6.8ms | 3.3x |
| same, git/rg/cat | — | identical | no regression |
| 20 concurrent python procs memory | 78MB | 50MB (incl. daemon) | -36% |

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
