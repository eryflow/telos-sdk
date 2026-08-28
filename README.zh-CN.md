<div align="center">

<img src="assets/logo.svg" alt="TELOS — 私有 Agent 上下文" width="460"/>

### 上下文归你，经验留下，Agent 持续变好

**将长任务上下文私有化，把真实生产轨迹变成可回放样本与离线进化证据。**

<sub>💰 Token 账单最高节省约 90% &nbsp;·&nbsp; 🔒 本地优先 &nbsp;·&nbsp; 🔌 Harness 无关 &nbsp;·&nbsp; ⏪ 可回放 &nbsp;·&nbsp; 🧬 为持续进化而生</sub>

<br/>

[![Core](https://img.shields.io/badge/core-Apache%202.0-2C5F66?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-4FB3BF?style=flat-square)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-Beta-d8851f?style=flat-square)](CHANGELOG.md)
[![Version](https://img.shields.io/badge/version-0.1.8-4FB3BF?style=flat-square)](CHANGELOG.md)

[**快速开始**](#quickstart) &nbsp;·&nbsp; [**工作原理**](#how-it-works) &nbsp;·&nbsp; [**节省效果**](#cost-optimization) &nbsp;·&nbsp; [**当前能力**](#what-ships-today) &nbsp;·&nbsp; [**设计决策**](docs/adr/0002-opik-style-agent-tracing-platform.md)

[📖 English](README.md) &nbsp;|&nbsp; **🇨🇳 简体中文**

</div>

---

## 从 Agent 网关到 Agent 学习系统

Agent 已经可以连续工作数小时，但它的运行上下文仍寄存在某个 Harness 中，散落在不透明的日志里，并随任务结束而丢失。同一种失败会反复修复，真实生产经验很少能沉淀为回归样本、离线评测或训练数据。

TELOS 是位于 Agent Harness 与模型之间的本地控制面。它的目标是让长任务上下文：

- **私有**：持久化在你控制的本地环境中，没有 TELOS 云端遥测。
- **可迁移**：不被某个 Harness 或模型供应商绑定。
- **可回放**：真实生产任务可以转化为可复现样本。
- **可进化**：结果证据可以驱动 Prompt、Tool、Workflow 和模型路由的离线评测。

Prompt Cache 优化仍然是 TELOS 的底层能力，但它现在服务于更完整的目标：**留下上下文，从工作中学习，让下一次执行更好。**

<a id="how-it-works"></a>

## 工作原理

```mermaid
flowchart LR
    H["Agent Harness<br/>Codex · DeepSeek Harness · 其他"] --> G["本地 TELOS Gateway"]
    G --> M["你的模型供应商"]
    H -->|"原生 Hook / Telemetry"| T["Thread → Trace → Span"]
    G -->|"模型 Span"| T
    T --> D[("本地 SQLite")]
    T --> R["回放与回归样本"]
    R --> E["自动离线评测"]
    E --> O["Prompt · Tool · Workflow · 模型路由候选"]
    O -. "人工发布" .-> H
```

**Tracing 数据库**是默认的唯一记录：它把 Harness 与模型生命周期保存为可查询的 `Thread → Trace → Span` 树，其中包含可回放的原始 LLM 请求、工具、子 Agent、审批、usage、TTFT 与错误。旧 JSONL corpus 仅作为显式 `--record-corpus` 兼容选项保留。

<a id="what-ships-today"></a>

## 当前能力

| 能力 | 状态 |
|---|---|
| 为 Codex、Claude Code、OpenClaw 和 Hermes 注入本地 Gateway | **已可用** |
| 从 SQLite LLM Span 跨模式回放；兼容可选旧 corpus | **已可用** |
| SQLite Trace/Span 存储、带鉴权的 batch ingest 与本地 Trace Explorer | **已可用** |
| Codex 原生 Hook 与 DeepSeek Harness telemetry adapter | **已可用** |
| `telos evolve --task` 任务类型策略，默认离线评测、人工发布 | **已可用** |
| 更多 Harness 的原生 tracing adapter | **下一阶段** |
| 自动结果标注、失败归因、回归集生成和候选方案评测 | **下一阶段** |
| SFT/RL 数据集导出 | **规划中** |

当前 `evolve` 命令只负责配置进化策略，尚不会启动评测 Worker，也不会自动修改生产 Agent 行为。

<a id="quickstart"></a>

## 快速开始

```bash
# 安装（Linux / macOS / WSL2 / Android Termux）
curl -fsSL https://raw.githubusercontent.com/learningCatHD/telos-sdk/main/scripts/install.sh | bash
# 或：uv pip install -U telos-sdk

# 接管一个 Harness。之后新启动的 Codex 进程会经过本地 Gateway。
telos init --harness codex

# 为某类任务开启离线进化策略。
telos evolve --task "代码缺陷修复"

# 查看 Harness 注入、Gateway 和流量转发状态。
telos status
```

执行 `telos init` 后请启动一个新的 Harness 进程；已经运行的进程不会追溯加载新的模型供应商配置。

查看流量和成本节省：

```bash
telos dashboard
```

## 默认私有、随时迁移

TELOS 将状态持久化到 `~/.telos/`：

| 路径 | 内容 |
|---|---|
| `config.json` | Gateway、Harness Trace 和进化策略；包含各 Harness 的 tracing token |
| `corpus/` | 可选旧版原始请求 corpus（仅 `--record-corpus`） |
| `telos.db` | SQLite projects、threads、traces、spans 与 feedback |
| `usage.jsonl` | Token 用量与成本指标 |

这些文件可能包含 Prompt、源代码、工具结果，以及模型请求中出现的凭据，应按敏感数据保护。TELOS 不会把它们上传到 TELOS 服务，但你配置的模型供应商仍会收到完成推理所需的请求。

迁移时，停止 Gateway，安全复制完整的 `~/.telos/` 目录，再在目标设备启动 TELOS 即可；不依赖托管控制面或专有数据库。

## Trace 数据模型

TELOS 在不同 Harness 间使用同一套最小层级：

```text
Project  →  Thread  →  Trace  →  Span
```

- **Project**：本地逻辑分组。
- **Thread**：一次 Harness Session 或对话。
- **Trace**：一次用户 turn 或 Agent attempt。
- **Span**：一次 Agent、LLM、Tool、Approval 或 Compaction 操作。

安装 adapter 后，即可在本地查看统一 Trace 树：

```bash
telos init --harness codex
telos init --harness kimi-code
telos init --harness deepseek-harness --replace-telemetry-backend
# http://127.0.0.1:7171/__telos/traces
```

如果 `PATH` 中优先出现的是另一个同名 `dsh`，请显式指定真正的 DeepSeek Harness CLI。
对于已经构建的源码 checkout：

```bash
telos init --harness deepseek-harness --replace-telemetry-backend \
  --dsh-executable /path/to/deepseek-harness/apps/cli/lib/bin.js
```

Adapter 生成稳定 ID 并提交幂等实体快照；Gateway 是唯一的 SQLite writer。Codex 安装优先使用原生 plugin manager，旧客户端才回退到不破坏用户配置的 `hooks.json` 合并。

Kimi Code 适配会追加 fail-open 生命周期 hooks，且不修改托管 OAuth provider。API Key provider 还会经 Gateway 记录模型 span。卸载只移除 TELOS hooks，并恢复曾被代理的 provider URL。

## 自我进化契约

目标闭环默认离线运行，并以证据为门槛：

```text
生产 Trace
  → 结果标签与失败归因
  → 冻结的回归样本
  → 单变量候选改动
  → 配对离线评测
  → 优化建议
  → 人工发布
```

每个候选方案只改变 Prompt、Tool 策略、Workflow 或模型路由中的一项，避免多个变量同时变化而无法归因。私有评测标准不会进入候选生成上下文；通过评测的候选只会被推荐，不会静默发布到生产环境。

Tracing 的完整取舍、替代方案与阶段边界见 [ADR-0002：基于 Trace/Span 的 Agent Tracing 平台](docs/adr/0002-opik-style-agent-tracing-platform.md)。

<a id="cost-optimization"></a>

## 上下文与成本优化仍然存在

Prompt Cache 优化仍然是 TELOS 的底层能力，无需重写或压缩 Prompt。TELOS 会把 Agent 上下文规范化为稳定 IR，并为模型供应商的 KV Cache 复用进行排列。

在既有的一次真实 OpenClaw 六轮实测中：

| 模式 | Raw input tokens | Cache read | 六轮总成本 |
|---|---:|---:|---:|
| Passthrough | 24,151 | 0 | **$0.3623** |
| TELOS | 0 | 18,701 | **$0.0281（−92.3%）** |

SWE-bench Verified 测得 new input `−52.8%`、端到端成本 `−40.5%`。A/B 解决率对比未发现统计显著回归（McNemar `p = 0.66`）。实际节省取决于工作负载和模型供应商的缓存计价；可用 `telos dashboard` 查看自身流量的绝对成本。

详见[协议](https://docs.telosai.pro/zh/concepts/protocol)、[Benchmark 方法](https://docs.telosai.pro/zh/benchmark/swebench)和[支持矩阵](https://docs.telosai.pro/zh/reference/support-matrix)。

## 文档

| 主题 | 参考 |
|---|---|
| 本地 Trace 与进化决策 | [ADR-0001](docs/adr/0001-local-trace-and-task-type-evolution.md) |
| 当前实现架构 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 安装与集成 | [docs.telosai.pro](https://docs.telosai.pro/zh/start/installation) |
| 版本历史 | [CHANGELOG.md](CHANGELOG.md) |

## 参与贡献与许可证

欢迎提交 Issue 和 Pull Request。运行测试：

```bash
pytest
```

TELOS Core 使用 [Apache 2.0](LICENSE) 许可证。

## Citation

```bibtex
@misc{wang2026telos-agent,
  title        = {Telos: A Cost-Aware Inference Infrastructure for AI Agent},
  author       = {Zheng Wang, Shenzhi Wang, HongTao Zhong, Shiji Song, Gao Huang},
  howpublished = {\url{https://github.com/learningCatHD/telos-sdk.git}},
  year         = {2026}
}
```

<div align="center">

**留下上下文，从工作中学习，让下一次执行更好。**

</div>
