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
| `python -m unittest discover`（测试循环） | 34.2 ms | 7.1 ms | **4.8x** |
| `python -c` 重 import | 21.2 ms | 6.0 ms | **3.6x** |
| 真实轨迹回放（等价校验）：unittest discover | 33.7 ms | 7.8 ms | **4.3x** |
| 真实轨迹 A/B：498 条回放命令中 python p50 | 70.4 ms | 9.4 ms | **7.5x** |
| 30 个稀疏会话，python / unittest p50 | 22.2 / 34.9 ms | 6.9 / 7.9 ms | 3.2x / 4.4x |
| 真实轨迹 A/B：总 CPU、native 工具 | — | CPU -10%，p50/p95 完全一致 | 零回归 |

### 内存（全进程组口径，含 daemon）

| 指标 | 基线 | AThread | 节省 |
|---|---|---|---|
| 20 个并发重 import python 进程 | 129.5 MB | 111.6 MB | **-14%** |
| 合成 CoW 上界（空闲子进程） | 129.5 MB | 16.5 MB | -87%（仅作上界） |
| Docker 容器（10 个 python，docker stats） | — | 12.4 MB/实例 | 比裸进程更差 |

以上每一行都有**结果等价背书**：rc/stdout/stderr 与冷启动 python 逐字节一致（24 项差分测试 `athread/parity_test.py` + 498 条命令轨迹回放，全部 MATCH）。真实 trace 命中率：92% 的 python 调用可进 fast path，但 python 只占分析类任务调用的 2.4%——收益密度由任务类型决定。旧 headline（21x 回放、-36%/-89% 内存、585 次 execve）经外部审阅发现测量错误后**已撤回**，完整勘误见 [experiments/REPORT.md](experiments/REPORT.md) 第七轮。

### 关键结论（来自七轮实验 + 14 个真实 Agent 会话的 execve trace）

* **第一性问题已收敛**：不是 Linux 承载不了很多 Agent，而是大量 Agent 重复冷启动 Python/Node 等重运行时，CPU 被启动成本吞掉，重复初始化堆造成内存浪费。
* **任务类型决定瓶颈**：分析/测试类任务被 Python/Node 启动税主导（等价校验后单命令加速 3.2–7.5x）；超大仓库的文本流水线（sort/xargs/wc）需要的是共享索引，AThread 后续阶段解决。
* **真实轨迹容量 A/B**：python p50 -87%、总 CPU -10%、native 命令零回归、结果逐字节等价；单会话墙钟不变（时间花在等 LLM）——收益在机器级成本与高密度并发。
* **原诊断被修正**：PTY 开销与内存/IO 压力在现代 WSL2 上可忽略；CPU 满载是"重复初始化"而非调度问题，所以第一版不需要调度器。
* **容器是正交层，不能替代 AThread**：容器每实例固定 12.4MB 开销、无运行时去重（REPORT 第五、七轮）。

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

python3 -m unittest discover -s tests   # 34.2ms -> 7.1ms（4.8x，rc/输出等价）
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
