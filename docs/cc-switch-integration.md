# TELOS × cc-switch 集成方案

> 本文档详细介绍 TELOS 如何**识别并兼容** [cc-switch](https://github.com/farion1231/cc-switch)
> —— 一个流行的桌面端 provider 管理器，用于在 Claude Code / Codex / OpenClaw / Hermes
> 之间切换服务商。cc-switch 改写的本地配置文件，正是 TELOS 也在写的那几个，因此二者会互相覆盖。
> 本方案的核心结论是：**它们不是竞争关系，而是可组合关系** —— cc-switch 负责选「哪个上游中转」，
> TELOS 是挡在「任意上游前面」的省 token 网关。
>
> - 想看 CLI 速查 → [User-guide.md](User-guide.md)
> - 想看代码架构 → [ARCHITECTURE.md](ARCHITECTURE.md)
> - 想看变更记录 → [../CHANGELOG.md](../CHANGELOG.md)
>
> 最后更新：2026-06-01

---

## 目录

1. [背景：冲突从哪来](#1-背景冲突从哪来)
2. [核心设计：从「互斥」到「串联」](#2-核心设计从互斥到串联)
3. [整体数据流](#3-整体数据流)
4. [识别层：`init/cc_switch.py`](#4-识别层initcc_switchpy)
5. [捕获并串联：各 harness 的处理](#5-捕获并串联各-harness-的处理)
6. [CLI：`telos ccswitch status / sync`](#6-clitelos-ccswitch-status--sync)
7. [一个被顺带修掉的真实 bug：marker 类型漂移](#7-一个被顺带修掉的真实-bugmarker-类型漂移)
8. [鉴权 token 如何安全穿透](#8-鉴权-token-如何安全穿透)
9. [推荐工作流](#9-推荐工作流)
10. [已知边界与权衡](#10-已知边界与权衡)
11. [测试与验证](#11-测试与验证)
12. [设计决策记录](#12-设计决策记录)

---

## 1. 背景：冲突从哪来

cc-switch 是一个跨平台桌面 App，让用户用 GUI 一键在不同服务商之间切换。它的工作方式是
**在每次切换 provider 时，热改写各 harness 的本地"活配置"文件**：

| Harness | cc-switch 改写的活配置文件 | 写入的关键字段 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` → `env` | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`（可选 `ANTHROPIC_MODEL`） |
| Codex | `~/.codex/config.toml`、`auth.json` | `model_provider`、`[model_providers.*].base_url` |
| OpenClaw | `~/.openclaw/openclaw.json` | `providers.<id>.baseUrl` / `apiKey` |
| Hermes | `~/.hermes/config.yaml` | `model.base_url` / `api_key` |

问题在于：**TELOS 写的是同一批字段**。TELOS 的 `telos init` 会把
`ANTHROPIC_BASE_URL` 改成网关地址（`http://127.0.0.1:7171/_h/claude-code`），
而 cc-switch 切换 provider 时会把它覆盖回某个第三方中转地址。两者「谁后写谁赢」，互相踩踏：

- TELOS 注入后，用户在 cc-switch 里切了 provider → TELOS 的网关地址被覆盖，请求绕过网关，省 token 失效。
- cc-switch 设好后，用户跑了 `telos init` → cc-switch 选的中转被覆盖。

> cc-switch 自 v3.8.0 起把 provider 的权威存储迁到了 SQLite（`~/.cc-switch/cc-switch.db`），
> `~/.cc-switch/settings.json` 只保留设备级状态（如 `currentProviderClaude` / `currentProviderCodex`
> 这两个「当前激活 provider 的 id」）。**本方案不读取它的 SQLite**，只读各 harness 的活配置文件
> —— 这样对 cc-switch 的内部 schema 变化免疫。

---

## 2. 核心设计：从「互斥」到「串联」

关键洞察：cc-switch 决定的是「**用哪个上游**」，TELOS 是「**挡在任意上游前面**」的网关。
这两件事是正交的，可以串起来：

```
Claude Code ──▶ TELOS 网关 (127.0.0.1:7171) ──▶ cc-switch 选定的中转
```

更幸运的是，TELOS **本来就具备**实现这条链路所需的全部机制，无需新造轮子：

1. **带标签的 upstream 路由** —— `proxy/server.py` 的 `handle_upstream_route`
   （约 `proxy/server.py:1306`）：当请求打到 `/_h/<harness>/upstreams/<slug>/...` 时，
   网关会把 `self.upstream` 临时切换成该 slug 对应的 `UpstreamConfig.url`，转发，并用
   `via` 给这条流量打上 harness 归属标签。

2. **鉴权头原样转发** —— `_forward_headers`（约 `proxy/server.py:614`，白名单含
   `x-api-key` / `authorization`）：客户端带的鉴权头**逐字转发给上游**，不改写。

3. **已被 openclaw / hermes 复用的「捕获 upstream」范式** —— 这两个安装器早就在做
   「读当前 live baseUrl → 注册成 `via=<harness>` 的 upstream slug → 把活配置指向网关路由」，
   并在卸载时用 `config.revert_upstreams_owned_by(via)` 回滚。

所以「兼容 cc-switch」最终被泛化成一句话：

> **TELOS 捕获每个 harness 活配置里当前生效的那个中转（无论是谁写的，包括 cc-switch 写的），
> 把它注册成自己的一个 upstream 并把网关串到它前面；绝不把某个中转的流量错误地发往默认上游。**

cc-switch 只是这个能力最显眼的触发者。识别 cc-switch 本身，则是上层一个很薄的「检测 + 展示 + reconcile」层。

---

## 3. 整体数据流

以 Claude Code 为例，一次完整的「捕获并串联」：

```
① 用户在 cc-switch 里选了某 provider
   → cc-switch 写 ~/.claude/settings.json:
       env.ANTHROPIC_BASE_URL = https://relay.example/api/v1
       env.ANTHROPIC_AUTH_TOKEN = ccs-secret

② 用户运行 `telos ccswitch sync`（或 `telos init`）
   → ClaudeCodeInstaller.install() 读到一个「非 telos、非官方」的中转地址：
       · 把 https://relay.example/api（去掉尾部 /v1）注册成 upstream:
           slug   = claude-code-upstream
           via    = claude-code
           engine = anthropic / protocol = anthropic-messages
       · 原中转地址存进 env.__telos_previous_base_url（卸载时还原）
       · env.ANTHROPIC_BASE_URL 改写为：
           http://127.0.0.1:7171/_h/claude-code/upstreams/claude-code-upstream
       · ANTHROPIC_AUTH_TOKEN 原样保留 ← 关键

③ Claude Code 发请求
   → 打到 /_h/claude-code/upstreams/claude-code-upstream/v1/messages
   → 网关跑 TELOS 省 token 流水线
   → 转发到 https://relay.example/api/v1/messages
   → 带上 Claude Code 自己发的 ANTHROPIC_AUTH_TOKEN 作为鉴权头
```

第 ③ 步里，token 自始至终都在「请求头」这条路径上，从未进入 TELOS 的任何配置文件。

---

## 4. 识别层：`init/cc_switch.py`

这是方案的「识别」（识别 cc-switch 是否存在、当前激活哪个 provider、TELOS 是否已串好）一半。
全部只读活文件，从不打开 SQLite。

| 函数 | 作用 |
|---|---|
| `ccswitch_home()` | 返回 `~/.cc-switch`（可用环境变量 `CC_SWITCH_HOME` 覆盖，便于测试） |
| `is_installed()` | cc-switch 是否存在（home 目录存在即视为存在） |
| `read_device_settings()` | 解析 `~/.cc-switch/settings.json`，取出 `currentProviderClaude` / `currentProviderCodex`。文件缺失或损坏时返回空字段，**绝不抛异常**（检测不能被对端数据卡死） |
| `classify_harness(name, *, proxy_url)` | 判定某 harness 的 provider 状态 |

`classify_harness` 返回一个 `HarnessState`，`state` 取四个值之一：

| state | 含义 |
|---|---|
| `TELOS_CHAINED` | TELOS 网关已串在前面 |
| `RELAY_ACTIVE` | 当前活配置指向一个第三方中转（如 cc-switch 选的），但 TELOS 还没串上 → 应跑 `sync` |
| `OFFICIAL` | 指向服务商官方 API（`api.anthropic.com` / `api.openai.com` / `chatgpt.com`） |
| `ABSENT` | 该 harness 未配置 / 无活文件 |

判定逻辑：先用各 harness **自己的** `installer.status().already_installed` 来权威判断
「是否已被 TELOS 串上」，再读 live baseUrl 来区分「第三方中转」与「官方端点」。

---

## 5. 捕获并串联：各 harness 的处理

不同 harness 的「缺口」大小不同。下表是现状与本方案补齐的内容：

| Harness | 改动前 | 缺口 / 本方案 |
|---|---|---|
| **claude-code** | 注入固定的 `/_h/claude-code`（永远转发到 `anthropic` 默认上游），任何自定义中转只被存进 `__telos_previous_base_url` 当死数据 | **主缺口**。新增：把中转捕获成 `claude-code-upstream` 并串到它前面 |
| **codex** | API-key 模式固定走 `openai` 上游 | cc-switch 在 `config.toml` 里写的自定义 `base_url` 会被丢弃 → 新增捕获成 `codex-upstream` |
| **openclaw / hermes** | 本就「读 live baseUrl → 注册 upstream slug」 | 基本已天然兼容，只需识别层覆盖 + 幂等复跑 + 补测试 |

### 5.1 Claude Code（`init/claude_code.py`）

`install()` 读到 `env.ANTHROPIC_BASE_URL` 后分三种情况：

- **已是 telos 路由** → 幂等。顺便：把 marker 规整成布尔 `True`（见 §7）、按当前
  `proxy_url` 刷新路由 host、若是 relay 路由则从 `__telos_previous_base_url` 重新
  确保 upstream slug 存在（这样即使 `~/.telos/config.json` 被删也能自愈）。
- **官方 `api.anthropic.com` 或为空** → 沿用旧行为，注入简单路由 `/_h/claude-code`。
- **第三方中转（其它任何值）** → 捕获：用 `re.sub(r"/v\d+$", "", url.rstrip("/"))`
  归一化（与 openclaw 一致），注册 `UpstreamConfig(url=中转, engine="anthropic",
  protocol="anthropic-messages", via="claude-code")` 到 slug `claude-code-upstream`，
  并把 `ANTHROPIC_BASE_URL` 指向 `/_h/claude-code/upstreams/claude-code-upstream`。

`uninstall()` 先调 `revert_upstreams_owned_by("claude-code")` 清掉 slug，再从
`__telos_previous_base_url` 还原原中转地址（cc-switch 期望看到的那个值）。

### 5.2 Codex（`init/codex.py`）

Codex 配置是 TOML，且仓库要兼容 Python 3.10（无 stdlib `tomllib`），所以沿用
codex 安装器既有的「字符串手术」风格，新增一个**尽力而为**的 helper
`_extract_provider_base_url(text, provider)`：用正则从 `[model_providers.<name>]`
表里读出 `base_url`。

API-key 模式下，若上一个 provider 的 `base_url` 是非 OpenAI 的自定义中转，则注册成
`UpstreamConfig(..., engine="openai", protocol="openai-chat", via="codex")` 到 slug
`codex-upstream`，并把 codex 的 `base_url` 指向 `/_h/codex/upstreams/codex-upstream/v1`。
任何解析失败都**回退**到原来的固定 `openai` 上游（无回归）。ChatGPT 登录模式不受影响。

卸载复用既有的 `revert_upstreams_owned_by("codex")`，它会一并清掉
`codex-chatgpt` 与 `codex-upstream`（两者 `via` 都是 `codex`）。

### 5.3 OpenClaw / Hermes

二者的安装器 `_ensure_upstream_slug` 本就会捕获活配置里的 baseUrl，因此 cc-switch
写进去的中转**已经天然被串联**。本方案对它们不改核心，只是让识别层覆盖它们、并补充
「cc-switch 写入中转」场景的测试。

---

## 6. CLI：`telos ccswitch status / sync`

入口在 `cli.py` 的扁平分发里新增一行 `if subcommand == "ccswitch": ...`。

### `telos ccswitch status`

只读、不改任何文件。报告：cc-switch 是否安装、当前激活的 provider id、以及每个 harness 的串联状态。

真实机器上的输出示例：

```text
cc-switch: detected at /Users/george/.cc-switch
  active Claude provider: bfc31921-68d4-4fb6-94c5-87e41f50870e
  active Codex provider:  codex-official

harness chaining state:
  claude-code  telos-chained  telos gateway is chained in front
  codex        telos-chained  telos gateway is chained in front
  openclaw     telos-chained  telos gateway is chained in front
  hermes       telos-chained  telos gateway is chained in front

After switching a provider in cc-switch, run `telos ccswitch sync` to chain telos in front of the new choice.
```

### `telos ccswitch sync`

**按需 reconcile**：对每个检测到的 harness 重跑 `install()`（即「捕获并串联」），若
`~/.telos/config.json` 的 upstream 表发生变化，则重启正在运行的网关让新 slug 生效
（复用 `init/__main__.py` 里的重启逻辑）。

这是处理「cc-switch 每次切换都热改写活配置」的手段 —— 我们选择了**按需 reconcile**
而非常驻文件 watcher（理由见 §12）。

此外，`telos init` 在检测到 cc-switch 时，会在结尾打印一行提示，引导用户在切换 provider 后跑 `sync`。

---

## 7. 一个被顺带修掉的真实 bug：marker 类型漂移

在开发机上（cc-switch 与 TELOS 并存）发现了一个具体 bug：

TELOS 用 `env.__telos_installed` 标记「这是我注入的」，写入的是**布尔** `True`。但
cc-switch 的 "backfill from live"（编辑当前 provider 时从活文件回填）把它**漂移成了
字符串** `"true"`。而 TELOS 旧代码用的是 `env.get(_TELOS_MARK_KEY) is True` —— 对字符串
`"true"` 判定为 `False`，于是：

1. `status` 误报 claude-code「未连接」；
2. 重跑 `install` 会把 TELOS **自己的网关路由**当成「用户原值」存进
   `__telos_previous_base_url`，**摧毁真实中转地址的记录**（正是注释里反复警告的数据丢失场景）。

修复（`init/claude_code.py`）：

- 新增 `_is_marked(env)`，同时接受布尔 `True` 与真值字符串（`"true"` / `"1"` / `"yes"`）。
- 四处判定（install / uninstall / status）统一改用它。
- 每次 install 都把 marker 重写成真正的布尔 `True`，让被漂移的值**自愈**。
- 用 `_is_telos_route(url)`（判断是否含 `/_h/claude-code`）守住「绝不把自己的路由存进
  `__telos_previous_base_url`」—— 即使 marker 丢了也不会污染。

修复生效的证据：上面 §6 的 `status` 输出里，claude-code 正确显示为 `telos-chained`
（修复前会错误地显示「未连接」）。

---

## 8. 鉴权 token 如何安全穿透

这是整个方案最值得强调的安全性质：

- cc-switch 把中转的 token 写进 `ANTHROPIC_AUTH_TOKEN`（Codex/OpenClaw/Hermes 各有对应字段）。
- TELOS **不动这个字段**，只改 `ANTHROPIC_BASE_URL`。
- Claude Code 启动时把 `ANTHROPIC_AUTH_TOKEN` 作为请求鉴权头发出。
- 网关的 `_forward_headers` 把 `x-api-key` / `authorization` **逐字转发**给上游中转。

因此：**token 全程只在「请求头」路径上，从不被写入 `~/.telos/config.json` 或任何 TELOS
状态文件。** `UpstreamConfig` 里也根本没有存凭据的字段（只有 `url / engine / protocol / via`）。

---

## 9. 推荐工作流

由于 cc-switch 每次切换都会热改写活配置，覆盖掉 TELOS 的网关地址，正确顺序是
**先在 cc-switch 切换，再 `sync`**：

```bash
# 1. 首次接入（自动检测所有 harness 并注入网关）
telos init

# 2. 随时查看共存状态
telos ccswitch status

# 3. 每当你在 cc-switch 里换了 provider
telos ccswitch sync        # 把 TELOS 重新串到新选择的前面

# 4. 想彻底退出（还原 cc-switch 期望的原始中转地址）
telos uninstall            # 或 telos uninstall --harness claude-code
```

---

## 10. 已知边界与权衡

- **cc-switch 的 backfill 可能污染它自己的 DB。** 当 TELOS 已串好（活文件里是网关路由）时，
  cc-switch 的「从活文件回填」可能把网关路由当成该 provider 的 baseUrl 写回它自己的 SQLite。
  这是「两个管理器抢同一个字段」的固有问题，无法完全消除（我们不去改 cc-switch 的 DB）。
  缓解：TELOS 卸载时会还原原始中转地址；并在文档里明确「先切换再 sync」的顺序。

- **live-files-only 的盲区。** 当 TELOS 已占据活文件（里面是网关路由）时，「当前中转」
  并不在任何活 JSON 里（只在 cc-switch.db 里）。其持久记录是「已注册的 upstream slug +
  `__telos_previous_base_url`」。`sync` 的设计前提就是「在 cc-switch 切换之后、中转短暂可见时」运行。

- **Codex 捕获是尽力而为。** 基于正则的 TOML 读取，解析不到就回退到固定 `openai` 上游，
  保证无回归；ChatGPT 登录模式完全不变。

---

## 11. 测试与验证

- **新增 `tests/test_cc_switch.py`（10 例）**：检测 / 设备设置解析 / `classify_harness`
  四态 / Codex 自定义中转捕获与回滚 / 官方 OpenAI 不被捕获。
- **`tests/test_init_claude_code.py` 扩充**：字符串 marker 自愈回归、cc-switch 中转
  捕获往返（含 token 保留与卸载还原）；并把 3 个编码了「旧行为」的用例更新到新语义。
- **全量套件 284 passed。**
- **真实机器 `telos ccswitch status`**：正确读出 `bfc31921-…` / `codex-official`，
  并证明 marker 自愈生效。
- **隔离环境端到端 `ccswitch sync`**（用临时 `HOME`）：模拟中转
  `https://relay.demo/api/v1` → 被捕获为 `claude-code-upstream`（`/v1` 已归一化掉）、
  网关路由注入成功、`ANTHROPIC_AUTH_TOKEN` 保留、原中转地址存入 `__telos_previous_base_url`。

所有路径都用 `CC_SWITCH_HOME` / `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `TELOS_HOME`
重定向到临时目录，绝不触碰真实用户文件。

---

## 12. 设计决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| 数据来源 | **只读活文件**，不读 cc-switch 的 SQLite | 松耦合，对 cc-switch 内部 schema 变化（v3.8.0 改过一次）免疫 |
| 冲突处理节奏 | **按需 reconcile**（`init` + `ccswitch sync`），不做常驻 watcher | 简单、可预测、无后台进程与边界态；切换后手动一行命令即可 |
| 覆盖范围 | **claude-code / codex / openclaw / hermes 全部** | 与 cc-switch 管理的范围对齐；其中 claude-code 是主战场，openclaw/hermes 基本已天然兼容 |
| upstream 归属 `via` | 用 **harness 名**（如 `claude-code`），而非 `cc-switch` | dashboard 按 harness 归因流量更准确；卸载用 `revert_upstreams_owned_by(harness)` 统一回滚 |
| token 处理 | **不存储**，靠请求头穿透 | 不让任何密钥落到 TELOS 配置；`UpstreamConfig` 本就无凭据字段 |

---

## 涉及的文件

| 文件 | 改动 |
|---|---|
| `init/claude_code.py` | marker 加固（§7）+ 中转捕获并串联（§5.1） |
| `init/cc_switch.py` | **新增**：检测 / 设备设置 / `classify_harness`（§4） |
| `init/codex.py` | API-key 模式自定义中转捕获（§5.2） |
| `cli.py` | `ccswitch` 子命令分发 + 用法文本（§6） |
| `init/__main__.py` | `telos init` 检测到 cc-switch 时的提示（§6） |
| `tests/test_cc_switch.py`、`tests/test_init_claude_code.py` | 新增 / 扩充测试（§11） |
| `README.md`、`README.zh-CN.md`、`CHANGELOG.md` | 共存说明与变更记录 |
