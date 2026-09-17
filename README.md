# AThread

[中文](README.zh.md) | English

**A Linux runtime for massive concurrent AI agent sessions.**

Run hundreds of AI agent sessions on ordinary Linux without changing the agents.

AThread is an open-source runtime designed to make Linux handle large numbers of concurrent AI agent sessions efficiently.

It keeps the standard Linux experience intact:

* Bash still works.
* POSIX still works.
* ELF binaries still work.
* Git, Python, Node.js, Docker, CUDA and existing CLI tools still work.
* Claude Code, Codex, OpenCode, OpenClaw and future agents should run without modification.

AThread does not replace Linux.

It makes Linux better at running agents.

---

## Why AThread?

Modern coding agents and general-purpose AI agents behave very differently from human users.

A human developer may keep a few terminals open and execute hundreds of commands per day.

An agent can continuously create processes, scan repositories, run Git commands, open files, execute shell utilities, start language runtimes and maintain long-lived sessions.

Run enough agents at the same time and the operating system begins to suffer from:

* excessive process creation;
* large numbers of shell and PTY sessions;
* repeated filesystem metadata access;
* duplicated runtime memory;
* repeated repository scans;
* high context-switch rates;
* duplicated file watchers;
* excessive short-lived subprocesses;
* memory pressure;
* I/O contention.

A typical agent session today may look like this:

```text
Agent
├── bash
├── PTY
├── Node.js / Python runtime
├── git
├── ripgrep
├── language server
├── MCP servers
├── filesystem watchers
└── many short-lived subprocesses
```

Ten agents may already mean hundreds of processes and threads.

AThread starts from a simple question:

> Why should an AI agent session be as expensive as a traditional Unix process tree?

---

## The idea

Operating systems historically evolved around processes and threads.

Programming runtimes later introduced lighter execution abstractions.

For example:

```text
OS Thread
   ↓
Goroutine
```

AThread explores a similar idea for AI agents:

```text
Process Tree
   ↓
Agent Session
```

The goal is not to turn agents into a proprietary runtime.

The goal is to keep the existing Linux interface while dramatically reducing the underlying cost of running many agent sessions.

In other words:

> Keep the interface. Optimize the implementation.

---

## Design principles

### 1. Full Linux compatibility

AThread should not require agents to use a special API.

An agent should still be able to execute:

```bash
bash
python
node
git
ssh
sed
awk
rg
gcc
docker
vim
```

Existing Linux tools must continue to behave normally.

AThread is not an agent-specific operating environment.

It is an optimization layer underneath normal Linux workloads.

---

### 2. Zero agent modification

AThread should work with existing agents.

Target examples include:

```text
Claude Code
Codex
OpenCode
OpenClaw
custom agents
future agent runtimes
```

The long-term goal is:

```bash
athread enable

claude
codex
opencode
```

instead of requiring agents to integrate a custom SDK.

---

### 3. Agent sessions should be cheap

An idle agent session should consume very little system overhead.

The existence of 100 or 1,000 sessions should not automatically imply 100 or 1,000 fully active process trees.

AThread aims to separate:

```text
number of sessions
```

from:

```text
number of actively consuming execution resources
```

This is similar to how lightweight concurrency runtimes separate logical concurrency from physical execution resources.

---

### 4. Transparent optimization

Applications continue to see standard Linux behavior.

Underneath, AThread may optimize:

```text
process creation
runtime startup
filesystem access
repository access
PTY handling
workspace management
memory sharing
I/O scheduling
session lifecycle
```

AThread should provide a fast path where possible and fall back to normal Linux behavior everywhere else.

Unknown software must continue to work.

---

## Architecture

```text
Claude Code / Codex / OpenCode / Any Agent
                     │
                     ▼
              Standard Bash
                     │
                     ▼
┌──────────────────────────────────────────┐
│                 AThread                  │
│                                          │
│  Session Runtime                         │
│  Exec Accelerator                        │
│  Workspace Runtime                       │
│  PTY Manager                             │
│  Shared Runtime Pool                     │
│  Repository Cache                        │
│  Process Observer                        │
│  Memory Optimization                     │
│  Pressure-aware Runtime                  │
│                                          │
└─────────────────────┬────────────────────┘
                      │
                      ▼
            Linux / POSIX / ELF
                      │
                      ▼
                Linux Kernel
```

AThread begins as a userspace runtime.

Kernel-assisted optimizations may be added later when userspace optimization is no longer sufficient.

---

## Core concepts

### Agent Session

AThread treats an agent session as a first-class logical execution context.

