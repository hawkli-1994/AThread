# AThread 诊断验证实验报告

日期：2026-09-16
环境：WSL2（kernel 6.18.33.2-microsoft-standard）、20 vCPU、16 GB RAM、Python 3.14.4、Node v26.8.1、git 2.53.0
方法：用脚本化"模拟 agent 会话"（git status / rg / find / python / node / bash 的工具调用混合循环）做受控实验。脚本在 `scripts/`，原始数据在 `results/`。

复现方式：

```bash
cd experiments
python3 scripts/exp1_scaling.py   # 约 90 秒
python3 scripts/exp2_startup.py
python3 scripts/exp3_memory.py    # 约 40 秒
python3 scripts/exp4_repo.py
python3 scripts/exp5_pty.py       # 约 30 秒
```

---

## 实验 1：诊断验证 —— 会话数扩展性（闭环压力测试）

一个"会话" = 不停歇地循环执行工具命令 15 秒；命令延迟从会话内记录。会话数取 1 / 10 / 50 / 100。

| 指标 | 1 会话 | 10 | 50 | 100 |
|---|---|---|---|---|
| python 命令 p50 (ms) | 15.8 | 31.9 | 75.6 | **220.7** |
| node 命令 p50 (ms) | 31.7 | 32.0 | 77.7 | **219.5** |
| git status p50 (ms) | 3.5 | 3.6 | 16.9 | **34.0** |
| bash -c true p50 (ms) | 1.4 | 1.5 | 4.8 | 7.8 |
| find p50 (ms) | 3.4 | 7.7 | 18.9 | 44.3 |
| 每会话吞吐 (cmds/15s) | 3060 | 2306 | 773 | **345** |
| fork 速率 (/s) | 334 | 2389 | 3982 | 3477 |
| 上下文切换 (/s) | 10.4k | 51.9k | 68.9k | 61.0k |
| PSI CPU (µs/s) | 2297 | 10050 | 701567 | 898008 |
| PSI IO / PSI Mem | ~0 | ~0 | 很低 | 很低 |
| MemAvailable 下降 (MB) | 62 | — | 279 | 476 |

诊断运行（100 会话时每秒采样 /proc）：**20/20 核全部跑满**，load 41，可运行队列 60–88 个任务。

**结论 1（诊断成立，且根因比 README 说的更聚焦）：**
- README 列了 11 项病症，实测在 WSL2 上**唯一显著的瓶颈是 CPU 争用**——由大量短生命周期进程（尤其是 Python/Node 运行时冷启动）的进程churn 造成。IO/PSI、内存压力在此负载下均可忽略。
- 同样的工作，100 会话时延迟放大 6–14 倍，每会话吞吐掉到 1/9。fork 速率 ~3.5k/s。这就是 WSL 里"agent 一多整个环境变卡"的直接机制。
- 重要限定：这是**闭环压测**（命令之间没有 LLM 思考时间），真实 agent 的占空比低得多。它证明的是"当机器被 agent 会话填满时会发生什么"，对应 README 中 100+ 活跃会话的场景；对空闲/稀疏会话不适用。
- 数据修正（第二轮发现）：表中 rg 行为无效数据——本机 rg 不带路径参数时静默空转（exit 1）。补测真实值：`rg -n 'def foo' -t py .` p50 6.4ms。修正后 100 会话时 rg 真实膨胀倍数会高于表中的 10.4x，但不改变"python/node 冷启动是最大单项"的结论。

## 实验 2：进程启动开销 —— prefork/热运行时的收益上限

| 执行方式 | p50 |
|---|---|
| `bash -c true` | 0.68 ms |
| `python3 -c pass` | 8.03 ms |
| `python3 -c 'import json,re,...'` | 20.03 ms |
| `node -e 0` | 15.92 ms |
| 热 fork+exit（imports 已加载的父进程） | **0.59 ms** |
| 冷启动中 import 初始化占比 | 12 ms / 20 ms = **60%** |

**结论 2（强烈支持 prefork/热运行时方向）：** 对 Python/Node，prefork + CoW 快照可把每次执行从 ~20ms 降到 ~0.6ms，**省 97%**。瓶颈不在 execve 本身，而在运行时初始化（import、模块表等）。对 bash/git/rg 这类轻量 ELF 二进制没有收益（冷启动已 <1ms，热 fork+exec 反而略慢）——所以加速应只针对重运行时，不需要也不能改变进程语义。

## 实验 3：运行时内存去重 —— CoW 快照模型

25 个 Python 会话（各 import 常用 agent 栈库）：

| 模式 | 总 RSS | 总 PSS |
|---|---|---|
| 25 个独立进程 | 328 MB | 161.6 MB |
| 25 个预导入父进程的 CoW 子进程 | 245 MB | **17.1 MB** |

- 内核已经通过文件-backed 页共享自动去重了 ~51%（代码页），**真正的重复在匿名页/已初始化堆**（6.5MB/会话 的 PSS）。
- CoW 快照模型下 25 会话总 PSS 仅 17MB，**节省 89%（~5.8MB/会话）**。按 100 会话推算 ≈ 580MB，约占本机内存 3.6%。
- 限定：快照子进程是"纯 idle"的，真实会话会弄脏页面；收益打折扣但仍显著，且 Python 的 `fork`+CoW 语义与现有代码完全兼容。

## 实验 4：仓库元数据重复访问 —— 共享 workspace 缓存

2018 个文件的合成 git 仓库上：

- `git status` 稳态 p50 1.98ms，50 个"agent"并发时 p50 涨到 4.49ms（2.3 倍，锁/CPU 争用）。
- 60 次顺序 `git status` 烧掉 0.12s CPU 得到 60 个相同答案。
- **原型共享缓存**（以 index/HEAD mtime 为 key）：59/60 次命中，平均 0.039ms vs 1.98ms，**省 98%**。

**结论 3（方向正确，但单项收益小）：** 收益真实存在，但量级是毫秒级/次——它消除的是"重复劳动"，不是机器瓶颈。对大仓库（agent 常用的 monorepo 远大于 2018 文件）和跨仓库索引（rg 内容索引）收益会放大。属于锦上添花，不是主要矛盾。

