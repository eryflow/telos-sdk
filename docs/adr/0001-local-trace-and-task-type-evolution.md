# ADR-0001：本地 Trace 与按任务类型持续进化

**状态：** Superseded by [ADR-0002](./0002-opik-style-agent-tracing-platform.md)
**日期：** 2026-08-24

## 背景

TELOS 当前是本地模型 Gateway：`telos init --harness <name>` 将 Harness
请求接入 Gateway，Gateway 默认把每轮原始请求写入 `~/.telos/corpus/`，供
`telos replay` 使用。现状有三个缺口：

1. corpus 只有模型请求，没有审批、工具真实结果、工作区变化和任务结果；
2. 配置中没有“哪些 Harness 已开启 Trace”的持久状态；
3. 没有按任务类型开启持续进化的控制面。

同时，README 中“Captures no content”的承诺与默认 corpus 行为冲突。引入完整
Trace 前必须明确：运行数据只在本地持久化，但默认完整记录会包含 Prompt、代码和
工具输出。

## 决策驱动

- `telos init --harness <name>` 后，该 Harness 默认记录本地完整轨迹；
- 数据不上传，并能随本地目录或导出包迁移；
- 不破坏现有 corpus/replay 格式；
- Reporter 故障不能阻断 Harness；
- `telos evolve --task <type>` 按任务类型开启自动离线进化；
- Candidate 可以自动产生和评测，但生产 Revision 必须人工发布；
- 第一阶段必须用标准库和现有 Gateway 完成，不引入消息队列或外部数据库。

## 决策

### 1. 两条互补的本地记录

保留现有 corpus 作为可回放的“模型请求事实源”，新增 append-only Reporter
事件日志补足 Harness 生命周期：

```text
~/.telos/
├── corpus/<session>.jsonl       # 现有：原始模型请求，replay 继续读取
├── traces/<harness>/<session>.jsonl
│                                # 新增：Reporter 事件
└── config.json                  # Harness 注册与 evolution policy
```

第一阶段不把两类文件强行迁移到新格式，避免破坏已有 replay。后续统一 Vault 时，
两者通过 `session_id` 合并为同一个 Attempt 视图。

### 2. 运行模型

内部采用以下层级：

```text
TaskType                         可持续学习的能力，如“代码缺陷修复”
└── TaskRun                      一次具体用户目标
    └── Attempt                  某 Harness + Agent Revision 的一次尝试
        └── Event                请求、工具、审批、结果等追加式事件
```

当前 Gateway 的 `session_id` 暂时作为 Attempt 关联键。显式 TaskRun/Attempt ID
在跨 Harness handoff 阶段加入；本 ADR 不用不可靠的时间启发式提前伪造它们。

### 3. `telos init --harness`

安装 Harness Gateway 配置成功后，TELOS 在 `config.json` 中原子写入：

```json
{
  "trace_harnesses": {
    "codex": {
      "enabled": true,
      "capture": "full",
      "reporter_token": "<local opaque token>"
    }
  }
}
```

重复 init 保留原 token，不产生多个注册。`telos uninstall --harness` 在成功撤销
接入后禁用该注册，但不删除历史 Trace。

“完整 Trace”是能力目标，不是对所有 Harness 的虚假承诺。只有 Gateway 的 Harness
仍只有模型请求；安装了原生 Hook/Reporter 的 Harness 才有审批、工具和结果事件。

### 4. Harness Reporter 协议

Reporter 通过同一 Gateway 的 loopback-only 接口发送事件：

```text
POST /__telos/reporter/events
```

请求级字段：

| 字段 | 规则 |
|---|---|
| `harness` | 必须是已 init 且 enabled 的 Harness |
| `reporter_token` | 与本地注册 token 常量时间比较 |
| `session_id` | 与 Gateway corpus 使用同一 Session 标识 |
| `events` | 一到多个事件，单次请求有大小上限 |

事件字段：

| 字段 | 规则 |
|---|---|
| `event_id` | Reporter 生成；用于幂等去重 |
| `kind` | 固定事件集合 |
| `observed_at` | 可选 ISO-8601 时间；落盘时间由 Gateway 补充 |
| `data` | JSON 对象；不得包含凭证和完整环境变量 |

