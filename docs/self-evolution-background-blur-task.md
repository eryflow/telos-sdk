# 真实自进化 Task：持续优化视频人像背景虚化

本例改编自 OpenEvolve 官方 [`background_blur`](https://github.com/algorithmicsuperintelligence/openevolve/tree/main/examples/background_blur) 示例。目标很直观：视频中的人保持清晰，背景被虚化，同时让每帧处理得更快。

它不是一次性问答，而是用户显式创建、允许反复执行和验证的长期 Task：

> 从正确但缓慢的二维高斯卷积开始，在不突破画质硬门禁的前提下，持续搜索更快的背景虚化实现；保存每次尝试、评估证据和可复用经验。

如果用户只在普通对话中问“怎样给照片背景加模糊”，那仍是 Conversation：可以产生 Trace，但不会自动创建 Task，也不会触发 self-evolve。

## Task 定义

```text
Goal
  在画质硬门禁内最大化背景虚化速度

Contract
  保持函数输入输出兼容；错误或作弊实现得 0 分；
  mean SSIM >= 0.98；worst-frame SSIM >= 0.95；
  worst-region SSIM >= 0.90；过门后 score = baseline_time / candidate_time

State
  当前最佳实现、评估轮次、已占用 MAP-Elites 单元、
  未解决失败、最新质量/速度指标、下一步实验

agent.md
  先过正确性和画质门禁，再比较速度；不得绕过评估器；
  失败时读取 artifact；只有审计证据可以更新可信 State

Knowledge
  已验证的性能瓶颈、质量风险、作弊模式、优化结果和测量陷阱

Skills
  经过多次 Execution 证明可复用的级联评估、对抗测试和公平基准方法

Trace
  每个候选的代码 diff、父候选、三级评估、SSIM、计时、artifact 和 Outcome
```

每次 TaskExecution 开始时固定当时的 State、Knowledge、Skill 和 `agent.md` revision，因此任何一次结果都能重放和解释。

## 起点：正确，但故意很慢

官方 seed 使用完整二维高斯核直接卷积，复杂度随核面积增长，是正确但缓慢的 `O(k²)` 实现。它提供可信参考输出和 `1.0x` 基线，而不是一个已经高度优化、难以判断进展的起点。

Task Contract 把画质设为硬门禁，而不是综合分中的一个软权重：

```text
mean SSIM         < 0.98  → reject
worst-frame SSIM  < 0.95  → reject
worst-region SSIM < 0.90  → reject
otherwise                   score = speedup
```

因此“非常快但画面错误”的候选得分是 `0`，不能用速度补偿错误。

## 第一次执行：先发现并记录 Knowledge

第一批候选包含几个看似聪明的捷径：直接返回输入、忽略人物蒙版、减小模糊强度，以及只计算第一帧的模糊背景并在后续帧复用。

最后一种 stale background cheat 最危险。它速度很快，mean SSIM 和 worst-frame SSIM 也可能过线，但人物移动后会留下局部“人形残影”。整帧平均值掩盖了这块小区域；检查最差 `16×16` 区域后，作弊候选的 worst-region SSIM 会明显跌破门禁。

第一次 Execution 首先生成有来源的 Knowledge ChangeSet：

```text
fact: seed 的完整二维高斯卷积为 O(k²)，是主要性能瓶颈
failure-pattern: 复用首帧背景会留下局部残影
failure-pattern: mean / worst-frame SSIM 会掩盖局部损伤
procedure: worst-region SSIM 可捕获局部作弊
```

每条 Claim 都链接到候选 diff、失败帧、区域指标和评估 Trace。此时只积累 Knowledge，不因为一次发现就发布 Skill，也不修改 `agent.md`。

## 第二次执行：只给正确候选计时

评估采用三级 cascade，避免在错误候选上浪费昂贵计时：

```text
stage 1  cheap smoke      两帧检查 shape、finite、是否真正模糊
stage 2  quality gate     全部帧检查 mean / worst-frame / worst-region SSIM
stage 3  benchmark        只有通过前两级的候选才能计时
```

失败不只返回 `0`。Artifact 会说明失败阶段、实际指标、阈值、最差帧或最差区域，以及类似“不要复用旧背景”的可执行反馈。下一次 Execution 注入这些知识后，不必重新踩同一个坑。

计时本身也需要防止误导。若只缓存一次 baseline，机器当时的瞬时负载会把此后所有 speedup 放大。可信做法是在同一次评估中交错测量 baseline 和 candidate，分别预热并取多次运行的最小值，使缓慢的系统漂移尽量在比值中抵消。

这轮追加 Knowledge：

```text
failure-pattern: 单次缓存 baseline 会把环境噪声误认为候选加速
procedure: baseline/candidate 交错计时，预热后取 best-of-N
fact: separable convolution、float32、批处理或降采样是不同的候选路线
```

## 第三次执行：保留不同解法，再沉淀 Skill

只保存当前最高分容易让搜索过早收敛。官方示例用 `(complexity, ssim)` 作为 MAP-Elites 网格，使“精确但较复杂”和“牺牲少量画质换取更高速度”的候选能同时保留。每个候选仍必须先通过质量硬门禁；MAP-Elites 负责保留多样性，不负责放宽正确性。

当级联评估、区域质量门和交错计时在多个可信 Execution 中持续拦住坏候选，并正确区分有效优化后，系统才提出 Skill Candidate，例如：

```text
Skill: 受质量约束的热点函数优化
触发条件: 需要优化一个已有可信参考输出的热点函数
步骤: 对抗作弊测试 → cheap smoke → 完整质量门 → 交错基准 → 复测胜者
输入: reference、candidate、质量阈值、确定性 fixtures
输出: 通过/拒绝、质量指标、speedup、诊断 artifacts
回退: 指标不稳定时停止晋级，并在空闲机器复测
证据: 多个 TaskExecution 的 Trace 与 Outcome
```

Skill 通过冻结评测和人工发布后才能进入 production。单个快速候选不能直接产生 Skill。

## 最后才允许修改 `agent.md`

假设 Knowledge 已明确记录 stale background cheat，Skill 也规定必须运行对抗测试，但 Agent 在多次 Execution 中仍跳过作弊测试、直接相信平均 SSIM。这是剩余的稳定行为问题，才允许提出 `agent.md` Candidate：

> 优化前必须先运行 evaluator gaming tests；任一作弊候选未得 0 分时，停止搜索并把 evaluator 标记为 blocker。

具体的 SSIM 数值、某个候选代码或某轮最快时间仍属于 Contract、Knowledge 或 State，不能塞进 `agent.md`。

完整顺序始终是：

```text
TaskExecution Evidence
→ Knowledge Candidate
→ 多次验证后的 Skill Candidate
→ 仍无法由 Knowledge/Skill 解释的 agent.md Candidate
```

## Dashboard 与知识图谱

Dashboard 的 Task 页面可直接看到：

```text
Overview    Goal、Contract、当前最佳结果和下一步
State       已完成轮次、未解决失败、blocker、风险
Executions  候选 lineage、MAP-Elites 单元和每轮 Outcome
Knowledge   性能瓶颈、作弊模式、有效策略及来源
Skills      评估 Skill 的证据数、版本和评测
Agent       agent.md 当前版本、Candidate diff、发布历史
Evidence    Trace、候选代码、质量报告、计时和 artifacts
Evolution   Knowledge → Skill → Agent 的演进漏斗
```

知识图谱可以把结论和证据连起来：

```text
二维高斯卷积 ──belongs-to────> O(k²) 复杂度
局部人形残影 ──derived-from──> stale background
worst-region SSIM ──applies-to──> 局部损伤检测
交错计时 ──applies-to────────> baseline 漂移控制
半分辨率模糊 ──depends-on────> 可接受的 SSIM 预算
```

图谱是 Wiki Claim 的投影。点击节点或边可以回到 TaskExecution、候选 diff、评估 artifact 和 Trace；未经审核的推断不能静默注入下一轮。

## 验收

1. Task 只能由用户显式创建；一次性的图片模糊问答不会自动成为 Task；
2. seed 正确且缓慢，完整二维卷积明确为 `O(k²)` 起点；
3. 质量是硬门禁，mean、worst-frame、worst-region SSIM 任一失败均得 `0`；
4. stale background cheat 必须被最差区域指标拒绝；
5. 只有通过 smoke 和画质门的候选进入计时；
6. baseline 与 candidate 交错计时，失败 artifact 可供下一轮定位原因；
7. MAP-Elites 保留不同 complexity/SSIM 路线，但不能绕过画质门；
8. 首轮先产生 Knowledge，多次可信证据后才产生 Skill；
9. 只有剩余的稳定行为问题才产生 `agent.md` Candidate；
10. State、Knowledge、Skill 和 Agent 的每次变化都能下钻到 Trace 和 Outcome。

## 参考

- [OpenEvolve `background_blur` 官方示例](https://github.com/algorithmicsuperintelligence/openevolve/tree/main/examples/background_blur)
- [示例说明与评估设计](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/examples/background_blur/README.md)