## 实验 5：PTY vs 轻量流

- 每次执行延迟：pipe 0.69ms vs PTY 0.75ms（**仅 +9%**）。
- 100 个 idle 会话：pipe 与 PTY 持有成本都在 ~0.5MB/会话 量级（测量受 page cache 波动影响，两者差异在噪声内）；idle PTY 会话产生 ~2210 ctxt/s。

**结论 4（README 此条在本环境不成立）：** 在现代 WSL2（6.18 内核）上，PTY 开销可忽略。PTY 优化优先级应降低；README 中"大多数 agent 进程不需要完整终端"作为资源论证是弱的。

---

## 总体判断

### 诊断是否成立？—— 部分成立，且可精确化

| README 声称的病症 | 实验判定 |
|---|---|
| 进程churn / 短生命子进程过多 | ✅ **主瓶颈**。CPU 争用导致 6–14x 延迟放大 |
| 运行时内存重复 | ✅ 真实，~6.5MB/会话 的 PSS 重复，CoW 可省 89% |
| 重复文件系统/仓库元数据访问 | ⚠️ 真实但量级小（ms 级），属于次要收益 |
| 内存压力 / IO 竞争 | ❌ 本负载下未观测到（PSI≈0） |
| PTY 开销 | ❌ 现代 WSL2 上可忽略（+9% exec 延迟） |
| 上下文切换率高 | ✅ 现象存在（10k→69k/s），但它是 CPU 争用的症状而非独立病根 |

即：**README 的病症清单偏宽。真正的病根只有两个——重运行时的冷启动 CPU 成本和运行时堆的内存重复。**

### 设计是否是对的？—— 方向正确，优先级应重排

1. **最优先：重运行时的 prefork/CoW 热池**（README 的 Execution acceleration + Memory optimization + 部分 PTY 章节实际指向同一个机制）。实验 2/3 给出了量化上限：python 执行 -97% 延迟、内存 -89%/会话。这直接攻击唯一的主瓶颈。
2. **次优先：共享 workspace/仓库缓存**（正确但收益小，适合作为第二步）。
3. **可降级：PTY 管理、压力感知运行时**——在本环境（20 核/16GB、现代内核）实测支撑不足。README 声称的"100 个 idle 会话应很便宜"在本机本来就成立（idle 会话 ~0.5MB + 无 CPU），这部分愿景在 WSL2 上其实没有需要修的问题。
4. README 坚持的"不改 agent、不改接口、快路径+回退"的工程原则与实验结论完全相容——所有优化（fork 池、CoW、缓存）都可以在标准 POSIX 语义内透明实现，这是该设计最有说服力的部分。

### 一句话结论

> 诊断方向对但清单过宽；设计方向对但优先级应聚焦——**先吃掉 Python/Node 冷启动这个 97% 的收益，其余都是边际优化**。

### 后续实验建议

- 在更大合成仓库（50k+ 文件）上重跑实验 4，验证元数据收益是否随规模放大。
- 测稀疏占空比（命令间加 2–10s 思考延迟）下的会话上限，对应 README"100 idle + 少量活跃"的目标场景。
- 用真实 agent（如 Claude Code/Codex）的 trace 回放替代合成负载，验证命令分布假设。

---

# 第二轮实验（2026-09-17）：目标收缩后的验证

第一轮的结论把 AThread v0.1 收缩为单一目标：**Shared Warm Runtime / Agent Zygote（Python 优先）**。本轮实验回答四个跟进问题：

1. 稀疏占空比（真实 agent 节奏）下，zygote fast path 的端到端收益是多少？（exp6）
2. CoW 内存节省在子进程做真实工作（写脏内存）后还剩多少？（exp7）
3. 大仓库下文件系统/仓库元数据成本是否升为主要矛盾？（exp8）
4. warm-fork shim 的正确性和 fork 安全性边界是否成立？（exp9）

另有一个重要方法学发现：**本机 ripgrep 15.0.0 不带路径参数时静默空转（exit 1，不搜任何文件）**。第一轮 exp1/exp4 中所有 rg 数据都是空转数据（rg p50 "1.4ms" 实际是无操作）。补测真实值：2k 文件仓库 `rg -n 'def foo' -t py .` p50 = **6.4ms**。这修正了第一轮"rg 很便宜"的口径，但不改变主瓶颈结论（python/node 冷启动仍是数量级最大的单项）。**教训：benchmark 必须校验退出码/输出，不能只测延迟。**

## 实验 6：稀疏占空比 —— zygote 端到端模拟（最关键）

100 会话，命令间隔 uniform(0.5, 3.0)s（模拟 LLM think-time）。条件 A：全部冷启动 exec；条件 B：python 命令走一个真实实现的 fork-broker zygote（预导入 + CoW，unix socket IPC，模拟 `athreadd` + shim），native 工具不变。

| 指标 | A baseline | B AThread-sim | 变化 |
|---|---|---|---|
| python p50 / p95 / p99 (ms) | 19.8 / 23.3 / 26.5 | **2.7 / 3.3 / 4.4** | **-86% / -86% / -84%** |
| 全部命令 p50 / p95 (ms) | 6.1 / 20.6 | **2.7 / 17.8** | -55% / -14% |
| 每会话吞吐 (cmds) | 22.4 | 22.4 | 持平（受 think-time 限制） |
| CPU 占用（核） | 1.76 | 1.88 | 持平 |
| git/rg/bash/cat/node 延迟 | — | 与 A 完全相同 | fast path 无回归 |

**结论 5（核心验证通过）：**
- 即使在 CPU 远未饱和（1.8/20 核）的稀疏负载下，python 命令延迟仍有 **7.3 倍**差距——证明延迟瓶颈是启动成本本身，不是排队。真实 agent 的每次 python 工具调用都能从 ~20ms 降到 ~3ms，且不干扰其他命令。
- zygote 机制端到端可行：socket 请求 → fork → 子进程执行代码 → 回收，p99 仅 4.4ms。
- CPU 不省（工作本身还在做），省的是**每次调用的固定启动税**。第一轮"CPU 被启动成本吞掉"的闭环压测 + 本轮"稀疏下延迟仍差 7 倍"合起来完整覆盖了 README 的两个场景。

