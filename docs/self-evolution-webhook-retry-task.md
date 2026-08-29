# 真实自进化任务：Webhook 重试语义修复

这个任务用于验证 Profile 指令能否让真实 Codex 与 Kimi Code 更可靠地修复带副作用的代码缺陷。评测不使用模拟分数；两个 Harness 都在独立工作区修改同一冻结代码仓，由进程隔离的确定性 Evaluator 运行私有测试。

## 任务定义

- TaskType：`python-webhook-retry-repair`
- Candidate 维度：`instructions`
- Harness：`codex`、`kimi-code`
- 每个 `case × variant × harness` 运行 3 次
- 最多优化 3 轮，目标分数 0.90
- production 只允许人工 `promote`

冻结 fixture 是一个无第三方运行时依赖的小型 Python 项目。公开任务要求修复 `deliver(events, send, sleep, max_attempts=3)`：只重试暂时性网络错误，使用指数退避，保持事件顺序，已经成功的事件不得重复发送，永久错误必须立即返回。公开测试只覆盖正常发送和一次暂时性失败。

私有 Evaluator 覆盖以下边界，但不向 Agent 或 Optimizer 暴露具体输入和断言：

1. 永久错误零重试，标记为 protected；
2. `max_attempts` 没有 off-by-one；
3. 退避序列严格为 `1, 2, 4, ...`，成功或终止后不再 sleep；
4. 多事件之间重试状态隔离且顺序稳定；
5. 前序成功事件不会因后序失败而重复产生副作用；
6. 代码仍通过公开测试且不新增依赖。

主分数为六项通过率。永久错误回归直接触发 critical-regression gate；成本上限为 Reference 的 1.25 倍，p95 延迟上限为 1.50 倍。所有 trial 还必须通过 Pack/Profile/Case digest、Attempt/Trace 和权威 LLM Span 完整性检查。

## Profile 与优化假设

Reference Profile 只给出基线指令：

> Reproduce the failure, make the smallest correct change, and run the existing tests.

Optimizer 只能读取公开任务、Reference 指令、聚合分数、失败分类和对应 Trace 标识。每轮只能重写 `instructions.md`，并给出一个可证伪预测。例如：要求 Agent 在编辑前枚举契约边界、识别副作用、补充针对性测试，预计会提高私有边界用例通过率。Optimizer 看不到私有测试、rubric 或 gold。

## 执行协议

Runner 在每个 TELOS 隔离工作区执行真实非交互 Harness：

```bash
codex exec --ephemeral --approve-for-me --sandbox workspace-write -C "$TELOS_EVALUATION_WORKSPACE" "<public task + Profile instructions>"
kimi --auto --prompt "<public task + Profile instructions>"
```

两者都继承 `TELOS_ATTEMPT_ID` 并通过本地 Gateway 记录 Trace。Runner 只返回 Harness/Profile identity、退出状态、成本与延迟；私有 Evaluator 随后在工作区外加载冻结用例并返回分项结果和总分。

建议用隔离的 TELOS home 执行，避免混入日常数据：

```bash
export TELOS_HOME="$(mktemp -d)/telos-home"
telos evolve bootstrap --task python-webhook-retry-repair \
  --instructions "Reproduce the failure, make the smallest correct change, and run the existing tests."

# 创建失败 Outcome 和 Context Pack 后，冻结 fixture；--command 必须放最后。
telos evolve freeze --task python-webhook-retry-repair \
  --pack <pack-id> --outcome-id <outcome-id> --protected \
  --fixture <frozen-repository> --private-rubric <rubric.json> \
  --private-gold <private-cases.json> \
  --evaluator-command python3 <private-evaluator.py> \
  --command python3 <dual-harness-runner.py>

telos evolve run --task python-webhook-retry-repair \
  --rounds 3 --runs 3 --target-score 0.90 \
  --optimizer-command python3 <evidence-optimizer.py>
```

## 验收

一次有效实验产生 36 个不可变 trial：`1 case × 2 variants × 2 Harnesses × 3 runs × 最多 3 rounds`。每轮只有严格高于当前 Reference 且全部 gate 通过的 Candidate 才成为下一轮 Reference；失败轮保留证据但不进入链。最终 Candidate 只能标记为 `recommended`，用户检查 Profile diff、私有分项汇总和 Trace 后再执行 `telos evolve promote`。

真实运行会消耗 Codex/Kimi 模型额度。若任一 Harness 没有产生可归属的权威 LLM Span，本次实验无效，不能用进程退出码代替证据。
