# AThread

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

### 4. Transparent opti
