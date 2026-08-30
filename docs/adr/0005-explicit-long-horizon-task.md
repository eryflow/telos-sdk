# ADR-0005：显式长任务、版本化状态与分层 Self-Evolve

## 状态

Proposed

## 上下文

TELOS 当前使用 `TaskType → TaskRun → Attempt` 表达任务，但 Dashboard 的普通输入也会创建 TaskRun，容易把一次日常对话和需要持续管理、反复执行、自进化的长期 Task 混为一谈。

本 ADR 明确：Task 是用户显式定义并发起的长期工作单元。普通对话、临时问答和未绑定的 Harness Session 不是 Task，也不能自动进入 self-evolve。

Task 状态管理参考 `/Users/george/Dev/LongHorizon-Harness` 的语义约束：稳定 Task Contract、基于审计事实维护 Current Task State、每轮只推进一个主状态变化、只有干净且与契约一致的审计才能支持完成。TELOS 采用这些状态原则，不直接复制其 Manager/Executor/Auditor 实现。

## 决策

### 1. 术语

| 术语 | 定义 |
|---|---|
| Conversation | 普通对话或临时 Harness Session；默认只保存 Trace，不创建 Task，不参与 self-evolve |
| Task | 用户显式创建的长期目标容器；拥有稳定身份、Goal、Contract、State、`agent.md`、Knowledge、Skills 和演进策略 |
| TaskExecution | Task 的一次执行或继续推进；固定使用一组 State/Agent/Knowledge/Skill revision |
| Attempt | 一个 Execution 中由某个 Harness 发起的一次尝试；handoff 或失败重试会产生新 Attempt |
| Round | Attempt 内一次“计划/执行/审计”状态推进；不是所有 Harness 都必须暴露 Round |
| TaskStateRevision | Task 当前可信状态的不可变版本，可跨 Execution 延续 |

层级：

```text
TaskType                         可选分类，不是 Task 身份
└── Task                        显式定义的长期目标
    ├── Goal / Task Contract
    ├── current TaskStateRevision
    ├── production agent.md revision
    ├── Knowledge bindings
    ├── Skill bindings
    └── TaskExecution           一次启动或继续推进
        └── Attempt             Harness 尝试 / handoff
            └── Trace / Span
```

### 2. 创建边界

只有以下显式动作可以创建 Task：

- Dashboard 点击“创建长期 Task”，填写 Goal 并确认；
- CLI 执行 `telos task create`；
- 调用有 write token 的 `POST /api/v1/tasks`；
- 在普通对话上点击“转为长期 Task”，预览并确认 Goal/Contract。

以下行为不能创建 Task：

- 打开或继续一次普通对话；
- 单纯产生 Trace；
- 对话持续时间很长；
- 模型判断“这看起来像任务”；
- 自动根据最近 Session、workspace 或时间猜测。

普通对话可以由用户显式保存某条内容到 Wiki，但这不属于 self-evolve，也不会自动创建 Task。

### 3. Goal 与 Task Contract

`goal.md` 保存用户原始、稳定的长期目标。Task Contract 将 Goal 解释为可执行、可验证的目标状态，但不能改写为更容易完成的替代目标。

Contract 至少包含：

```text
目标解释
最终状态载体
权威输入
验收约束
允许的状态产生流程
持久化 / 提交边界
可接受证据
禁止捷径
```

Goal 默认不可被 Agent 修改。Contract 可以提出 Candidate，但涉及目标范围和验收条件的变化必须由用户确认。

### 4. Task State

State 是 Task 的可信当前状态，不是对话摘要。每次更新生成不可变 revision，并引用产生该结论的 Execution、Attempt、Round 和 Evidence。

```json
{
  "schema_version": 1,
  "task_id": "task-...",
  "revision": 7,
  "status": "running",
  "completed": [],
  "incomplete": [],
  "blockers": [],
  "risks": [],
  "untrusted_do_not_reuse": [],
  "artifacts": [],
  "next_action": {},
  "evidence_refs": []
}
```

状态：

```text
defined → ready → running ↔ waiting_user
                         ↘ blocked
                         ↘ completed
                         ↘ cancelled
```

采用 LongHorizon-Harness 的状态约束：

1. Task Contract 是跨轮和跨 Execution 的稳定语义锚点；
2. `completed` 只能写入有审计证据支持的事实；
3. Executor 自述不能直接更新可信 State；
4. 每轮只推进一个主要状态变化；
5. `incomplete`、`blockers`、`risks` 和 `untrusted_do_not_reuse` 不得在压缩时丢失；
6. 完成必须存在于用户或下游真实消费的最终状态载体；
7. 只有审计结论同时满足 `complete + clean + aligned`，Task 才能进入 `completed`；
8. 需要用户信息时进入 `waiting_user`，不得由 Agent 编造答案。