A session may contain:

```text
session identity
working directory
environment
process tree
PTY state
workspace
runtime state
resource usage
execution history
```

AThread can therefore understand that:

```text
bash
git
rg
python
gcc
```

may all belong to the same logical agent session.

Traditional Linux normally sees only unrelated processes.

AThread adds the missing semantic layer.

---

## Session-aware execution

Traditional execution:

```text
Agent A
  ├── bash
  ├── git
  ├── rg
  └── python

Agent B
  ├── bash
  ├── git
  ├── rg
  └── python
```

AThread aims to recognize:

```text
              AThread Runtime
                    │
        ┌───────────┴───────────┐
        │                       │
    Agent A                  Agent B
        │                       │
        └──────────┬────────────┘
                   │
          shared execution layer
```

The agents still see normal Linux processes.

The runtime gains enough context to optimize them.

---

## Execution acceleration

AI agents create huge numbers of short-lived processes.

AThread will explore techniques such as:

```text
prefork
warm processes
runtime snapshots
copy-on-write
shared runtime initialization
process pools
fast process restore
binary startup optimization
```

Potential targets include frequently used runtimes such as:

```text
bash
Python
Node.js
```

The objective is not to break process semantics.

The objective is to reduce cold-start and process churn.

---

## Workspace Runtime

Multiple agents often operate on the same source repository.

Today each agent may repeatedly execute:

```bash
git status
git diff
rg
find
stat
cat
```

AThread can introduce a shared workspace abstraction:

```text
                 Workspace
                     │
        ┌────────────┼────────────┐
        │            │            │
    Agent A       Agent B      Agent C
```

A workspace may maintain shared:

```text
filesystem metadata
repository state
directory indexes
content indexes
watchers
dirty-file information
page-cache hints
```

This allows duplicated work to be reduced while preserving normal filesystem semantics.

---

## Repository-aware optimization

Agent workloads generate unusually high Git activity.

Common operations include:

```bash
git status
git diff
git log
git show
git branch
```

AThread will explore persistent repository state and shared repository metadata so that multiple agents operating on the same repository do not repeatedly perform identical work.

The command remains:

```bash
git status
```

The optimization happens underneath.

---

## PTY optimization

Most agent processes do not actually require a full interactive terminal.

They primarily need:

```text
stdin
stdout
stderr
exit status
```

AThread will explore lightweight execution streams and allocate full PTYs only when required.

Interactive applications must still work normally.

---

## Memory optimization

Concurrent agents often load identical software stacks:

```text
bash
Python
Node.js
glibc
TLS libraries
Git
agent SDKs
MCP libraries
```

AThread will explore:

```text
copy-on-write
shared immutable memory
runtime snapshots
memory deduplication
prefork models
zygote-like runtime initialization
```

The objective is to reduce duplicated memory without changing application behavior.

---

## Pressure-aware runtime

AThread will observe operating-system pressure rather than blindly starting more work.

Potential signals include:

```text
CPU pressure
memory pressure
I/O pressure
context switches
process creation rate
page-cache behavior
filesystem latency
```

Linux PSI, cgroups, procfs and eBPF may be used where available.

The objective is not to artificially restrict agents.

It is to make execution adapt intelligently to the physical machine.

---

## WSL support

WSL is a first-class target.

AThread was motivated in part by a common real-world problem:

> Running many concurrent AI agent sessions inside WSL can make the entire environment increasingly sluggish.

AThread should work as a normal userspace runtime inside WSL2.

Example:

```text
Windows
   │
 WSL2
   │
Ubuntu
   │
AThread
   │
AI Agents
```

Some advanced features may depend on kernel capabilities.

AThread will detect available features automatically.

Example:

```bash
$ athread doctor

AThread Runtime

Platform
  Linux Kernel        6.x
  WSL2                yes

Capabilities
  cgroup v2           yes
  PSI                 yes
  pidfd               yes
  io_uring            yes
  fanotify            yes
  eBPF                 partial
  AThread Kernel       no

Optimization Level    72%
```

---

## Supported deployment models

AThread is designed to support several environments.

### AThread Runtime

Install AThread on an existing Linux distribution.

Target platforms:

```text
Ubuntu
WSL2
Debian
Arch Linux
other modern Linux distributions
```

This is the primary distribution model.

---

### AThread Kernel

Optional future Linux kernel enhancements may provide additional acceleration.

Possible areas include:

```text
process startup
scheduler hints
VFS behavior
PTY scalability
page-cache behavior
session metadata
```

The userspace runtime must remain usable without a custom kernel.