## 实验 7：CoW 节省 vs 子进程写脏量

25 会话，热根预导入 + 持有 8MB 堆结构 + 64MB 缓冲。子进程 fork 后按每页 4KB 写脏 X MB 再驻留：

| 每会话写脏 | 总 PSS | 每会话 PSS | vs 独立进程 (68.8MB/会话) |
|---|---|---|---|
| 0 MB | 71 MB | 2.85 MB | **省 96%** |
| 1 MB | 96 MB | 3.83 MB | 省 94% |
| 4 MB | 168 MB | 6.72 MB | 省 90% |
| 16 MB | 456 MB | 18.25 MB | 省 73% |

**结论 6：节省随写脏量近似线性衰减（每写脏 1MB 约 +1MB PSS/会话），即使每会话弄脏 16MB 仍省 73%。** agent 的一次性工具调用（import + 小数据 + 输出）弄脏量通常在 KB~低 MB 级，落在曲线左端。第一轮的 89% 不是在"纯快照理想条件"下才成立的数字。

## 实验 8：5 万文件大仓库 —— 元数据成本重新评级

2018 文件 → 50000 文件（500 目录），稳态（warm cache）：

| 操作 | 2k 仓库 (第一轮) | 50k 仓库 | 30 并发 p50 |
|---|---|---|---|
| git status --porcelain | 2.0 ms | **21.5 ms** | **96 ms**（4.5x 膨胀） |
| rg -l def | 6.4 ms（补测真值） | **71 ms** | **612 ms**（8.6x 膨胀） |
| find -name '*.py' | 3.4 ms | 17.3 ms | — |
| fresh clone 首次 git status | — | 182 ms | — |

**结论 7（修正第一轮的评级）：** 第一轮在 2k 仓库上得出"元数据优化只有毫秒级、属次要收益"——在真实规模的仓库上不成立。50k 文件（仍远小于 Linux/Chromium 级 monorepo）下，`rg`/`git status` 已经是 **几十到几百毫秒级**，30 路并发再膨胀 5–9 倍。共享内容索引 + 仓库元数据缓存应从"Phase 2 锦上添花"上调为**与 zygote 并列的第二支柱**，触发条件（fs/repo 操作占 agent wall time >10%）在 50k 文件规模上已满足。

## 实验 9：shim 正确性 + fork 安全性边界

实现最小 `athread python` 原型（fork-broker + runpy/exec），11 项断言全部通过：

- argv（含空格）、cwd 相对路径、会话 env、stdin、exit code（含 SystemExit 语义）、argparse 脚本、子进程崩溃隔离（rc=1 且 broker 存活）、子进程可见热导入（证明 fast path 真的热）。
- **fork 安全边界验证：warm root 在预导入全部目标库后仍只有 1 个线程（MainThread）**——满足"single-threaded clean state"的 zygote 前提。工程红线得到实验支持：预加载可以激进，但必须保持单线程、不初始化网络/异步运行时。

## Node 路线探测（补充）

`node --build-snapshot`/`--snapshot-blob`（V8 startup snapshot）在本机 Node v26.8.1 上可用，但 trivial 状态下 restore+run ≈ 冷启动（均 ~15–20ms）——snapshot 要预热足够多的模块图才有收益，且 Node 的 libuv 线程池/GC 使 fork 路线不安全。**确认研发顺序 Python → Node，v0.1 只做 Python。**

## 第二轮总结论

1. **v0.1 的核心机制（Python warm-fork zygote）在真实占空比下端到端成立**：python 调用延迟 -86%（p50 19.8→2.7ms），无回归，正确性 11/11。
2. **内存收益稳健**：写脏 4MB/会话仍省 90%。
3. **第二支柱确认为共享索引/仓库缓存**（50k 文件下并发 rg p50 612ms），与第一轮 2k 仓库的结论相反——评级依据规模而定。
4. **工程边界清晰**：单线程 warm root 可行（已验证）；Node 用 snapshot 路线缓行。
5. AThread v0.1 的立项数据已经齐备：**一个 PR 大小的运行时 + 一个 PATH shim**，就能把真实 agent 负载中最重的单项税（Python 冷启动）砍掉 ~86%，顺带把会话内存砍掉 ~90%。

---

# 第三轮实验（2026-09-17）：真实 Agent Trace 验证

方法：写了一个 C 语言的 exec-shim（`trace/shim.c`，77 个工具符号链接，~50µs 开销），通过 PATH 拦截真实 agent 会话中的每一次工具调用，记录 argv/cwd/耗时/退出码到 JSONL。让 **Claude Code 2.1.270** 和 **Kimi CLI 0.43.1** 在隔离 scratch 仓库中执行同一任务（摸清 20 模块×100 文件仓库结构 → 写 Python 脚本批量验证 2000 个文件符合命名约定 → git 确认工作区 → 输出报告，全程只读）。reasonix 是 Windows shim、WSL 内无 linux-x64 二进制，不可用。

## 真实命令分布（两个 agent 合计，3 个会话）

| 维度 | 数据 |
|---|---|
| exec 总数 | 120 次（claude 57 + claude wrongcwd 44 + kimi 19） |
| **python3 占比（次数）** | **2%（3 次）** |
| **python3 占比（命令耗时）** | **65%（983ms / 1516ms）** |
| node 占比 | 0%（本任务未触发 node；任务类型相关） |
| 其余高频 | wc×26、git×23、cat×19、head×9、grep×7、sort×7 —— 全是毫秒级 |

单会话逐项：

| 会话 | execs | 会话墙钟 | 命令总耗时 | 占空比 | python 耗时占比 |
|---|---|---|---|---|---|
| claude_A | 57 | 98s | 542ms | **0.6%** | 68% |
| claude_A（wrongcwd） | 44 | 119s | ~740ms | ~0.6% | 63% |
| kimi_A | 19 | 87s | 226ms | **0.3%** | 67% |

