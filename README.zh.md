# AThread

[English](README.md) | 中文

**面向大规模并发 AI Agent 会话的 Linux 运行时。**

在普通 Linux 上不改 Agent 地运行数百个 AI Agent 会话。

AThread 是一个开源运行时，目标是让 Linux 高效承载大量并发 AI Agent 会话，同时保持标准 Linux 体验不变：

* Bash 照常工作。
* POSIX 照常工作。
* ELF 二进制照常工作。
* Git、Python、Node.js、Docker、CUDA 及现有 CLI 工具照常工作。
* Claude Code、Codex、OpenCode、OpenClaw 以及未来的 Agent 无需修改即可运行。

AThread 不替代 Linux，它让 Linux 更擅长运行 Agent。

一句话定位：

> **AThread 消灭 AI Agent 会话之间重复的运行时初始化成本。**

---

## 为什么会有 AThread？

人类开发者可能只开几个终端、每天执行几百条命令；而 Agent 会持续创建进程、扫描仓库、执行 Git 命令、启动语言运行时并维持长生命周期会话。

同时运行足够多的 Agent，操作系统就会开始承受：过度的进程创建、大量 shell/PTY 会话、重复的文件系统元数据访问、重复的运行时内存、重复的仓库扫描、高上下文切换率、重复的文件监听、海量短命子进程、内存压力与 I/O 竞争。

AThread 从一个简单的问题出发：

> 为什么一个 AI Agent 会话要像一棵传统 Unix 进程树一样昂贵？

---

## 设计思想

AThread 对 Agent 做的，正如当年 Goroutine 对 OS 线程做的事：

```text
OS Thread      →  Goroutine
Process Tree   →  Agent Session
```

目标不是把 Agent 变成专有运行时，而是**保留现有 Linux 接口，同时大幅降低运行大量 Agent 会话的底层成本**。

> 保留接口，优化实现。

---

## 实测收益

以下数据全部在本机（WSL2，20 核）实测。完整方法、原始数据与实验设计见 [experiments/REPORT.md](experiments/REPORT.md)（中文）。

### 延迟

| 工作负载 | 基线 | AThread | 加速 |
|---|---|---|---|
| `python -m unittest discover`（测试循环） | 33.7 ms | 7.0 ms | **4.8x** |
| `python -c` 重 import | 21.5 ms | 6.2 ms | **3.4x** |
| 真实命令回放：`python -m unittest discover` | 64.2 ms | 3.1 ms | **21x** |
| 30 个稀疏会话，python p50 | 22.5 ms | 6.8 ms | 3.3x |
| 30 个稀疏会话，unittest p50 | 34.4 ms | 7.8 ms | 4.4x |
| git / rg / cat（原生工具） | 不变 | 不变 | 无回归 |

### 内存

| 指标 | 基线 | AThread | 节省 |
|---|---|---|---|
| 20 个并发 python 进程 | 78 MB | 50 MB | **-36%** |
| 25 个 warm-fork 会话，PSS 上限 | 161.6 MB | 17.1 MB | **-89%** |
| 同上，加入真实会话弄脏后 | — | — | **仍保留约 90%** |

### 关键结论（来自四轮以上实验 + 14 个真实 Agent 会话的 execve trace）

* **第一性问题已收敛**：不是 Linux 承载不了很多 Agent，而是大量 Agent 重复冷启动 Python/Node 等重运行时，CPU 被启动成本吞掉，重复初始化堆造成显著内存浪费。
* **任务类型决定瓶颈**：分析/测试类任务被 Python/Node 启动税主导（warm-fork zygote 在真实命令上提速 5–21x）；超大仓库的文本流水线（sort/xargs/wc）需要的是共享索引，Athread 后续阶段解决。
* **原诊断被修正**：PTY 开销（约 0.5 MB/会话）与内存/IO 压力在现代 WSL2 上可忽略；CPU 满载是"重复初始化"而非调度问题，所以第一版不需要调度器。
* **容器是正交层，不能替代 AThread**：容器解决打包与隔离，不解决同一镜像内上百个会话重复初始化运行时的问题（实验见 REPORT 第 11 轮）。

---

## Quick start

```bash
git clone <仓库地址> && cd AThread

./athread/athread install    # 编译 shim 到 ~/.athread/bin
./athread/athread start      # 启动 athreadd 守护进程（预热 CPython root）
./athread/athread status     # 检查守护进程状态

# 让 PATH 上的 `python` 透明走 fast path
export PATH="$HOME/.athread/bin:$PATH"
export ATHREAD_SOCK="$HOME/.athread/athreadd.sock"
export ATHREAD_REAL_PYTHON="/usr/bin/python3"

python3 -m unittest discover -s tests   # 33.7ms -> 7.0ms（4.8x）
```

AThread 不认识的场景（`VIRTUAL_ENV`、`PYTHONPATH`、未知参数、守护进程不可达）会自动回退到正常的 `execve`——Agent 永远不会因此挂掉。

预热模块列表可在 `~/.athread/warm_modules.txt` 自定义。完整语义、注意事项（`PYTHONHASHSEED=0`、warm root 必须单线程、子进程继承预热 `sys.modules`）及其他命令（`stop`、`doctor`）见 [athread/README.md](athread/README.md)。

### 架构

```text
Claude Code / Codex / Kimi CLI / 任意 Agent
                    │
             Standard Bash
                    │
             AThread shim (python)
                    │
              athreadd（守护进程）
                    │
         Python Warm Root（已预热解释器 + 常用模块）
                    │ fork / Copy-on-Write
        ┌───────────┼───────────┐
        ▼           ▼           ▼
     Agent A     Agent B     Agent C
```

机制类似 Android Zygote：预热一次，fork 多次，只读页共享、写入页私有。Agent 侧完全无感知。

---

## 项目状态与路线图

当前处于早期研究与原型阶段。v0.1 原型（[athread/](athread/)）只聚焦一件事：**Shared Warm Runtime**——Python 优先，Node 计划走 V8 snapshot 路线，暂不与 Python 并行攻坚。

后续阶段按 profiling 触发、按需推进：大仓库索引（当 filesystem/repo 操作占 Agent wall time >10% 时）、PTY 优化、压力感知调度等均已从核心架构降级为未来研究项——因为实验表明它们当前不是瓶颈。

详见 [experiments/REPORT.md](experiments/REPORT.md)（六轮实验完整报告，中文）与 [athread/README.md](athread/README.md)。

---

## License

TBD

---

## Contributing

欢迎对以下方向感兴趣的贡献者：Linux 性能、进程运行时、VFS、PTY、eBPF、cgroup、PSI、io_uring、CRIU、运行时快照、写时复制、WSL、文件系统性能、AI Agent 负载、基准测试。

---

**AThread**

*面向大规模 AI Agent 并发的 Linux 运行时。*
