# ADR-0004：以可追溯 Wiki 和知识图谱作为 Self-Evolve 主闭环

## 状态

Proposed

## 上下文

ADR-0003 已建立 TaskRun、Context Pack、Trace、Profile Candidate 和离线评估，但把 self-evolve 的重心放在 Profile 指令优化上。显式定义的长期 Task 在多次 Execution 中更稳定的复利来自知识沉淀：系统应从这些 Execution 中抽取事实、偏好、决策、方法和失败经验，组织成用户可查看、可修订、可复用的 Wiki，并在 Dashboard 上通过知识图谱展示关系和证据。

知识不能等同于自动摘要。未经验证的推断、互相冲突的事实、敏感信息和失去来源的结论都不能静默进入后续任务上下文。

## 决策驱动

- 显式长 Task 在多次 Execution 后能形成可复用知识，而不是只留下 Trace；
- 用户能在 Dashboard 中浏览、搜索、审核、修订和遗忘知识；
- 每条知识能下钻到 TaskExecution、Context Pack、Trace 和 Outcome；
- 图谱与 Wiki 不产生两套互相漂移的事实源；
- 新任务只取与当前目标相关且可信的知识；
- 冲突不能用“最后写入覆盖”解决；
- 首版复用现有 SQLite、loopback API 和静态 Dashboard。

## 决策

Self-evolve 的主循环调整为：

```text
Task evidence
→ WikiChangeSet
→ normalize / deduplicate / conflict check
→ review or policy merge
→ immutable Wiki revision
→ graph projection
→ task-scoped retrieval
→ outcome-based usefulness evidence
```

Profile 优化保留为第二层，用于改进知识抽取、整理和检索策略；它不再是 self-evolve 的唯一产物。

Wiki 是知识事实源。知识图谱由 Wiki Page、原子 Claim、显式 Relation 和 Evidence Link 投影生成，不维护独立事实副本。首版使用 SQLite 邻接表和 FTS5，不引入图数据库或向量数据库。

## 知识模型

| 实体 | 作用 | 关键字段 |
|---|---|---|
| `wiki_spaces` | 隔离用户、项目或生活领域 | `id`、`name`、`scope`、`sensitivity_policy` |
| `wiki_pages` | 稳定主题入口 | `id`、`space_id`、`namespace`、`slug`、`title`、`current_revision_id` |
| `wiki_revisions` | 不可变页面版本 | `id`、`page_id`、`digest`、`summary`、`body`、`state`、`created_at` |
| `knowledge_claims` | 可独立验证的最小知识 | `id`、`revision_id`、`kind`、`text`、`status`、`confidence`、`valid_from/to` |
| `knowledge_relations` | Claim/Page 间的有向关系 | `source_id`、`target_id`、`relation`、`status` |
| `knowledge_sources` | 知识到证据的来源链 | `claim_id`、`task_execution_id`、`pack_id`、`trace_id`、`outcome_id`、`source_digest` |
| `wiki_change_sets` | Agent 提出的原子变更集合 | `id`、`task_execution_id`、`status`、`proposal_json`、`reviewed_at` |
| `knowledge_usages` | 后续执行实际使用记录 | `claim_id`、`task_execution_id`、`context_pack_id`、`retrieval_reason`、`outcome_id` |

Claim 类型首版固定为：

```text
fact | preference | decision | procedure | failure-pattern | asset | glossary
```

关系类型首版固定为：

```text
is-a | part-of | belongs-to | depends-on | applies-to |
contradicts | supersedes | derived-from | used-by | similar-to
```

不允许模型创建任意关系名。新增类型必须通过 schema migration，避免图谱逐渐退化为不可查询的自然语言标签集合。

## 分类结构

Dashboard 使用稳定 namespace，而不是让模型自由创建无限目录：

```text
people/       人物、团队和偏好
projects/     项目结构、约束和术语
domains/      领域事实与词汇
playbooks/    可复用流程和操作方法
decisions/    决策、理由和适用范围
lessons/      失败模式、排障经验和反例
assets/       已有资源、环境和能力
```

Page 可以有标签和别名，但只属于一个主 namespace。跨分类通过 Relation 表达，不复制页面。

## 抽取、合并与冲突