## 真实 python 命令回放（冷启动 vs zygote，replay_real.py）

对 3 个真实出现的 python 命令（`verify_convention.py` 等，逐文件 import + exec 2000 个模块做验证）重测：

| | 冷启动 exec | zygote warm fork | 加速 |
|---|---|---|---|
| verify_convention.py | 66.5ms | 43.4ms | 1.5x |
| verify_fixture.py | 63.9ms | 41.0ms | 1.6x |

分解：这 3 条命令的 warm 耗时（~43ms）几乎全是**真实工作**（解析执行 2000 个文件），冷启动比 warm 多出的 ~23ms（35%）是**运行时启动税**（解释器初始化 + import ast/re 等）——正是 zygote 能砍掉的部分。trace 中观察到首次执行 366ms vs 重测 66ms 的差是 page cache 冷启动，属于另一维度。

## 第三轮结论：假设证实，且故事讲得更准了

1. **"python 调用次数少但成本 dominant"的假设被真实数据证实**：3 个会话里 python 只占 2% 的 exec 次数，却占 65% 的命令耗时。真实 agent 的行为模式与第二轮 synthetic profile 的预测一致。
2. **占空比只有 0.3–0.6%**——比之前 synthetic 的 10-30% 估计低一个数量级。真实 agent 会话的成本结构：墙钟 ≈ LLM 思考，CPU 成本全部集中在极少数的批处理脚本上。
3. **这精确划定了 zygote 的价值边界**：
   - 单会话墙钟：zygote 几乎无感（命令时间本身只占 0.5%）。
   - **zygote 的真实价值在机器级：当 N 个这样的会话共存时，那 20ms/次的启动税 × 高 fork 率就是 exp1 里 20 核被打满的机制；每会话 6.5MB 的初始化堆重复 × 100 会话就是 exp3 里的 0.6GB 浪费。** 单会话 trace 看不到这些，多会话压测（exp1/exp6）看得到。
   - 对 agent UX：交互式命令（p95 等待）延迟 -86%。
4. **任务类型敏感性**：本任务 node 出现 0 次——node 的权重强依赖 workload（前端/TS 项目会反转）。python 优先的排序不受影响。
5. **样本局限**：2 个 agent × 1 类任务 × 单会话，绝对数字不可外推；但"python 少而重"的结构在 3 个独立会话中稳定重现（63%/67%/68%），方向性结论可靠。

**最终叙事**：真实数据把 AThread 的价值主张从"让 Linux 承载更多 agent"精确化为——*agent 会话的墙钟是 LLM 的，但机器的成本是运行时的；AThread 消灭的是后者中那个 100% 重复、0% 必要的部分（运行时初始化税）*。

---

# 第四轮实验（2026-09-17）：扩大真实数据集

## 规模

14 个真实会话：3 个 agent（Claude Code 2.1.270 / Kimi CLI 0.43.1 / Codex CLI 0.154.0）× 4 类任务 × 含 3 会话并发 burst。~~共 585 次 execve 被记录~~ **勘误（第七轮）**：原始日志 1238 行中 110 行因旧 trace shim 非原子写（多进程 dprintf 交错）损坏；修复后可确认 **510 次 execve**，另有 106 个孤儿 end 事件佐证丢失的 start，~110 条记录碎片不可恢复。分布结论（git 最频、python 少而重）在修复后不变。

## 按任务类型的真实命令画像

| 任务 | 会话 | python 次数占比 | python 耗时占比 | 主导耗时的工具 |
|---|---|---|---|---|
| A 仓库批量验证（2k 文件） | claude×4, kimi×1 | 2–5% | **26–68%** | python3 |
| B 修 bug+跑测试 | claude, kimi, codex | 5–12% | 17–40% | python3、`which`（claude 查 python 花 76ms） |
| C Node 修复（webapp） | claude, kimi | 0% | 0% | **node×4 + npm×2 ≈ 410ms**（占 50%+） |
| D 大仓库只读分析（50k 文件） | claude, kimi, codex | 0–5% | 6–16% | **coreutils 管道**：sort 1405ms、xargs 1551ms、wc/tail/cat/grep 各 500–1000ms |

全组合计（工具调用）：python3×12（2% 次数 / 17% 耗时）、node×8（2%/3%）、git×94（最频繁）、其余为 cat/wc/grep/sort/coreutils 长尾。

## 关键新发现

1. **任务类型决定瓶颈归属**——这是对第三轮"python 主导"的重要修正：
   - 分析/测试类任务：python 主导（A/B 类），zygote 直接命中；
   - Node 类任务：node+npm 占一半以上耗时，需要 Node 方案（V8 snapshot）；
   - 大仓库文本处理类任务：成本在 coreutils 管道（sort/xargs/wc/grep），这些**冷启动已经很快（<1ms），zygote 帮不上**，需要的是共享索引/内容缓存（Phase 2）和 page-cache 友好性。
   → AThread 的两个支柱（zygote + workspace 缓存）分别命中不同的任务类型，都有真实数据支撑。
2. **测试循环是真实命中场景，但加速比要诚实重测**：~~回放真实命令 `python3 -m unittest discover -s tests -v`——冷启动 64.2ms → zygote warm 3.1ms（21 倍）~~ **勘误（第七轮）：该 21 倍无效**——旧 replay 把 `-m` 当脚本路径执行，失败立即返回制造了假加速。用真实 shim+daemon 重测并做 rc/输出等价校验后：**冷 33.7ms → warm 7.8ms（4.3x）**。`python3 -`（stdin 脚本）5.3 倍同属无效（stdin 内容从未被捕获，该命令不可回放，撤回）。重计算型脚本只有 1.2–1.4x——诚实的下限：启动税占比小。
3. **三个 agent 的行为画像不同**：claude 爱用 python 脚本做批量分析；kimi 更爱纯 shell 管道（kimi_D 几乎没有 python，cat×14 wc×7）；codex 重度使用 tr/cut 文本工具和 git，零 python（但其 node 运行时本身占据整个会话）。**zygote 对 claude 类行为收益最大，对 codex 类行为收益最小**——收益取决于 agent 的编码风格。
4. **agent 也在为"找运行时"付费**：claude_B 中一次 `which` 耗时 76ms（大 PATH 扫描 shim 目录放大了这一点，但真实 agent 的长 PATH 同样存在）。
5. 并发 burst（3×claude 同时）：未观察到显著延迟膨胀（3 会话远低于 20 核饱和线），与 exp1 的 100 会话饱和结论一致——**并发惩罚只在会话密度足够高时出现**。

