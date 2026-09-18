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
| 真实轨迹回放（沙箱作用域，exp15）：python p50 | 41.6 ms | 10.8 ms | **3.9x**（可回放 n=3） |
| 30 个稀疏会话，python / unittest p50 | 22.2 / 34.9 ms | 6.9 / 7.9 ms | 3.2x / 4.4x |
| 真实轨迹回放（exp15）：450 条沙箱命令 | — | 逐字节等价（含 stderr 与文件树） | 零分歧 |
| 任务级 A/B（exp16）python 密集 d10：每任务 CPU（含 daemon 摊销） | 0.391 s | 0.070 s | **-82%**（432 步 0 分歧） |
| 任务级 A/B（exp16）python 密集 d10：任务墙钟 P50 | 466 ms | 195 ms | **-58%** |
| 任务级 A/B（exp16）git/cat 密集对照：每任务 CPU | 0.039 s | 0.037 s | -3~5%（诚实对照值） |

### 内存（全进程组口径，含 daemon）

| 指标 | 基线 | AThread | 节省 |
|---|---|---|---|
| 20 个并发重 import python 进程 | 129.5 MB | 111.6 MB | **-14%** |
| 100 个并发 worker、单 warm root（exp17） | 1281.8 MB | 923.7 MB | **-28%** |
| 盈亏平衡（exp17）：一个 warm root 需要 | — | ~9 个并发共享子进程 | 低于此数为净亏损 |
| 100 worker 跑在 docker 容器中（cgroup 口径） | — | 1870 MB | 是 warm 的 2 倍 |
| 合成 CoW 上界（空闲子进程） | 129.5 MB | 16.5 MB | -87%（仅作上界） |
| Docker 容器（10 个 python，docker stats） | — | 12.4 MB/实例 | 比裸进程更差 |

以上每一行都有**结果等价背书**：rc/stdout/stderr 与冷启动 python 逐字节一致（33 项差分测试 `athread/parity_test.py` + exp15：450/450 可回放沙箱 trace 命令逐字节等价 + exp16：三臂 432/432 步、192/192 任务终态树一致）。真实 trace 命中率：86–92% 的 python 调用可进 fast path，但 python 只占真实会话调用的 0–4%——收益密度由任务类型决定；固定思考延迟下吞吐 +7~9%（延迟受限有界），未达 +20% 门槛。内存收益以"≥9 个并发子进程共享一个 warm root"为前提，多 venv（每环境一个 root）线性右移盈亏点。旧 headline（21x 回放、-36%/-89% 内存、585 次 execve）经外部审阅发现测量错误后**已撤回**，完整勘误见 [experiments/REPORT.md](experiments/REPORT.md) 第七轮，修正后的测量见第八、九轮。

### 关键结论（来自九轮实验 + 20 个真实 Agent 会话的 execve trace）

* **第一性问题已收敛**：不是 Linux 承载不了很多 Agent，而是大量 Agent 重复冷启动 Python/Node 等重运行时，CPU 被启动成本吞掉，重复初始化堆造成内存浪费。
* **任务类型决定瓶颈**：分析/测试类任务被 Python/Node 启动税主导（等价校验后单命令加速 3.2–7.5x）；python 密集任务（exp16）每任务 CPU -82%（含 daemon）、任务墙钟 -58%；超大仓库的文本流水线（sort/xargs/wc）需要的是共享索引，AThread 后续阶段解决。
* **收益面比"Agent 运行时"窄**：AThread v0.1 本质是一个"python 高频调用加速器"，盈亏由 并发数 × python 密度 × import 重量 ÷ 环境分散度 决定（exp16/17/18）。真实会话中前两个变量天然低（P1-A：0–4%），所以轨迹级 CPU 收益近 0（exp15 硬化口径）；当它们高时收益巨大且开销诚实。
* **fast path 剩余耗时拆解（exp18）**：固定开销仅 ~1ms（fork+fd/env 设置），user_code 几乎全是 import 时间——预热可将其最多压缩 44x；端到端 shim 往返另有 ~3–4ms 常数（已声明）。
* **真实轨迹回放（exp15，硬化口径）**：可回放沙箱命令 450/450 逐字节等价；python p50 3.9x（n=3）；轨迹级 CPU 差异 -0%（per-command rusage）。旧"CPU -10%""498 全 MATCH"表述已撤回。
* **原诊断被修正**：PTY 开销与内存/IO 压力在现代 WSL2 上可忽略；CPU 满载是"重复初始化"而非调度问题，所以第一版不需要调度器。
* **容器是正交层，不能替代 AThread**：容器每实例固定 ~18.8MB 开销、无运行时去重，100 实例 1.87GB 是同负载 warm 方案的 2 倍（REPORT 第五、七、九轮）。

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