TELOS 首版可以由同一 Harness 依次完成执行和审计，但 State Store 只接受结构化 State Patch 及其 Evidence，不直接接受自由文本“已完成”。

### 5. Task 资产

每个 Task 维护六类资产：

```text
goal.md                         稳定目标
contract.json                   可验证任务契约
state/<revision>.json           可信状态版本
agent/<revision>/agent.md       Task 专属 Agent 指令
knowledge/<manifest>.json       本次绑定的知识 revision/digest
skills/<manifest>.json          本次绑定的 Skill revision/digest
```

`agent.md` 只保存稳定行为规则、角色边界和状态协议，不嵌入当前 State、完整 Trace 或知识正文。这样 State 或 Wiki 更新不会无意义地生成新的 Agent revision。

Trace 是执行证据，不是 Task State。Execution 通过固定 ID 关联 Trace、Agent revision、Knowledge manifest 和 Skill manifest，保证之后能解释“这次执行到底使用了什么”。

### 6. Knowledge

Task 有两层知识：

- `injected`：从全局 Wiki 检索并绑定到本次 Execution 的相关知识，只读；
- `task-local`：本 Task 多次执行产生的事实、决策、经验和失败模式。

每次 Execution 启动时冻结 Knowledge manifest。Execution 中途 Wiki 发生变化不会悄悄改变当前上下文；新知识从下一次 Execution 或显式 refresh 开始使用。

Task-local knowledge 经审核后可以提升到 Wiki；被 Wiki 更新或撤回的知识不会改写历史 Execution。

### 7. Skills

Skill 是经过多次执行验证的可复用过程，不是某次任务的事实，也不是 Agent Prompt 中的一段临时建议。

Skill 至少包含：

```text
触发条件
适用范围
步骤 / 工具策略
输入输出契约
失败与回退
来源 Execution
评测结果
revision / digest
```

Task 可以绑定全局 Skill 或 Task-local Skill。Skill Candidate 默认至少需要三个有可信 Outcome 的相关 Execution 支持，具体数量由 Task policy 调整。

### 8. Self-Evolve 顺序

Self-evolve 只对显式 Task 启用，并在多次 TaskExecution 上运行。每轮证据按以下顺序归因和沉淀：

```text
Execution evidence
→ Knowledge Candidate
→ Skill Candidate
→ agent.md Candidate
```

#### 第一层：Knowledge

每次 Execution 都可以提出 Knowledge ChangeSet。优先积累：

- 已确认事实和偏好；
- Task 当前决策；
- 成功/失败 Outcome；
- 环境、资产和依赖变化；
- 带来源的失败模式。

#### 第二层：Skill

当多个 Execution 出现重复步骤、稳定成功策略或重复失败恢复时，才抽取 Skill Candidate。单次成功不能证明一个 Skill 可复用。

#### 第三层：`agent.md`

只有当失败不能由缺失知识或缺失 Skill 解释，且表现为稳定的规划、验证、工具选择或状态管理问题时，才允许提出 `agent.md` Candidate。

`agent.md` Candidate 仍需 Reference/Candidate 冻结评测、strict improvement、critical-regression gate 和人工 promote。

这不是一次性阶段，而是每批新 Execution 证据都重新执行的归因顺序。Optimizer 不能把具体事实塞进 `agent.md`，也不能把一次操作步骤伪装成通用 Skill。

### 9. Execution Context

一次 TaskExecution 的上下文按固定来源组装：

```text
Goal + Task Contract
→ current TaskStateRevision
→ production agent.md
→ selected Skill revisions
→ injected Wiki / task-local Knowledge revisions
→ current Execution input
→ relevant Evidence references
```

完整历史 Trace 不直接塞入 Prompt。需要时通过 Evidence reference 下钻。

### 10. 数据模型迁移

新增：

```text
tasks
task_state_revisions
task_agent_revisions
task_executions
task_knowledge_bindings
task_skill_bindings
skill_revisions
```

现有 `task_runs` 在迁移期作为 `task_executions` 的内部兼容表，并新增 nullable `task_id`：

- `task_id IS NOT NULL`：显式 Task 的 Execution，可参与 self-evolve；
- `task_id IS NULL`：旧记录、普通运行或未归属证据，不参与 self-evolve。

`task_types` 保留为可选分类和默认策略，不再承担长期 Task 身份。

### 11. CLI 与 API