## 全量回放汇总（13 条真实命令）

python 命令（9 条）冷启动合计 1225ms → zygote 投影 930ms。**勘误（第七轮）：该投影基于失效 replay，撤回。** 经等价校验的重测（`results/exp10_replay_v2.txt`）：unittest 4.3x、合成等效脚本 1.2–1.6x、混合 1.4x。**真实负载下 zygote 对 python 调用启动税的削减约 15–76%，取决于"启动:工作"比例；混合真实命令约 1.4x。**

## 四轮实验的最终判断

1. 诊断成立且精确：**机器成本的构成是任务类型决定的**——python/node 启动税（A/B/C 类）+ coreutils 管道与大仓库元数据（D 类）。README 的原始清单里真正重要的就这两条。
2. v0.1（Python zygote）有真实数据支撑的最大胜利场景是**测试循环和分析脚本**（经等价校验 3–7.5x 单命令加速，见第七轮）；对 shell 管道型 agent（kimi/codex 风格）收益有限——这决定了 shim 的 PATH 优先级设计和"何时进 fast path"的启发式值得做（例如：只对 import 重的调用预热）。
3. Node 任务占耗时可达 50%+，Node 方案（snapshot）从"以后再说"上调为"第二优先"，但仍排在 Python 之后（fork 不安全，snapshot 需预热模块图）。
4. workspace 缓存/共享索引（Phase 2）在 D 类任务上有真实需求信号（单条 sort/xargs 管道秒级）。
5. 数据规模：14 会话 / 585 execve / 3 agent / 4 任务类型。仍建议持续采集（接入真实开发会话一个月），但立项所需的证据链已完整。

---

# 第五轮实验（2026-09-17）：容器能否替代 AThread？

问题：如果 agent 在容器里跑，容器是否已经解决了 AThread 要解决的问题？实测（`results/exp11_containers.txt`）：

| 测量 | 结果 |
|---|---|
| 每次 exec 都 `docker run`（新建容器） | **300–400ms**（宿主机直接 exec 22ms）——容器创建开销主导，比启动税本身大 15–18 倍 |
| 在**运行中的容器**里 exec python（`docker exec`） | 56ms（其中 ~30ms 是 docker exec 自己的开销，python 冷启动 ~20ms 与宿主机**一模一样**） |
| 容器内 python 的内存 | **勘误（第七轮）：原测量 0.0MB（进程已退出，测量失败）；exp13 用 docker stats 重测：12.4MB/实例**（cgroup 计费含 shim），比宿主机裸进程 6.5MB 更差——每实例固定开销，无跨容器去重；zygote 模型同规模 0.8–5.6MB/会话 |
| 同镜像 N 个容器共享的部分 | 只有只读镜像层（文件-backed 页）——这与宿主机上共享 .so 是同一机制，不涉及运行时匿名堆 |

## 结论：容器与 AThread 是正交的两层，不互相替代

**容器解决的问题**（AThread 不管）：隔离/安全边界、环境可复现（镜像）、资源限额（cgroup）。

**容器没有解决、也不会解决的问题**（AThread 的核心）：
1. **启动税原样保留**：容器内的进程和宿主机进程在内核眼里是同一个东西——相同的 execve 路径、相同的解释器初始化。56ms 的 `docker exec` 甚至还不如 22ms 的裸 exec。
2. **匿名堆重复原样保留**：每个容器持有自己完整的解释器堆副本（PSS≈RSS 证明无去重）。镜像层共享只覆盖文件-backed 页，那本来就是内核免费给的。
3. **会话语义**：容器 ≈ 一个会话一个容器，确实提供了"会话"边界（cgroup 记账）——这是 AThread 方案 3 想要的原语，但**每会话一个容器进程树的固定成本**（shim、namespace、独立 writable 层）高于 AThread 想要的轻量会话。

**反向注意**：microVM 路线（Firecracker/gVisor）在上述每个维度上更差（独立内核、无 page cache 共享、无跨 VM KSM）。

**互补关系**：AThread 不与容器竞争。它住在容器**下面**（对容器内的 exec 同样生效——容器里的 python 冷启动还是 20ms，zygote shim 放进镜像即可）或**旁边**（每个容器一个 zygote，CoW 收益在每个容器内部依然成立）。正确的架构是：容器做围栏，AThread 做围栏内的发动机。

---

# 第六轮（2026-09-17）：v0.1 原型落地与透明端到端验证

把 exp6/exp9 验证过的机制产品化为可运行的原型 `athread/`（`athreadd.py` 守护进程 + C shim + CLI），**agent 视角完全透明**：会话只是正常执行 `python3 ...`，由 PATH 决定走 shim 还是真二进制。

实现要点：shim 做资格检查（`-c`/`-m`/脚本/stdin 且无 VIRTUAL_ENV/PYTHONPATH/CONDA_PREFIX），通过 unix socket + SCM_RIGHTS 把 argv/env/cwd 和 fd 0/1/2 交给守护进程；守护进程从预热解释器 fork 子进程执行；任何疑点一律回退真 exec（fail-open）。子进程退出通过 SIGCHLD→pipe 唤醒 select 即时回收。语义对齐 CPython（argv、`-m` 时 cwd 入 sys.path、stdout flush 后 `_exit`）。

## exp12 结果（透明 PATH shim，本机实测）

