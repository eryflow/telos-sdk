# 真实自进化 Task：持续管理一家四口的每周菜单与采购

这个例子刻意选择一件普通人每周都会重复做的事。重点不是让 Agent 一次写出更漂亮的菜单，而是让它在多次执行中先记住家庭知识，再沉淀稳定方法，最后才调整自己的长期行为规则。

## 为什么它是 Task

用户在 Dashboard 点击“创建长期 Task”，明确创建：

> 每周日根据家庭成员、下周日程、现有库存和预算，生成 7 天菜单与可直接采购的清单；每周复盘浪费、缺货和家人反馈，持续改进下一周计划。

它会跨周执行，有稳定 Goal、持续 State 和可验证结果，所以是 Task。

如果用户只在聊天中问“帮我列一下这周买菜清单”，那只是 Conversation：可以产生 Trace，也可以由用户手工保存知识，但不会自动创建 Task 或触发 self-evolve。

## Task 里保存什么

一家四口：父母、8 岁孩子、4 岁孩子。4 岁孩子花生严重过敏。每周餐费预算 900 元。

```text
Goal
  每周生成安全、合预算、符合日程且尽量少浪费的菜单与采购清单

State
  本周库存、已确认日程、待用户回答的问题、上周剩余食材、下一次执行时间

agent.md
  先读取可信 State；缺关键输入就询问；输出前检查过敏、预算、数量；
  只有审计通过的结果才能更新 completed

Knowledge
  家庭成员与过敏、口味、常用商店、包装规格、食材消耗速度、历史反馈

Skills
  库存核对；按人数和餐数换算数量；菜单与采购项双向校验

Trace
  每次读取、推理、工具调用、输出和审计的证据，不直接等同于 State
```

每次 TaskExecution 开始时固定当时的 State、`agent.md`、Knowledge 和 Skill revision，之后可以准确解释这一次计划为什么这样生成。

## 第一次执行：先积累 Knowledge

周日，用户提供：

- 周一父母加班，晚餐要能在 15 分钟内完成；
- 周三孩子学校聚餐，不需要准备儿童晚餐；
- 冰箱已有 6 个鸡蛋、半颗卷心菜和 1 盒牛奶；
- 全家不喜欢连续两天吃同一种主菜。

Agent 生成菜单和采购清单。审计发现清单总体可用，但牛奶重复购买，鸡蛋数量也没有扣除库存。

本轮首先形成待审核 Knowledge ChangeSet：

```text
preference: 全家不喜欢连续两天吃同一种主菜
fact: 4 岁孩子对花生严重过敏
decision: 周一晚餐应能在 15 分钟内完成
failure-pattern: 生成采购数量前没有扣除现有库存
```

这些 Claim 都链接到本轮输入、输出和审计证据。用户确认后进入 Task-local Knowledge；稳定的家庭事实可以提升到 Wiki。第一次执行不会因为一个错误就创建 Skill，也不会立即改写 `agent.md`。

## 第二次执行：验证知识是否真的被用上

下一周启动新的 TaskExecution。系统从 Wiki 注入花生过敏和家庭口味，从 Task-local Knowledge 注入上次的库存失败模式。

Agent 成功避免含花生食品，也没有安排连续重复主菜，但把超市一盒 12 个鸡蛋误按“需要 8 个”加入两盒，造成浪费。审计更新 State，并追加带来源的包装规格知识。

此时系统有两次可信证据说明“采购数量必须同时考虑库存、实际需求和包装规格”，但仍以 Knowledge 为主，不急着抽象成通用 Skill。

## 第三次执行：再沉淀 Skill

第三周再次出现相同计算场景，并验证下面的过程能稳定避免缺货和多买：

```text
需求量 = 每餐用量 × 用餐人数 × 餐次
净采购量 = max(0, 需求量 - 可用库存)
购买包装数 = 向上取整(净采购量 / 包装规格)
```

在至少三次有可信 Outcome 的相关 Execution 后，系统才提出“家庭采购数量核算”Skill Candidate。它包含触发条件、步骤、输入输出、失败回退和来源 Execution；通过评测并由用户发布后，后续计划可以复用。

## 什么时候才改 `agent.md`

假设后续多次执行都具备正确 Knowledge 和数量核算 Skill，但 Agent 仍在日程缺失时自行猜测，没有进入 `waiting_user`。这不是知识不足，也不是缺少计算方法，而是稳定的状态管理问题。

此时才允许提出 `agent.md` Candidate：

> 当日程、人数或健康约束缺失且会改变计划时，停止生成最终清单，写入 blocker 并进入 `waiting_user`；不得猜测。

Candidate 仍需冻结评测、严格提升、关键回归检查和人工发布。具体的“花生过敏”“鸡蛋剩 6 个”不能写进 `agent.md`。

## Dashboard 中读者会看到什么

```text
Overview    Goal、Contract、本周状态、下一步
State       已完成、未完成、阻塞、风险、不可复用信息
Executions  第 1/2/3 周的执行和审计结果
Knowledge   家庭事实、偏好、库存经验及其来源
Skills      数量核算 Skill 的证据数、版本和评测
Agent       agent.md 当前版本、Candidate diff、发布历史
Evidence    Trace、输出文件和审计记录
Evolution   Knowledge → Skill → Agent 的演进漏斗
```

知识图谱可以直观看到：

```text
4 岁孩子 ──has-allergy──> 花生
家庭菜单 ──must-avoid────> 花生
采购清单 ──depends-on────> 当前库存
采购清单 ──uses──────────> 家庭采购数量核算 Skill
周一晚餐 ──constrained-by─> 15 分钟
```

点击任何节点都能回到 Wiki Claim 和原始 Execution 证据；遇到冲突时进入审核，不使用“最后写入覆盖”。

## 验收结果

这个例子通过的标志不是“第三周分数更高”这一项，而是：

1. Task 只由用户显式创建，普通买菜对话不会变成 Task；
2. 每周执行使用固定的 State、Agent、Knowledge 和 Skill revision；
3. 未审计的 Agent 自述不能更新可信 State；
4. 第一次执行先产生 Knowledge，不直接发布 Skill 或 Agent revision；
5. 重复方法经过多次 Outcome 验证后才成为 Skill Candidate；
6. 只有剩余的稳定行为问题才进入 `agent.md` Candidate；
7. 所有知识、Skill 和 Agent 变化都能下钻到 Execution、Trace 和审计证据。