1. 只有显式 Task 下的 TaskExecution 完成或创建可信 checkpoint 后才能进入自动抽取队列；没有 Outcome 的内容只能形成 `proposed` Claim，不能自动成为经验或失败结论；
2. Extractor 只读取已归属的 Context Pack、Trace 摘要、用户反馈和结果；
3. 输出结构化 `WikiChangeSet`，不能直接写 Page；
4. 校验器检查 schema、来源、敏感信息和 confirmed/inferred 区分；
5. 使用 slug、别名和 FTS5 找到候选页面，做精确去重；
6. 同一 subject/relation/scope 出现不兼容值时创建 `contradicts` 关系；
7. approved change set 生成新的不可变 Wiki Revision，并更新 Page pointer；
8. rejected proposal 保留原因，但不进入检索索引。

Claim 状态：

```text
proposed → verified → superseded
        ↘ contested → retracted
```

- 用户明确陈述且有来源的事实可以标记 `verified`；
- 模型归纳默认是 `proposed`；
- 健康、财务、身份凭证和其他高风险知识必须人工确认；
- 新证据不能原地覆盖旧 Claim，只能建立 `supersedes` 或 `contradicts`；
- Outcome 只能影响“有用程度”，不能把一个事实自动改成真或假。

## Dashboard 信息架构

主导航增加一个“知识库”入口，内部使用三个视图：

### Wiki

- 左侧为 namespace 和 space；
- 中间为搜索结果或页面正文；
- 页面显示摘要、原子 Claim、关系、版本和敏感级别；
- 每个 Claim 都有“查看来源”，可下钻到 TaskExecution、Pack 和 Trace；
- 页面可以执行修订、合并、标记过期和遗忘。

### 知识图谱

- 默认展示 Page 级节点，不直接铺开所有 Claim；
- 默认只显示选中节点的一跳邻居，防止全局 hairball；
- Overview 模式按 namespace 聚合成簇；
- 点击节点在右侧显示摘要、Claim、来源和最近使用记录；
- 点击边显示关系类型、支持它的 Claim 和 Evidence；
- `TaskExecution / Pack / Trace` 证据节点默认隐藏，通过“显示证据”开关展开；
- `contradicts`、`supersedes` 使用不同线型和文本标签，含义不只依赖颜色；
- 高敏节点只显示模糊标签，显式解锁后才显示内容。

### 待审核

- 按“新增、更新、冲突、敏感”分组显示 ChangeSet；
- 审核界面并列展示现有 Claim、建议变更和来源证据；
- 支持逐条接受，而不是只能整批接受；
- 拒绝必须记录原因，供后续 Extractor 评估使用。

图谱响应有硬上限，默认 100 个节点、200 条边、深度 1，最大深度 2。服务端返回 `truncated`，前端不自行请求整个数据库。

## 检索与任务注入

新的 TaskExecution 根据 Goal、Contract、当前 State、workspace 和识别出的实体生成检索查询：

1. FTS5 找主题和别名；
2. 沿 `applies-to`、`depends-on`、`belongs-to` 扩一跳；
3. 按 scope 匹配、verified 状态、新鲜度和历史有效使用排序；
4. 排除 contested、retracted、无来源和未获授权的敏感 Claim；
5. 在预算内写入 Context Pack 的 `knowledge_refs.json`；
6. 保存 `knowledge_usage` 和“为什么包含”，便于事后归因。

检索不会复制或修改 Wiki 内容。Context Pack 记录 Page Revision 和 Claim digest，保证之后可以重放当时实际使用的知识。

## 本地 API

```text
GET  /api/v1/wiki/pages
GET  /api/v1/wiki/pages/{id}
GET  /api/v1/wiki/pages/{id}/revisions
GET  /api/v1/wiki/graph?root={id}&depth=1
GET  /api/v1/wiki/change-sets?status=proposed
POST /api/v1/wiki/extractions                 # task_execution_id
POST /api/v1/wiki/change-sets/{id}/approve
POST /api/v1/wiki/change-sets/{id}/reject
POST /api/v1/wiki/claims/{id}/retract
POST /api/v1/wiki/pages/{id}/forget
```

读取继续限制为 loopback；写操作使用现有 control write token。列表 API 不返回原始 Trace 内容，只有明确打开 Evidence 时才读取。

图谱 payload：