| 场景 | baseline | athread | 加速 |
|---|---|---|---|
| S1 `python -m unittest discover` 循环（15 次） | 33.7ms p50 / 0.52s | **7.0ms p50 / 0.11s** | **4.8x** |
| S2 重 import `python -c` | 21.5ms | **6.2ms** | **3.4x** |
| S3 30 会话稀疏模拟，python p50 / p95 | 22.5 / 25.2ms | **6.8 / 7.5ms** | 3.3x |
| S3 同场景 test（unittest）p50 / p95 | 34.4 / 37.9ms | **7.8 / 9.2ms** | 4.4x |
| S3 git / rg / cat | — | 与 baseline 完全一致（2.8/7.1/2.0ms） | 零回归 |
| S4 20 并发 python 进程内存 | ~~78MB~~ | ~~50MB（含守护进程）~~ | **勘误：系统级 PSS 增量法不可验证（daemon CoW 稀释+噪声）；exp13 全进程组口径：cold 129.5MB vs warm 111.6MB = -14%** |
| S5 回退正确性（PYTHONPATH 设置时） | — | 输出正确、行为与真 python 一致 | ✅ |

冒烟测试全过：argv（含空格）、-c/-m/脚本/stdin 四种形态、env/cwd 注入、exit code 传播（42/7 均正确）、SystemExit、子进程崩溃隔离、守护进程稳定运行。

## 两个实现教训（已修复，写在这里防止重蹈）

1. **`os._exit` 跳过 stdio flush**——子进程 stdout 管道模式下块缓冲，退出前必须显式 flush（stderr 无缓冲所以最早只发现 stdout 丢）。
2. **select 超时周期 = 每个请求的固定延迟税**——第一版用 250ms select 超时等回收，所有 fast path 请求被拖慢 12 倍（253ms 恒定延迟）。正解是 SIGCHLD → `signal.set_wakeup_fd` 管道唤醒 select。
3. 另一个隐蔽 bug：daemon 里已完成请求的 conn 关闭后未从 selector 注销，fd 号复用导致 KeyError 崩溃——shim 的 2s 超时把它伪装成了"偶发慢"。

## 第六轮结论

1. **收益在真实透明路径下成立**：测试循环 4.8x、重 import 3.4x、稀疏多会话 p50 3.3–4.4x，native 命令零回归，回退语义正确。
2. 单次 fast path 全链路 ~6-7ms（socket+fork+runpy+回收），比 exp6 理想 broker 的 2.7ms 多出的部分是真实产品的固有成本（完整 env 传递、fd 转发、runpy 语义对齐），这是诚实数字。
3. 内存收益在真实形态下较小（-36%）因为这些子进程 sleep 且没做 exp7 那种重活；exp3/exp7 的 CoW 上限在会话真实工作时成立。
4. v0.1 可以开始真实使用了：`athread install && athread start`，把三行 export 放进 agent 的 shell 启动即可。下一步是真实命中率统计（shim 侧计数 fast path / fallback 原因分布）和 Node 方案评估。

---

# 第七轮（2026-09-17）：外部审阅修正 + 三组决定方向的实验

一次完整的外部技术审阅（阅读了全部代码、实验脚本、结果与 14 份 trace，并在隔离环境复现）发现：**实验可信度有四处硬伤、原型语义有六处透明性缺陷**。本轮全部修复并重测。原则：每次计时同时验证退出码/输出/副作用；不认识的场景执行前回退；不支持的结论撤回。

## 勘误清单（旧结论 → 处置）

| 旧结论 | 问题 | 处置 |
|---|---|---|
| 真实 unittest 回放 **21x** | replay 把 `-m` 当文件执行，失败立即返回 = 假加速（已复现） | **撤回**；真实 shim 重测 **4.3x**（rc+输出等价校验通过） |
| stdin 脚本 **5.3x** | 同类 bug + stdin 内容从未被捕获 | **撤回**，标记不可回放 |
| 内存节省 **-36%** | 系统级 PSS 增量法不可验证 | **撤回**；exp13 全进程组口径重测 **-14%** |
| CoW 节省 **-89%/90%** | 只统计子进程、漏 warm root；人工写脏不代表真实堆 | 修正口径：真实原型 -14%；空闲子进程上限 -87%（仍标注为合成上界） |
| 容器 python **17.2MB/实例** | 提交的原始数据是 0.0MB（进程已退出，测量失败） | exp13 用 docker stats 重测：**12.4MB/实例**，结论方向不变（容器加固定开销、无去重） |
| trace **585 次 execve** | 110 行损坏被分析器静默跳过 | repair_logs 重切分：**510 次可恢复** + 106 孤儿 end 事件；分布结论不变 |

## 原型语义修复（athread/，全部有差分测试锁定）

审阅者在 macOS/Python 3.9.6 复现了 6 个透明性缺陷，均已修复并写入 `athread/parity_test.py`（24 项 cold-vs-warm 差分测试，断言 rc/stdout/stderr/文件副作用全等，全过）：

| 缺陷 | 修复 |
|---|---|
| \>2s 的命令 ~2s 返回 125 | shim 的 2s 接收超时在 spawn 后不再适用，改为仅 connect 限时 |
| atexit 清理被跳过 | 子进程退出前 `atexit._run_exitfuncs()`（warm root 启动时清空注册表避免误触发） |
| SystemExit("msg") 丢信息 | 非 int code 打印到 stderr + rc 1，与 CPython 一致 |
| 脚本无法 import 同目录模块 | sys.path[0] 改为脚本所在目录（原来是 cwd） |
| `-c` 中 `__main__` 变量缺失 | 用真正的 `__main__` 模块对象 exec |
| 300 参数被静默截断 | MAX_ARGV 256→4096，超限 fail-open 到真 python |
| 附赠：traceback 不一致 | 过滤 daemon 内部帧 + linecache 喂源码，traceback 与冷启动逐字节一致 |

## 三组决定方向的实验

### 实验 A：真实会话命中率（`trace/expA_hit_rate.py`）

