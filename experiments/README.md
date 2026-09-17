# AThread 实验

验证 AThread 项目诊断（多 agent 会话的成本结构）与 v0.1 方向（Shared Warm Runtime / Agent Zygote）的实验数据。

**完整结论见 [REPORT.md](REPORT.md)**；原始数据在 `results/` 与 `trace/logs/`。

## 内容

```text
scripts/          实验脚本 exp1-exp9 + fixture 生成器
trace/            真实 agent exec-trace 采集管线（C shim + 运行器 + 分析 + 回放）
results/          实验原始输出
trace/logs/       14 个真实 agent 会话的 execve trace（JSONL）
fixture/          合成仓库（gitignore，用 gen_fixtures.py 重新生成）
```

## 快速复现

```bash
cd experiments
python3 scripts/gen_fixtures.py all     # 生成 4 个合成仓库（bigrepo 约 1 分钟）
./trace/setup_shims.sh                  # 编译 exec-trace shim

# 第一轮：机制测量（约 10 分钟）
python3 scripts/exp1_scaling.py         # 会话数扩展性（1/10/50/100）
python3 scripts/exp2_startup.py         # 冷启动 vs prefork
python3 scripts/exp3_memory.py          # CoW 内存去重
python3 scripts/exp4_repo.py            # 仓库元数据重复成本
python3 scripts/exp5_pty.py             # PTY vs pipe

# 第二轮：目标收缩验证（约 8 分钟）
python3 scripts/exp6_sparse_duty.py     # 稀疏占空比 baseline vs zygote 模拟
python3 scripts/exp7_cow_dirty.py       # CoW 节省 vs 写脏量
python3 scripts/exp8_big_repo.py        # 5 万文件大仓库（需先生成 bigrepo）

# 第三轮：真实 agent trace（需要 claude / kimi / codex CLI）
./trace/run_agent.sh mysession claude -p "你的任务" --permission-mode bypassPermissions
python3 trace/analyze_trace.py
python3 trace/replay_real.py
```

## 方法学备注

- shim 通过 PATH 拦截工具调用，对 agent 完全透明；每次调用开销 ~50µs。
- shim 只能拦截经 PATH 查找的调用；agent 用绝对路径 exec 的调用会漏记。
- 采真实 trace 时需把 agent 运行时自身的常驻进程（如 codex 的 node）从统计中剔除
  （analyze_trace.py 已按时长 >120s 过滤）。