```bash
telos task create --name NAME --goal-file goal.md
telos task show <task-id>
telos task execute <task-id> --harness codex
telos task resume <execution-id> --harness kimi-code
telos task checkpoint <task-id>
telos task finish <task-id>
```

```text
POST /api/v1/tasks
GET  /api/v1/tasks/{id}
POST /api/v1/tasks/{id}/executions
POST /api/v1/task-executions/{id}/attempts
POST /api/v1/tasks/{id}/state-patches
GET  /api/v1/tasks/{id}/knowledge
GET  /api/v1/tasks/{id}/skills
GET  /api/v1/tasks/{id}/agent-revisions
```

### 12. Dashboard

Dashboard 必须在信息架构上分开：

```text
Conversations     普通对话和未绑定 Trace
Long Tasks        显式创建的 Task
Knowledge         Wiki 与知识图谱
Evaluations       Task Self-Evolve 评测
```

创建 Task 使用独立表单，至少确认：名称、Goal、验收条件、workspace、是否启用 self-evolve。普通聊天输入框不再隐式创建 Task。

Task 详情页：

```text
Overview | State | Executions | Knowledge | Skills | Agent | Evidence | Evolution
```

- Overview：Goal、Contract、当前状态和下一步；
- State：版本化 completed/incomplete/blockers/risks/untrusted；
- Executions：多次执行及其 Attempt/Harness；
- Knowledge：Wiki 注入和 Task-local ChangeSet；
- Skills：绑定版本、使用次数和 Candidate；
- Agent：当前 `agent.md`、diff 和发布历史；
- Evidence：Trace/Span 下钻；
- Evolution：Knowledge → Skill → Agent 的证据漏斗。

## 示例

“怎样给一张照片添加背景模糊”通常是普通对话，不应自动创建 Task。

参考 OpenEvolve 官方 [`background_blur`](https://github.com/algorithmicsuperintelligence/openevolve/tree/main/examples/background_blur)，用户可以显式创建“持续优化视频人像背景虚化热点函数”Task：

- Goal：从正确但缓慢的 `O(k²)` 二维高斯卷积开始，在画质硬门禁内最大化速度；
- Contract：mean SSIM ≥ 0.98、worst-frame ≥ 0.95、worst-region ≥ 0.90，任一失败得 `0`；
- State：当前最佳候选、轮次、MAP-Elites 单元、未解决失败、质量/速度和下一步；
- Knowledge：stale background 作弊模式、局部损伤风险、有效优化和计时陷阱；
- Skills：经多次 Execution 验证的 cascade evaluation、对抗测试和交错计时；
- `agent.md`：稳定的质量优先、证据更新和失败处理规则，不保存具体候选或指标；
- Execution：每轮固定 State/Knowledge/Skill/Agent revision，保存候选 diff、三级评估、artifacts 和 Trace；
- Self-evolve：先记录失败/成功 Knowledge，再沉淀验证过的 Skill，最后才处理残余 `agent.md` 行为问题。

详细过程见 [`self-evolution-background-blur-task.md`](../self-evolution-background-blur-task.md)。

## 验收

1. 普通对话产生 Trace，但数据库中不创建 Task；
2. 只有显式 Dashboard/CLI/API 操作创建 Task；
3. TaskExecution 固定引用 State、Agent、Knowledge 和 Skill revision；
4. handoff 创建新 Attempt，但仍属于同一 Execution 和 Task；
5. 未审计 Executor 声明不能进入可信 State；
6. State 的 incomplete/blockers/untrusted 跨 Execution 保留；
7. 无 `task_id` 的记录不能进入 self-evolve；
8. 首次 Execution 只能积累 Knowledge，不直接产生 production Skill 或 Agent revision；
9. 多次可信 Execution 后才允许提出 Skill Candidate；
10. `agent.md` Candidate 不得包含任务事实，并保持人工发布。

## 后果

正面：Task 心智清晰；普通聊天不会污染 Wiki 或 Self-Evolve；长期目标能跨 Harness、跨 Execution 延续可信状态；演进层次可归因。

负面：现有 TaskRun 需要兼容迁移；Dashboard 必须增加显式创建流程；状态审计比对话摘要更严格。

已知边界：首版只定义状态语义和持久化，不要求复制 LongHorizon-Harness 的完整多角色调度器。

## 关联决策

- ADR-0002：Trace/Span 作为 Execution 证据；
- ADR-0003：Context Pack、Attempt 和 Profile Revision；
- ADR-0004：只有显式 Task 才自动形成 Wiki ChangeSet 和知识图谱演进证据。