对修复后的 510 次真实 execve 逐条套 shim 的准入规则：

- **92% 的 python 调用可进 fast path**（11/12；唯一不合格的是 `python3 -` stdin 形态，venv 阻塞为 0）
- **但 python 只占全部调用的 2.4%**（12/510）——本批任务以 git/cat/tr/wc 等 native 命令为主
- 含义：v0.1 的价值密度取决于任务的 python 占比；修 bug/测试类任务密度最高，仓库批量分析类最低

### 实验 B：固定真实轨迹的容量 A/B（`trace/exp14_trace_ab.py`）

把 14 个真实会话的命令时间线（含思考间隔）在两种模式下原样重放：A=干净 PATH，B=shim PATH。逐命令等价校验（12 条 python 的 rc+stdout 全部 MATCH）。

| 指标 | A baseline | B athread |
|---|---|---|
| 执行命令数 | 498 | 498 |
| 总墙钟 | 19.7s | 19.6s（无差别——轨迹是 LLM 思考密集型） |
| **CPU ticks（user+system）** | 727 | **655（-10%）** |
| **python3 p50** | 70.4ms | **9.4ms（7.5x）** |
| python3 p95 | 144.7ms | 143.5ms（p95 那条是重计算脚本，启动税占比小） |
| git/cat/tr/wc | 基准 | p50/p95 完全一致，零回归 |

含义：**在真实分析类轨迹上，AThread 把 python 延迟砍 87%（p50）、总 CPU 降 10%，对 native 命令零回归、结果逐字节等价**。墙钟不变是因为这类任务 99% 的时间在等 LLM——这正面回应了审阅的"量纲质疑"：收益不在单会话墙钟，在机器级 CPU/内存与高密度并发下的尾延迟（exp12 S3：30 会话时 python p50 3.2x）。

### 实验 C：完整进程组内存收支（`scripts/exp13_memory_pl.py`）

全部进程组口径（含 daemon/父进程/shim，验证清理无残留）：

| 模型 | 20 并发会话总 PSS | 每会话 | vs cold |
|---|---|---|---|
| A 冷启动独立进程 | 129.5MB | 6.47MB | — |
| **B 真实原型（shim+daemon+workers）** | **111.6MB** | 5.57MB | **-14%** |
| C 合成 CoW 上限（空闲子进程） | 16.5MB | 0.82MB | -87%（仅当上界引用） |
| D 容器（10 个，docker stats） | 124.1MB | 12.4MB/实例 | 比冷启动还差（固定开销、无去重） |

含义：**真实形态下内存收益是 -14% 量级，不是 -89%**；-87% 只在子进程几乎不写内存时成立。审阅的质疑正确：收益取决于同时存活的 python 进程数，不能按 agent 会话数线性外推。容器每实例 12.4MB，再次确认容器是正交层。

## 第七轮结论

1. **修正后的净收益（有等价校验背书）**：python 单命令 p50 加速 3.2–7.5x（任务类型相关）；真实轨迹总 CPU -10%；20 并发内存 -14%；native 命令零回归；rc/输出/副作用与冷启动逐字节一致（24 项差分测试 + 498 命令回放全 MATCH）。
2. **定位按审阅建议收窄**：当前有数据支撑的定位是"面向高频、短时、重 import 工作负载的 Python 预热执行器"；扩展回"Agent Runtime"需要先用门槛实验验证（见下）。
3. **继续投入的门槛**（采纳审阅建议，预先设定）：目标场景每成功任务 CPU 降低 ≥15%，或固定延迟约束下吞吐提高 ≥20%，且结果等价、错误率不增加。当前：CPU -10%（未达线），单命令延迟与内存达标——下一步优先提升 python 调用密度场景（测试循环型任务）的端到端测量。
4. **已撤回的 headline**：21x、5.3x、-36%、-89%、585 execs、容器 17.2MB。本报告所有在册数字以本轮及以后为准。

---

# 第八轮（2026-09-17）：Issue #1 —— P0 实验硬化与真实重测

Issue #1 要求"在扩大 Node/共享索引/调度之前，先完成一轮可复现实验，回答哪些编码 Agent 负载值得复用 Python 初始化状态"。本轮完成 P0（实验与等价校验硬化），并重测了第七轮的轨迹 A/B。

## P0-1 差分测试补强（`athread/parity_test.py`，33/33 通过）

- 修复同目录导入 fixture（原先 helper 与 main 不同目录、两边同样失败也算通过——现在断言成功且输出 99）
- 新增用例：atexit 回调里 import 脚本目录模块、非 daemon 线程副作用完成、保留引用的缓冲写（验证最终文件内容）、孙进程、**100KB 环境变量**、64KB stdout
- 100KB env 用例抓到一个真实 bug：shim 把**所有** `ATHREAD_*` 前缀的用户环境变量都剥掉了，改为只剥 shim 自己消费的两个（`ATHREAD_SOCK`/`ATHREAD_REAL_PYTHON`）

## P0-2 exp12 修复

- `--session` worker 调度移到任何实验代码之前（此前每个 30 会话 worker 都会把 S1/S2 重跑一遍，污染计时）
- S1/S2 等价比较加入**事先声明的归一化**（unittest "in X.XXs" 计时行）；S3 记录 rc 并跨模式比较
- **fast path 命中验证**：用 daemon `served` 计数器证明 S3 的 278 次 python 调用全部走了 fast path（无静默回退）
- v3 结果：S1 4.7x / S2 3.4x，输出+rc 等价，278/278 fast path

## P0-3 replay_real 副作用检查升级为内容等价（原来只是"有无新文件"布尔值）

## P0-4/P0-5 exp15：取代 exp14 的硬化版固定轨迹 A/B