---

### AThread OS

A future AThread Linux distribution may combine:

```text
AThread Runtime
+
AThread Kernel
+
preconfigured system tuning
+
agent-oriented defaults
```

AThread OS will be the optimized reference platform.

It will not be required to use AThread.

---

## Development roadmap

### Phase 0 — Workload characterization

Build a reproducible benchmark for concurrent AI agent workloads.

Measure:

```text
RSS / PSS
process count
thread count
fork rate
exec rate
context switches
filesystem operations
I/O latency
CPU PSI
memory PSI
I/O PSI
session startup latency
command latency
```

Test workloads:

```text
1 agent
10 agents
50 agents
100 agents
500 agents
```

Initial environments:

```text
Ubuntu
WSL2
```

---

### Phase 1 — Userspace runtime

Build the first version of `athreadd`.

Initial components:

```text
session registry
process observer
PTY manager
workspace tracker
resource accounting
runtime telemetry
```

Example:

```bash
athreadd start

athread run codex
athread run claude
```

---

### Phase 2 — Transparent acceleration

Begin reducing duplicated operating-system work.

Targets:

```text
process startup
workspace indexing
repository metadata
filesystem scanning
PTY usage
runtime initialization
shared caches
```

Agents should not require modification.

---

### Phase 3 — Runtime virtualization

Introduce deeper execution optimizations:

```text
warm runtime pools
prefork execution
runtime snapshots
copy-on-write workspaces
session suspend/resume
shared runtime state
```

---

### Phase 4 — Kernel-assisted optimization

Only after userspace bottlenecks are understood.

Potential research areas:

```text
exec fast paths
session-aware scheduling
VFS optimization
PTY scalability
memory sharing
page-cache optimization
agent workload hints
```

---

### Phase 5 — AThread OS

Build an optional reference Linux distribution optimized for agent workloads.

---

## Benchmark philosophy

AThread must prove itself with numbers.

The primary question is simple:

> How many useful concurrent agent sessions can the same machine run?

Baseline:

```text
Ubuntu / WSL
```

versus:

```text
Ubuntu / WSL + AThread
```

Metrics include:

```text
memory per session
CPU overhead per session
process count
context-switch rate
filesystem pressure
I/O amplification
startup latency
shell-command latency
maximum usable concurrent sessions
```

AThread should not claim success based on synthetic microbenchmarks alone.

Real agents should be used.

---

## What AThread is not

AThread is not:

```text
an agent framework
an LLM gateway
a workflow engine
an agent orchestrator
a sandbox product
a proprietary shell
a replacement for Bash
a replacement for Linux
```

AThread sits below the agent framework.

Its job is execution efficiency.

---

## Long-term vision

Unix made processes a fundamental execution abstraction.

Modern runtimes made lightweight concurrency practical.

AI agents introduce another workload class:

```text
long-lived sessions
high process churn
heavy filesystem access
large numbers of concurrent execution contexts
repeated use of identical runtimes and repositories
```

AThread aims to make this workload native to Linux.

The long-term goal is simple:

```text
Today:

10 agents     → noticeable overhead
50 agents     → system pressure
100 agents    → difficult


With AThread:

100 agents    → normal
1,000 idle sessions → inexpensive
large active agent workloads → efficiently multiplexed
```

Physical compute is still physical compute.

AThread cannot make 1,000 agents compile large projects simultaneously for free.

The goal is different:

> Make the existence, waiting, resuming and routine execution of an agent session dramatically cheaper.

---

## Project philosophy

AThread follows one core rule:

> **Do not change the agent. Change the execution environment underneath it.**

The best optimization is one the agent never needs to know exists.

---

## Status

AThread is currently in the early research and prototyping stage.

The first milestone is not a custom Linux distribution.

The first milestone is simpler:

> Prove that an ordinary Ubuntu or WSL machine can run significantly more concurrent AI agent sessions with AThread than without it.

### Current evidence

A seven-round experimental study (synthetic benchmarks, zygote simulations,
execve traces from 14 real agent sessions across Claude Code, Kimi CLI, and
Codex CLI, an external review with errata, and equivalence-checked replays)
is available in [experiments/REPORT.md](experiments/REPORT.md).
Headline findings:

* Agent session cost is task-type dependent: Python/Node runtime startup tax
  dominates analysis/test tasks (warm-fork cuts python command latency 3.2–7.5x
  with byte-identical output), while large-repo text pipelines (sort/xargs/wc)
  need shared indexing instead.