第一阶段允许的事件：

```text
attempt.started
approval.decided
tool.finished
workspace.changed
artifact.created
user.feedback
attempt.finished
```

Gateway 在每个 Session 文件内分配单调递增 `seq`。重复 `event_id` 返回原 `seq`
而不重复写入。Reporter 写入失败只影响可观测性，不影响模型请求。

### 5. `telos evolve --task`

命令中的 `task` 表示 TaskType，不表示某一次运行：

```bash
telos evolve --task "代码缺陷修复"
```

它原子写入一个幂等 policy：

```json
{
  "evolution_tasks": {
    "代码缺陷修复": {
      "enabled": true,
      "evaluation": "offline",
      "promotion": "manual"
    }
  }
}
```

第一阶段只交付可验证的配置控制面和 Reporter 数据入口。只有当 Benchmark、
Candidate Revision 和 evaluator worker 都可运行时，CLI 才能声称完成一次进化；
在此之前不得伪造 Candidate、分数或“自动优化成功”。

### 6. 后续自动进化闭环

每个 TaskType 拥有独立的 Telos `AgentProfile`，不直接原地修改 Harness 的
`AGENTS.md`：

```text
生产 Trace
→ 结果标签与失败归因
→ 冻结 RegressionCase
→ 单一维度 CandidateRevision
→ Reference/Candidate 隔离重跑
→ 确定性评分优先，LLM Judge 补充
→ 通过质量门后标记 recommended
→ 人工 promote 到 production
```

Optimizer 只能看到公开任务、分数和 Trace；私有 Rubric/Gold 只对 Evaluator
可见。Candidate 评测不完整、运行时不一致或受保护用例回归时一律拒绝。

## 安全与隐私

- Gateway 和 Reporter 接口默认只监听 loopback；
- 每个 Harness 使用独立随机 token，配置文件不在 CLI 输出中显示 token；
- Reporter 不接收完整环境变量；单次 payload 有硬上限；
- `~/.telos` 是唯一默认持久化根目录，不存在后台上传；
- “本地持久化”不等于“模型处理零出网”：远程模型评测仍会把评测输入发给用户
  配置的 Provider；严格零出网需要本地模型；
- uninstall 只停止新记录，不删除历史；删除历史必须是单独显式操作。

## 被否决的方案

### 用 SQLite 保存完整 Trace

否决。文件更容易检查、恢复和迁移；SQLite 以后只作为可重建索引和本地任务队列。

### 立即替换 corpus 格式

否决。它会扩大变更面并破坏已有 replay。第一阶段用 `session_id` 关联两条日志。

### 让 Agent 直接修改生产 Harness 配置

否决。无法可靠回滚，也会把 Harness 私有格式变成进化协议。所有修改先进入不可变
CandidateRevision。

### 自动发布通过评测的 Candidate

否决。离线评测自动，生产发布人工批准。

## 分阶段实现与验收

### 阶段 A：配置与 Reporter 纵向切片

- init 注册 Harness Trace；
- Reporter endpoint 鉴权、校验、去重并追加 JSONL；
- `telos evolve --task` 保存幂等 policy；
- 单元测试覆盖旧配置兼容、重复 init、非法 token、重复事件和 CLI。

### 阶段 B：任务标签与结果

- Reporter Hook adapters；
- TaskType 显式标签与低置信度隔离；
- Outcome Resolver 和失败归因；
- corpus + Reporter Trace 的统一只读视图。

### 阶段 C：自动离线评测

- RegressionCase、Reference/Candidate Revision；
- 隔离 Workspace 重跑；
- 自动质量门、recommended 状态和人工 promote。

### 阶段 D：迁移与训练出口

- `.telosbundle` 导入导出及 checksum；
- SFT、Preference、RL 数据导出；
- SQLite 可重建索引和保留策略。

## 后果

正面：CLI 符合用户心智；现有 replay 不受影响；Reporter 是旁路；演进路径可验证。

负面：阶段 A 中 corpus 与 Reporter 事件仍是两个文件；Gateway-only Harness 的任务
结果仍可能是 unknown；完整 self-evolve 要到阶段 C 才闭环。
