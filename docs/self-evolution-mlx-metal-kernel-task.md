# 真实自进化 Task：持续优化 MLX Metal GQA Kernel

本例采用 OpenEvolve 官方 [`mlx_metal_kernel_opt`](https://github.com/algorithmicsuperintelligence/openevolve/tree/main/examples/mlx_metal_kernel_opt)。目标是在 Apple Silicon 上为 Qwen3-0.6B 的 Grouped Query Attention（GQA）持续搜索自定义 Metal kernel，并在保持 `bfloat16` 正确性的前提下超过 MLX 的 `mx.fast.scaled_dot_product_attention` 基线。

它很适合 Long Task，因为官方 25 轮实验没有解决问题：最佳候选仍比 MLX 基线慢 `3.2%`。这不是一次问答能完成的工作，而是需要跨多次 Execution 保留失败、性能证据、评估方法和下一步实验的长期目标。

普通对话中的“Metal kernel 怎么写”仍是 One-off Run：可以产生 Trace，但不会自动创建 Task，也不会触发 self-evolve。

## Task 定义

```text
Goal
  让 Qwen3-0.6B-bf16 的 16:8 GQA 自定义 Metal kernel
  在 Apple Silicon 上稳定超过 MLX attention 基线

Contract
  保持 kernel 签名、输入输出和 16 query heads : 8 KV heads 语义；
  Metal 必须编译；bfloat16 correctness score >= 0.90；
  先正确性、后性能；最终直接 speedup 必须 > 1.0；
  不得绕过 subprocess hook、减少 benchmark 或隐藏失败

State
  当前最佳 kernel、父候选、轮次、各 benchmark speedup、
  编译/正确性失败、运行环境、未解决瓶颈和下一步实验

agent.md
  先复现 evaluator 有效性；失败必须保留 artifact；
  只有带环境、正确性和性能证据的结果才能更新可信 State

Knowledge
  bf16 Metal 语法陷阱、内存访问模式、SIMD/tiling 策略、
  benchmark 偏差、GPU profiling 结论和失败模式

Skills
  经多个可信 Execution 验证的 kernel correctness gate、
  统计基准、Metal profiling 和候选比较流程

Trace
  每个 kernel diff、编译日志、correctness、各 benchmark、
  baseline/candidate timing、环境、artifact 和 Outcome
```

每次 Execution 固定当时的 State、Knowledge、Skill 和 `agent.md` revision。后续运行可以利用已知失败继续搜索，但不能改写过去的实验事实。

## 起点：比基线慢，但评估已可复现

官方示例针对 Qwen3-0.6B-bf16 的 `16:8` GQA，head dimension 为 `128`。自定义 kernel 与 `mx.fast.scaled_dot_product_attention` 比较，必须通过真正作用于 benchmark subprocess 的 hook。

官方修复了四类会使历史结果失真的问题：

1. evolved kernel 没有真正注入 benchmark subprocess；
2. correctness 使用 float32，未覆盖生产中的 `bfloat16` 编译路径；
3. head ratio 曾错误地使用 `40:8`，而不是 Qwen3-0.6B 的 `16:8`；
4. 编译错误没有尽早退出，baseline 与 GPU 状态清理顺序也不可靠。

因此首次 Execution 的目标不是立即改 kernel，而是复现这些 validity gates。任何 hook 未生效、dtype 不匹配或架构不一致的性能数字都进入 `untrusted_do_not_reuse`。

## 第一次执行：先积累失败 Knowledge

官方 25 轮实验的可信结果为：

```text
initial kernel                  -11.5% vs MLX baseline
best evolved kernel             -3.2% vs MLX baseline
bf16 compilation failures        8 / 25 (32%)
short_context_quick             +6.9%
long_context_detailed          -15.9%
```

这轮首先形成带来源的 Knowledge，而不是 Skill：

```text
fact: 当前最佳候选只改善了相对回退，尚未超过 MLX 基线
failure-pattern: float-only Metal 操作会在 bf16 模板实例化时编译失败
failure-pattern: 短上下文加速可能掩盖长上下文严重回退
measurement: 必须保留逐 benchmark 结果，不能只看平均 combined_score
constraint: subprocess hook 必须证明候选 kernel 实际参与推理
```

这些 Claim 链接到候选 diff、编译日志、`best_program_info.json`、逐 benchmark 输出和 Trace。一次实验不能直接生成 production Skill。

## 第二次执行：改进可学习的评估信号

官方分析指出，搜索过程虽然拿到了详细指标，但选择仍依赖抽象 `combined_score`；`complexity` 使用代码字符数，`diversity` 使用字符级 diff。这些维度与 Metal kernel 的实际性能结构关系很弱。

下一轮应先让 evaluator 返回可解释的直接信号：

```text
compiled              Metal 是否编译
correctness            多组 bf16 输入是否满足阈值
speedup                baseline_time / candidate_time
timing                 mean / std / min / max / trials
per-benchmark          short、code、long-context、long-generation
child-vs-parent        本次 diff 对父候选的边际变化
```

MAP-Elites 特征也应来自 kernel 行为，而不是文本外形，例如：

```text
vector_width           scalar / 2 / 4 / 8
memory_access          coalesced / strided / mixed
algorithm              2-pass / 3-pass / online softmax
runtime_variance       low / medium / high
correctness_margin     距离容差门的余量
```

本轮追加的是 evaluator 与搜索表示相关 Knowledge。只有这些改动在多个 Execution 中稳定提高有效候选率和诊断能力，才值得提炼成 Skill。

## 第三次执行：用 profiling 指导 kernel 搜索

没有 GPU profiling 时，Agent 无法判断候选受限于带宽、register pressure、SIMD occupancy、cache 还是分支发散。下一轮应对可信候选采集可复现 profiling evidence，再选择单一变更维度：

```text
memory coalescing
SIMD vector width
threadgroup memory
online softmax / pass fusion
2:1 GQA head sharing
register pressure
```

每个候选仍按固定顺序执行：

```text
source validation
→ Metal compilation
→ bf16 correctness gate
→ warmup
→ baseline/candidate repeated timing
→ per-benchmark regression check
→ profiling artifact
```

错误或不完整候选直接拒绝，不能用某个短 benchmark 的加速抵消正确性失败或长上下文大幅回退。

## 多次验证后才沉淀 Skill

当同一评估流程在多个可信 Execution 中稳定识别编译失败、数值错误、环境噪声和真实性能提升后，系统才提出 Skill Candidate：

```text
Skill: Apple Silicon Metal kernel 优化评估
触发条件: 已有可信 framework baseline 和可替换 kernel
步骤: hook 验证 → bf16 correctness → 统计 timing → 分场景回归 → profiling
输入: baseline、candidate、shape/dtype fixtures、correctness threshold
输出: compile/correctness、direct speedup、variance、regression、artifact
回退: 结果不稳定或任一关键场景回退时停止晋级
证据: 至少三个可信 TaskExecution 的 Trace 与人工 Outcome
```

具体 kernel 源码、某台机器的 tokens/s 或本轮最快参数属于 State/Knowledge，不能写进通用 Skill。

## 最后才允许修改 `agent.md`

若 Knowledge 已记录 bf16 常见失败、Skill 也要求先编译和正确性，但 Agent 仍反复跳过 gate 或只汇报短上下文胜利，这才是稳定行为问题，可以提出 `agent.md` Candidate：

> 任何 kernel 晋级前必须证明 subprocess hook 生效，并报告全部 bf16 correctness 与逐 benchmark speedup；缺失一项即把结果标记为 untrusted。

完整顺序始终是：

```text
TaskExecution Evidence
→ Knowledge Candidate
→ 多次验证后的 Skill Candidate
→ 剩余稳定行为问题的 agent.md Candidate
```

## Dashboard 与知识图谱

Long Task 详情页分别展示 Goal/Contract、State、Execution lineage、Knowledge、Skill、Agent revision 和 Trace。Wiki 图谱可以形成：

```text
bf16 编译失败 ──derived-from──> float-only Metal intrinsic
长上下文回退 ──contradicts────> 短上下文加速
direct speedup ──improves──────> combined_score 可解释性
GPU profiling ──supports───────> 瓶颈归因
16:8 GQA ──depends-on──────────> 2:1 KV head sharing
```

点击 Claim 必须能回到具体 Execution、kernel diff、benchmark 和 Trace。未经审核的性能猜测不能注入下一轮。

## 验收

1. Task 只由用户显式创建；普通 Metal 问答不会自动创建 Task；
2. 环境必须是 Apple Silicon，并记录 MLX、MLX-LM、模型和系统版本；
3. subprocess hook 必须证明 candidate kernel 实际生效；
4. kernel 必须编译并通过 `bfloat16` correctness score `>= 0.90`；
5. 性能使用直接 speedup 和统计 timing，不以抽象分数代替；
6. 必须报告全部启用 benchmark，不能用短上下文胜利隐藏长上下文回退；
7. 编译、正确性、性能和 profiling artifact 都能下钻到 Trace；
8. 首轮先积累 Knowledge，多次可信证据后才产生 Skill；
9. `agent.md` 只处理剩余稳定行为问题，不保存具体 kernel 或跑分；
10. 最终成功条件是可信、可复现的整体 speedup `> 1.0`，而不是“比旧候选更快”。

## 参考

- [OpenEvolve `mlx_metal_kernel_opt` 官方示例](https://github.com/algorithmicsuperintelligence/openevolve/tree/main/examples/mlx_metal_kernel_opt)
- [示例 README](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/examples/mlx_metal_kernel_opt/README.md)
- [官方 Evolution Analysis](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/examples/mlx_metal_kernel_opt/EVOLUTION_ANALYSIS.md)
