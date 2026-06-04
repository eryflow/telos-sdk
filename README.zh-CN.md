<div align="center">

<img src="assets/logo.svg" alt="TELOS — 可移植 Agent 上下文" width="460"/>

### 上下文归你所有 · Agent 是雇来的

**无需重写。无需压缩。可节省 90% token 账单。**

<sub>💰 **token 账单 −50–90%** &nbsp;·&nbsp; 🎯 **agent 行为不变** &nbsp;·&nbsp; ⚡ **更快，不更慢** &nbsp;·&nbsp; 🔒 **不捕获任何内容** &nbsp;—&nbsp; [详情 ↓](#guarantees)</sub>

<sub>一份唯一 IR——tools、system、turns 与 memory——可在 Anthropic · OpenAI · DeepSeek · vLLM · SGLang 上不加修改地运行<br/>真实 6 轮会话节省 -92.3% · 成本按绝对 $/已解决请求 记录——比例可以造，美元不行</sub>

<sub>清华大学 LEAP Lab —— 聚焦机器学习、多模态学习与具身智能的研究团队 · <a href="https://www.leaplab.ai/">leaplab.ai</a></sub>

<br/>

[![Core](https://img.shields.io/badge/core-Apache%202.0-2C5F66?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-4FB3BF?style=flat-square)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-Beta-d8851f?style=flat-square)](CHANGELOG.md)
[![Protocol](https://img.shields.io/badge/protocol-TELOS%20IR-7FD8E0?style=flat-square)](docs/2026-05-06-telos-protocol.md)

[**快速开始**](#quickstart) · [**四个承诺**](#guarantees) · [**支持矩阵**](#support-matrix) · [**为什么**](#why-telos) · [**Benchmark**](#benchmark) · [**协议**](#protocol) · [**路线图**](#roadmap) · [**引用**](#citation)

[📖 English](README.md) &nbsp;|&nbsp; **🇨🇳 简体中文**

</div>

* * *

**最新动态** 🔥

* **[2026.05.31]** 与 [cc-switch](https://github.com/farion1231/cc-switch) 共存 —— TELOS 把网关挡在 cc-switch 选定的上游中转前面，不会有任何密钥被写入 TELOS 配置。
* **[2026.05.29]** `telos init` 现在会在注册新的 harness 上游时自动重启网关，省去手动重启那一步。
* **[2026.05.27]** Codex.app（ChatGPT 登录模式）成为一等 harness；安装器自动检测 `auth_mode` 并路由到正确的上游。

* * *

---

<a id="problem"></a>

## ⬢ &nbsp;凌晨 2 点：钱到底花到哪里去了？

凌晨 2 点，agent 还在跑。右下角计数器跳到 2,847,103，你换算成美元后心里一沉。更糟的是，上面一行写着 `cache_read: 0`。一整夜里，每一轮都把同一段 4,000-token 的 system prompt 从头喂给模型，按全价计费。

把同一段真实 **6 轮** 会话丢进 openclaw，只改两个开关：

| 模式 | raw input tokens | cache_read | 6 轮总成本 |
|---|:--:|:--:|:--:|
| passthrough（今天的默认） | 24,151 | 0 | **$0.3623** |
| 使用 TELOS | 0 | 18,701 | **$0.0281（-92.3%）** |

放大到 1,000 个会话：**$362 → $26** —— 每个月都能看见的真实服务器账单，再乘上团队规模。

**不要再用“token 少了几倍”来衡量。** 到 2026 年，同一模型家族不同计费层级之间的价格差已经达到 **80x–150x**。任何人都能把最便宜的层级塞进分母来造出漂亮比例，只有绝对美元不会说谎。

<p align="center">
  <img src="assets/01-waste.en.svg" alt="今天的 agent token 效率只有 25%" width="100%"/>
</p>

<a id="quickstart"></a>

## ⬢ &nbsp;3 步启动

#### ❶ &nbsp;安装

```bash
# 方式 A —— 一行安装脚本（Linux / macOS / WSL2 / Android Termux）
curl -fsSL https://raw.githubusercontent.com/learningCatHD/telos-sdk/main/scripts/install.sh | bash

# 方式 B —— pip（推荐用 uv 作为包管理器）
uv pip install -U telos-sdk
```

#### ❷ &nbsp;连接

```bash
telos init
```

自动检测本机的 **claude-code / codex / openclaw / hermes**，把配置注入对应工具，并在后台启动本地 gateway（状态写入 `~/.telos/gateway.json`）。不需要改 agent 代码。

#### ❸ &nbsp;观察

```bash
telos dashboard
```

会在浏览器中打开一个离线 HTML 看板，以绝对美元展示每次调用的节省。每次调用都会自动追加到 `~/.telos/usage.jsonl`，并实时汇总。

<p align="center">
  <img src="assets/05-dashboard.png" alt="TELOS savings dashboard — absolute dollars broken down by harness / model / session" width="100%"/>
</p>

<p align="center"><sub><strong>每一笔节省都固定到绝对美元</strong> · 无需云服务 · 支持离线打开 · <code>~/.telos/usage.jsonl</code> 直接驱动单文件 HTML 页面</sub></p>

**TELOS 是开源的。把它接到你的真实工作流里，看看那 92% 到底是真收益，还是又一个“X 倍 token”说法。**

---

<a id="guarantees"></a>

## ⬢ &nbsp;你真正关心的四件事

省钱只是标题。TELOS 敢挂在生产流量上，靠的是下面另外三行——它改变的是**你被计费的内容**，而不是**你的 agent 做什么**。

| 你关心的 | 承诺 | 为什么成立 |
|---|---|---|
| 💰 **Token 账单** | **计费输入 token 降低 50%–90%。** 6 轮真实会话 **−92.3%**；SWE-bench Verified **new_input −52.8% / 端到端成本 −40.5%**。 | 共享前缀由缓存（`cache_read`）命中，而不是每轮按全价重新计费。 |
| 🎯 **Agent 行为** | **完全不变。** 同一个模型、同样的 prompt 语义、同样的输出。TELOS 只重排 proxy→上游这一段，**agent 本地的上下文原封不动**。 | SWE-bench Verified A/B：McNemar **p = 0.66**，解决率无回归。DROP 只剥离时间戳、CWD、PID 等永远不影响答案的易变噪声。 |
| ⚡ **推理速度** | **不会更慢，只会更快。** 缓存命中可跳过对已提交字节的重新 prefill，而 prefill 是长上下文一轮的主要开销，因此**会话越长，首 token 时延越低**。 | 单调追加 → 前缀永不改变 → 引擎每次请求都能匹配最长公共前缀。会话越长，复用越多，延迟越低。 |
| 🔒 **你的数据** | **不捕获任何具体内容。** prompt 与回复从不被存储或外发。 | 网关跑在 `127.0.0.1`；用量日志只记录 token **计数**与色带结构，从不记录 prompt/回复正文；dashboard 是单个离线 HTML 文件。无云端、无遥测、不把任何密钥复制进 TELOS 配置。 |

---

<a id="support-matrix"></a>

## ⬢ &nbsp;支持矩阵

### Harness 支持

| Harness | 典型用途 | `telos init` 自动接入 | 状态 |
|---|---|:---:|---:|
| Claude Code | Anthropic 原生 coding agent 工作流 | ✅ | 🟢 一等支持 |
| OpenClaw | 开源 agent runtime，集成 TELOS parser | ✅ | 🟢 一等支持 |
| Hermes | 多 agent 编排，子 IR 独立处理 | ✅ | 🟢 一等支持 |
| Codex | OpenAI 风格 coding 工作流，通过本地 gateway 注入 | ✅ | 🟢 已支持 |

### Frontier model 支持

| 模型家族 | 提供方 | 通过 TELOS 引擎适配器 | 说明 |
|---|---|:---:|---|
| Claude（4.x / 4.6+） | Anthropic | ✅ | 显式 breakpoints 和 prewarm 路径 |
| GPT（4+/5.x） | OpenAI | ✅ | 使用 `prompt_cache_key` 路由策略 |
| DeepSeek（V3+） | DeepSeek | ✅ | 字节稳定的确定性前缀行为 |

### Inference framework 支持

| Framework | 部署方式 | 通过 TELOS | 缓存能力 |
|---|---|:---:|---|
| vLLM | 自托管 OpenAI 兼容服务 | ✅ | 显式锚点、prewarm、cache 探测/驱逐、部分 fork-and-replace |
| SGLang | 自托管高吞吐推理服务 | ✅ | 显式锚点、prewarm、cache 探测/驱逐、完整 fork-and-replace |

<sub>还想接别的 harness 或模型后端？TELOS 是 adapter 驱动的：保留同一份 IR，新增 engine / harness 适配器即可，不需要重写 agent 逻辑。</sub>

### 支持 cc-switch

[cc-switch](https://github.com/farion1231/cc-switch) 通过改写 Claude Code / Codex / OpenClaw / Hermes 的本地配置文件来切换 provider —— 改写的正是 TELOS 也在写的那些文件。两者是**可组合而非互斥**的：cc-switch 负责选**哪个上游中转**，TELOS 是一个挡在**任意上游前面**的省 token 网关：

```
Claude Code ──▶ TELOS 网关 (127.0.0.1:7171) ──▶ cc-switch 选定的中转
```

执行 `telos init`（或 `telos ccswitch sync`）时，TELOS 会把 cc-switch 当前激活的中转捕获为自己的一个 upstream，并把 harness 重新指向网关路由。中转的鉴权 token 原样保留 —— 它随请求头直接穿过网关，**不会有任何密钥被写入 TELOS 配置**。

```bash
telos ccswitch status   # cc-switch 是否存在、当前激活哪个 provider、TELOS 是否已挡在前面
telos ccswitch sync     # 在 cc-switch 切换 provider 后，重新把 TELOS 挂到前面
```

由于 cc-switch 每次切换都会热改写本地配置，在 cc-switch 里切换 provider 之后请运行 `telos ccswitch sync`（先在 cc-switch 切换，再 sync）。`telos uninstall` 会把 cc-switch 期望的原始中转地址还原回去。

---

<a id="why-telos"></a>

## ⬢ &nbsp;TELOS 只解决两件事

**① 把 token 效率推到极限。** 真实 6 轮会话 **-92.3%**；SWE-bench Verified A/B 在同一正确率区间下端到端成本 **-40.5%**。每一分钱都按绝对 $/已解决请求 核算，比例可以造，美元造不了。

**② 把上下文主权还给你。** `TelosIR` 是引擎无关、可序列化、可移植的上下文表示。你的 persona、你的 tools、你的 20 轮中段线程，全都封装在同一块“石碑”里。今天交给 Claude，明天迁到 DeepSeek，今晚跑在本地 vLLM 上。**上下文归你，agent 只是雇员。**

---

<a id="benchmark"></a>

## ⬢ &nbsp;SWE-bench Verified —— TELOS 不会牺牲任务正确率

token 省下来才有意义，前提是 agent 还能把题做对。我们在 **SWE-bench Verified** 上做了一次预先登记的 A/B：Hermes harness + `deepseek/deepseek-v4-flash`，每臂 100 个实例，种子化抽样覆盖 8 个仓库（sphinx、matplotlib、xarray、pytest、requests、pylint、seaborn、flask）。**每臂 99 个实例进入官方 Docker harness 评测**（1 个实例因上游缺失对应 docker 镜像而排除）。

#### 修复率（docker 评测，n=99/臂，配对）

| Arm | Resolved | 修复率 | 95% Wilson CI |
|---|---:|---:|---|
| **TELOS** | 45 / 99 | **45.5%** | [36.0%, 55.2%] |
| Vanilla | 42 / 99 | 42.4% | [33.2%, 52.3%] |

在同一组 99 个实例上做配对 2×2：两臂都解出 33；仅 TELOS 解出 12；仅 vanilla 解出 9；都没解出 45。精确 McNemar 双侧 **p = 0.66** —— +3 pp 的绝对差异**未达统计显著**，即 TELOS 在该样本量下不会回归修复率。

#### Token 效率（agent 端，n=99/臂，相同实例）

| 每任务 | TELOS | Vanilla | Δ |
|---|---:|---:|---:|
| **new_input**（去缓存后，计费） | 93,712 | 198,706 | **-52.8%** |
| prompt_tokens（raw + cache） | 352,400 | 515,953 | -31.7% |
| output_tokens | 24,975 | 25,218 | -1.0% |
| api_calls | 32.6 | 32.1 | +1.4% |
| **cache_share** | **73.4%** | 61.5% | **+11.9 pp** |
| 上报成本 (USD) | $2.29 | $3.85 | **-40.5%** |

**诚实读这份数据：** 99 个实例的 Wilson CI 宽度约 ±10 pp。本次运行可以在 95% 置信下排除超过约 6 pp 的绝对回归（配对差的下界），但还无法把 Δ 钉到 ±2 pp 以内。能高置信确认的是 —— **在同一修复率区间下，计费输入 token 大约减半，端到端成本下降 ~40%**。路线图上有 n ≥ 400/臂 的复跑计划，用来把修复率的置信区间进一步收窄。

<sub>复现命令：`scripts/run_swebench_batch.py -n 100 --seed 7`。完整技术报告（预先登记设计、统计细节、相关工作）见 [docs/2026-05-26-swebench-ab.md](docs/swebench-ab.md)。</sub>

---

<a id="protocol"></a>

## ⬢ &nbsp;协议：不是压缩，而是永不打断前缀

大多数 agent 框架把 KV cache 当成推理引擎“可能给你，也可能不给你”的运行时礼物。TELOS 把逻辑反过来：

> **缓存复用是 prompt 结构本身的属性，而不是运行时运气。只要你不改动已经提交的字节，缓存就不可能被失效。**

这个原则体现在三个互相配合的想法里。

### 三色带

<p align="center">
  <img src="assets/03-banding.en.svg" alt="PIN / FOLD / DROP bands" width="100%"/>
</p>

每个内容块在“出生”时就声明自己的缓存寿命，不靠事后启发式，不靠 LLM 猜测，而是一级结构注解：

| 带 | 颜色 | 语义 | 缓存行为 |
|---|:---:|---|---|
| **PIN** | 🟢 | 工具定义 · system prompt · 当前问题 | 永久在线。永不驱逐。是每个请求前缀 hash 的不可变底座 |
| **FOLD** | 🟡 | 对话历史 · 工具结果 · 大文档 | 可缓存、可压缩。压力下可被摘要替换，PIN 前缀字节保持不变 |
| **DROP** | 🔴 | 时间戳 · CWD · git status · PID | 瞬时信息。**完全不进入前缀 hash。** 必须放在所有 BP 之后，且不能污染上游字节 |

顺序不变量是绝对的：**PIN* → FOLD* → DROP***。消息内如此，整条 prompt 如此，每一层都如此。这是唯一真正能赢下缓存命中的结构规则，其余都是实现细节。

### 单调追加

prompt 是一条**只追加流**。新轮次只向尾部追加块，不会改写任何已经提交的字节。所谓“修改”通过新块表达（摘要、脱敏），绝不做原地重写。

<p align="center">
  <img src="assets/04-append.en.svg" alt="Monotonic append: cache hit rate is monotonically non-decreasing with session length" width="100%"/>
</p>

因为早期块不可变，而且跨轮字节完全一致，推理引擎的前缀匹配算法在每次请求里都能找到最长公共前缀。这不是运气，而是结构保证。**因此缓存命中率是会话长度的单调不下降函数：会话越长，复用越多，不会回退。**

---

<a id="roadmap"></a>

## ⬢ &nbsp;路线图

TELOS 只做一件事：**上下文是你的，Agent 是雇的。** 当前路线图完全围绕“省钱网关”展开，最后一阶段才埋下 trajectory 作为可移植资产的种子。**能被检查的工程才写进路线图，不能被检查的工程不写。**

| 阶段 | 命题 |
|---|---|
| **Phase 1** · Protocol correctness hardening | 把“缓存不可失效”从口号变成 CI 红绿灯 |
| **Phase 2** · Production reliability & observability | 让 gateway 足够安全，能承接别人的生产流量 |
| **Phase 3** · Take over the call chain | 从 prompt 重写器变成 agent 的流量平面 |
| **Phase 4** · Context becomes an asset | trajectory 不再只是日志，而是可 fork 的代码 |

---

<a id="citation"></a>

## Citation

Core contributors: Zheng Wang, Shenzhi Wang, HongTao Zhong, Shiji Song, Gao Huang

```bibtex
@misc{wang2026telos-agent,
  title        = {Telos: A Cost-Aware Inference Infrastructure for AI Agent},
  author       = {Zheng Wang, Shenzhi Wang, HongTao Zhong, Shiji Song, Gao Huang},
  howpublished = {\url{https://github.com/learningCatHD/telos-sdk.git}},
  year         = {2026}
}
```

---

<div align="center">
<a href="https://github.com/learningCatHD/telos-sdk"><img src="https://img.shields.io/badge/⭐%20Star%20on%20GitHub-learningCatHD%2Ftelos--sdk-1F4A50?style=for-the-badge&logo=github&logoColor=white" alt="Star on GitHub"/></a>