按 Issue #1 P0 要求逐条实现：冻结输入（workspace=fixture 提交件的新鲜副本，无旧宿主机路径依赖）；cold-first 分类（ok / 预期测试失败 / 失败，不只看退出码是否相同）；原始思考节奏（不再截断 1500ms——代价是 codex 会话忠实重放需 ~11.5 分钟/模式）；稳定 cmd_id 按 ID 配对；**stderr + 会话终态文件树（sha256）全部纳入等价比较**；动态字段归一化规则事先声明（沙箱路径/ISO 时间/HH:MM:SS/epoch/unittest 计时行/tmp 路径；env 类命令按键集+PATH 集合比较并声明剔除 harness 注入变量）；CPU 用 per-command `wait4` rusage 求和（不再用全机 /proc/stat）；原始节奏与压力回放分离报告。

**过程中修掉的 harness bug**：cat 2000 文件管道死锁（改临时文件）、stdin 继承导致 cat 型命令阻塞（改 /dev/null）、tree 存 bytes 无法 JSON 化（改 sha256）、`NORM(s, "")` 空串 replace 把 "<WS>" 插进每个字符间破坏了日期归一化。

### exp15 结果（原始节奏，489 对配对命令）

| 维度 | 结果 |
|---|---|
| 覆盖率 | 489 执行 / 23 不可恢复（node×10、npm×4 超出 v0.1 范围；/tmp 脚本×8、stdin×1 内容丢失），两模式跳过集合完全一致（mismatch 0） |
| 主机状态探针 | 35 条声明排除（date×26 时钟输出、env×8 内省、which×1 PATH 依赖）——它们内省的是活宿主状态，两次运行不可能一致，与 AThread 无关 |
| **等价性** | **450/450 可回放沙箱命令在声明归一化后逐字节等价**（454 配对中 4 条例外：2 条 rg 指向真实共享仓库（wrongcwd 病理会话）、1 条 `ls -la`（`..` 活时间戳）、1 条 pytest——见下） |
| 抓到的真实语义 bug | `python -m <缺失模块>`：冷启动打一行纯文本，warm 打完整 traceback（runpy 在 3.14 抛无 `.name` 的裸 ImportError）。已修复 + parity 锁定 + 定向重放验证一致 |
| CPU（per-command rusage 求和） | A 1868ms vs B 1874ms（**-0%**）——**撤回 exp14 的"CPU -10%"**（那是全机 /proc/stat，且混入了 workspace 复制） |
| python 延迟 | p50 41.6→10.8ms（3.9x），n=3（12 条 trace python 中 9 条不可回放，样本小，只作方向性参考） |
| 会话终态文件树 | 14 个会话全部跨模式一致 |

**结论修正**：第七轮"498 条全 MATCH / CPU -10%"的表述撤下。严格口径下的支持结论是：**在可回放的沙箱作用域命令上，fast path 与冷启动逐字节等价（含 stderr 与文件树）；该轨迹上可回放的 python 命令 3.9x，轨迹级 CPU 差异为 0**。收益上限仍在 python 密集任务——这正是 P1 实验 A 要采集的。

## P1 实验 A：python 密集任务的真实 trace 采集（2 波 × 3 agent）

任务设计（两波）：
- **pyfix**：修复 calc 包中 2 个种子 bug 直到 `unittest discover` 全过（测试循环型）
- **pyfeat**：新增 calc/stats.py（mean/median）+ 测试，反复跑测试直到全过（TDD 型）

新 shim（原子写）数据质量：6 会话 253 条记录，**0 条损坏**（旧 shim 同规模损坏率 ~9%）。

### 结果

| 会话 | wall | 命令数 | python 调用 | 任务完成 |
|---|---|---|---|---|
| pyfix_claude | 180s | 30 | **1**（`--version`） | ✅ 修复（但未跑过测试验证） |
| pyfix_kimi | 15s | 5 | **1**（unittest，38ms） | ✅ 修复 |
| pyfix_codex | 361s | 91 | **0** | ❌ 未修复（76+91 条全是 tr/cut/git） |
| pyfeat_claude | 114s | 26 | **1**（`--version`） | ⚠️ 写了代码但**从未运行测试**（2 个种子失败仍在） |
| pyfeat_kimi | 40s | 5 | **1**（unittest，58ms） | ✅ 顺带修了种子 bug |
| pyfeat_codex | 365s | 76 | **0** | ❌ 未改任何文件 |

合并新旧数据集（20 会话）：python 调用 14 次，shim 资格命中率 **86%**（12/14；不合格的是 stdin 和 `--version`，后者冷启动仅 0.7ms 无损失）；venv 阻塞 0。

### RQ1 的关键发现（对原假设的重要修正）

**"agent 在测试循环里反复冷启动 python"的前提比预期弱得多。** 即使是在明确要求的 TDD/测试循环任务上，3 个 agent 的 python 调用占比也只有 0–4%：Claude 用 grep/sed/读文件推理后直接编辑、**根本不跑测试**；Kimi 每个任务只跑了 1 次 unittest；Codex 两波任务 **0 次 python**（纯 tr/cut 管道风格）且都没完成任务（能力差异混淆了负载画像，如实记录）。

含义：
1. **收益密度是任务 × agent 风格的联合函数**。zygote 命中时 3–7.5x 是真实的（exp12/exp15 等价校验背书），但被命中的调用在真实会话中占比 2–4%。
2. 真实轨迹级 CPU 收益接近 0（exp15 严格口径 -0%）与这个低占比一致——第七轮"机器级收益"的叙事只在**高 python 密度场景**（大型测试套件反复跑、批量数据分析流水线）成立。
3. 这正面回应了 Issue 的"适用边界"问题：**AThread v0.1 的支持场景是"高频、短时、重 import 的 python 调用"，而当前 agent 行为模式下这类调用密度天然低**。要把项目推过投入门槛（CPU -15%/吞吐 +20%），要么等真实 workload 里出现高 python 密度任务（大测试套件），要么把目标转向"让 agent 更敢跑测试"（验证成本降 4x 后，Claude 式"不验证就交付"的行为可能改变——这是行为层面的收益，本实验无法测量）。
4. 样本仍小（6 新会话 × 2 任务 × 小仓库，测试 33–58ms 本就不需要循环）。更大仓库/更长会话可能反转——issue 建议的 30–50 会话分层采集继续有效，值得做。