```json
{
  "nodes": [
    {"id": "page-1", "type": "page", "label": "stale background", "namespace": "lessons", "status": "verified"}
  ],
  "edges": [
    {"id": "rel-1", "source": "page-1", "target": "page-2", "relation": "applies-to", "source_count": 2}
  ],
  "truncated": false
}
```

## 质量门与可观测性

知识闭环需要独立于 Profile 分数的指标：

- provenance coverage：每个 verified Claim 是否有可读来源；
- conflict rate：合并前发现冲突的比例，不追求越低越好；
- unsupported-claim rate：无证据 Claim 的比例；
- retrieval precision：注入的知识是否与任务相关；
- useful citation rate：被使用的 Claim 是否与成功 Outcome 相关；
- stale-use rate：过期或 contested Claim 是否仍被注入；
- sensitive-leak gate：未授权敏感 Claim 被注入时直接失败。

Extractor/Retriever Profile 的 Candidate 必须在冻结任务上评估这些指标，仍遵守 strict improvement 和 manual promotion。

## 不采用的方案

### 直接把任务总结写成 Markdown

简单，但无法原子冲突检测、来源归因和精确检索，不采用。

### Wiki 与图数据库双写

读图方便，但会产生双真相和恢复复杂度，不采用。

### 首版引入 Neo4j 或向量数据库

当前数据规模和查询深度不需要额外服务。只有 SQLite 邻接查询或 FTS5 在基准中达不到目标时再评估。

### Agent 自动覆盖旧知识

无法解释历史变化，也容易被错误任务污染，不采用。

## 分阶段交付

### P0：可信 Wiki

- schema、ChangeSet、抽取、审核、版本和来源下钻；
- Dashboard Wiki 与待审核页面；
- 手工触发 `task_execution_id` 抽取。

### P1：知识图谱

- Page/Relation 投影和邻域 API；
- Dashboard 图谱、节点详情、关系详情和冲突显示；
- 节点/边数量上限与敏感内容遮罩。

### P2：任务检索

- FTS5 + 一跳关系检索；
- `knowledge_refs.json` 注入 Context Pack；
- usage 和 retrieval reason 追踪。

### P3：知识策略自进化

- 冻结抽取/检索评测集；
- 优化 Extractor/Retriever Profile；
- 根据 Outcome 评估知识贡献，但不自动改变 Claim 真值。

## 端到端验收

1. 参考 OpenEvolve 官方 [`mlx_metal_kernel_opt`](https://github.com/algorithmicsuperintelligence/openevolve/tree/main/examples/mlx_metal_kernel_opt)，显式创建“持续优化 Qwen3 GQA Metal kernel”Task，首次 Execution 后生成待审核 ChangeSet；
2. Wiki 出现“32% 候选在 bf16 编译失败”“短上下文加速会掩盖长上下文回退”“direct speedup 比 combined_score 更可解释”等 Claim，每条可下钻到 kernel diff、artifact 和 Trace；
3. 图谱展示 Metal 优化策略、正确性门禁、benchmark 回退、性能瓶颈和证据的关系；
4. 新候选声称已经超过基线、但只报告短上下文时，不覆盖已有长上下文回退，而是建立冲突并进入审核；
5. 新的 kernel Execution 检索到 subprocess hook、bf16 correctness、统计 timing 和 profiling 知识，但不注入无关 Task 的知识；
6. Context Pack 固定记录实际使用的 Claim revision/digest；
7. 用户能从图谱打开 Wiki、查看证据、修订或遗忘知识；
8. 未审核敏感知识、contested Claim 和无来源 Claim 不进入任务上下文。

## 后果

正面：Self-evolve 从低频 Profile 调参转为高频知识复利；用户能看到系统“学会了什么”，并能纠错和追责。

负面：需要审核队列、冲突模型和敏感信息策略；知识抽取带来额外本地计算和存储。

已知边界：首版只支持两跳以内的局部图谱和文本检索，不承诺全局语义搜索或自动本体构建。

## 关联决策

- ADR-0002：Trace/Span 是知识来源证据层；
- ADR-0003：Context Pack、Profile Revision、离线评估和人工发布继续作为知识复用与策略进化的基础；
- ADR-0005：只有显式定义的长 Task 及其 Execution 才进入自动知识和 Self-Evolve 闭环。