* Memory savings with full process-group accounting: -14% for 20 concurrent
  import-heavy python workers (a -87% synthetic CoW upper bound applies only
  to near-idle children).
* PTY overhead and memory/IO pressure were measured to be negligible on
  modern WSL2 — the original README diagnosis list is narrower in practice.

### Current prototype

A working v0.1 lives in [athread/](athread/): `athreadd` (warm CPython root +
fork broker) plus a transparent PATH shim with fail-open fallback. Held to a
differential standard by [athread/parity_test.py](athread/parity_test.py)
(24 cases asserting rc/stdout/stderr/side-effects identical to cold CPython).
Measured end-to-end: `python -m unittest discover` 4.8x faster (34.2ms ->
7.1ms), heavy-import `python -c` 3.6x, real-trace python p50 3.9x (exp15,
sandbox-scoped), zero divergence on 450 replayable trace commands, correct
fallback under `VIRTUAL_ENV`/`PYTHONPATH`. (Earlier 'CPU -10%' and
'498 all-MATCH' claims were retracted in round 8: whole-machine CPU
accounting and incomplete equivalence coverage.)

### Quick start

```bash
git clone <repo-url> && cd AThread

./athread/athread install    # compile the shim into ~/.athread/bin
./athread/athread start      # launch the athreadd daemon (warm CPython root)
./athread/athread status     # check daemon health

# transparent interception for anything that runs `python` on PATH
export PATH="$HOME/.athread/bin:$PATH"
export ATHREAD_SOCK="$HOME/.athread/athreadd.sock"
export ATHREAD_REAL_PYTHON="/usr/bin/python3"

python3 -m unittest discover -s tests   # 34.2ms -> 7.1ms (4.8x, output-identical)
```

Anything AThread does not recognize (`VIRTUAL_ENV`, `PYTHONPATH`, unknown
flags, daemon unreachable) falls back to normal `execve` — agents never break.
Warm modules are configurable in `~/.athread/warm_modules.txt`. See
[athread/README.md](athread/README.md) for full semantics, caveats
(`PYTHONHASHSEED=0`, single-threaded warm root) and other commands
(`stop`, `doctor`).

### Measured benefits

All numbers measured on this machine (WSL2, 20 cores). Full methodology and
raw data: [experiments/REPORT.md](experiments/REPORT.md).

| Workload | Baseline | AThread | Speedup |
|---|---|---|---|
| `python -m unittest discover` (test loop) | 34.2 ms | 7.1 ms | **4.8x** |
| `python -c` heavy imports | 21.2 ms | 6.0 ms | **3.6x** |
| Real-trace replay (equiv-checked): unittest discover | 33.7 ms | 7.8 ms | **4.3x** |
| Real-trace replay (sandbox-scoped, exp15): python p50 | 41.6 ms | 10.8 ms | **3.9x** (n=3 replayable) |
| 30 sparse sessions, python / unittest p50 | 22.2 / 34.9 ms | 6.9 / 7.9 ms | 3.2x / 4.4x |
| Real-trace replay (exp15): 450 sandbox-scoped cmds | — | byte-identical (stderr+file trees) | zero divergence |

| Memory (full process group, incl. daemon) | Baseline | AThread | Saving |
|---|---|---|---|
| 20 concurrent import-heavy python procs | 129.5 MB | 111.6 MB | **-14%** |
| Synthetic CoW upper bound (idle children) | 129.5 MB | 16.5 MB | -87% (upper bound only) |
| Docker containers (10x python, docker stats) | — | 12.4 MB/instance | worse than bare metal |

All rows verified for **output equivalence** (rc/stdout/stderr byte-identical
to cold python; 33-case differential suite `athread/parity_test.py` plus
exp15: 450/450 replayable sandbox-scoped trace commands byte-identical
after declared normalization). Hit rate on real agent traces: 92% of
python calls are shim-eligible, but python is only 2.4% of invocations in
repo-analysis tasks — the benefit density is task-type dependent. Earlier
headlines (21x replay, -36%/-89% memory, 585 execs) were **retracted** after
an external review found measurement errors; see
[experiments/REPORT.md](experiments/REPORT.md) round 7 for the full errata.

---

## License

TBD

---

## Contributing

AThread is interested in work across:

```text
Linux performance
process runtime
VFS
PTY
eBPF
cgroups
PSI
io_uring
CRIU
runtime snapshots
copy-on-write
WSL
filesystem performance
AI agent workloads
benchmarking
```

If you are interested in making Linux better at running large numbers of AI agents, contributions are welcome.

---

**AThread**

*Linux runtime for massive AI agent concurrency.*
