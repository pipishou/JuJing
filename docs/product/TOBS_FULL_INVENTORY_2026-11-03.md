# Tobs 全量清单与设置说明（2026-11-03 实测）

> 目的：把 Tobs（deepseekbot 1.6.1）的**包结构 / 数据落点 / 全部设置面 / CLI 命令 / 硬约束**一次说清，作为我方挂载层的底座说明书。
> 取证方式：本机 `node_modules` 实测 + `deepseekbot --help` 实测 + 官方文档抓取（tobs.app）。**未做代码级逆向**的部分在下文标 `[未读]`。

---

## 一 它是什么（一句话）

PersonaBot 运行时：给 DSH 装上「有身份、有记忆、能在群聊里干活」的 Bot 层。**不改 DSH 引擎**，靠 `dsh.bundle.patch` 在 profile 启动时插入自己的一套包。

## 二 包结构（谁是真代码）

| 包 | 体积/文件 | 角色 |
| --- | --- | --- |
| `@botharness/cloudflared-win32-x64` | 53MB/4 | 内嵌 cloudflared（远程访问隧道用） |
| `@botharness/im-provider` | 24MB/672 | IM 平台适配（飞书/Slack/QQ/微信） |
| `@botharness/ui` | 21MB/9 | Bot 模式前端（面板、频道侧栏、像素位） |
| `@botharness/computer` | 18MB/959 | 桌面/电脑操作能力 |
| `@botharness/core` | 11MB/30 | **引擎本体**（Bot/Inbox/Schedule/Memory/Model 编排） |
| `@botharness/browser` | 2MB/22 | 浏览器能力（headless） |
| `botui-core` + `pixel-{morph,banner,avatar}` | 各 1MB | 像素形象/动效 |
| `deepseekbot` | 1MB/32 | **兼容包装**（bin 三件 + cordis patch），description 明写「Tobs (formerly DeepSeekBot)…」 |
| `@botharness/tobs` | 1MB/29 | 更薄的包装（`src/tobs.mjs` 一行 import + 桌面主题） |

`dist/deepseekbot.mjs` 只有 824B → **能力全在 `@botharness/*`**；`tobs` 不是新引擎，是 deepseekbot 的新名字。

启动补丁 `cordis.im.patch.yml` 插 6 处：`im-provider`（顶掉 `xmanrui-dsh-im`）、`botharness-core`（`agentPreset: deepseekbot`）、`botharness-client`（UI）、`botharness-browser`（`headless: true`）、`botharness-computer`（`target: local`）、`preset-deepseekbot`（`ask_user_question` 45 秒超时转后台、subagent 走 `bot_subagent`）。

## 三 数据在哪（跨设备的前提）

| 数据 | 位置 | 能否进 git |
| --- | --- | --- |
| Bot 元数据（名称/模型/记忆目录/频道） | `~/.dsh/botharness/botharness.db`（sqlite） | ❌ 二进制+含凭据引用 |
| Bot 人格与记忆 | `bot-memory/<角色>/<bot-id>/`（`SOUL.md`、`MEMORY.md`、`log/`、`notes/`） | ✅ **这是唯一同步面** |
| IM 凭据 | 本地加密存储（面板写入），`secret-put` 只走 stdin | ❌ |
| 群聊事件 | `botharness.db` 的 `source_events` / `inbox_admissions` | ❌ sqlite；**导出成按天 jsonl 后可同步**（我方 `tools/tobs_chat_export.py`） |
| 全量备份 | `.botharness-backup`（含 DB/记忆仓/附件/模型预设；**不含 API key 与 DSH 会话记录**） | ❌ 迁移用 |

## 四 设置面（全部，分两块）

### 4.1 全局：`Settings`

| 设置 | 现值/默认 | 说明 |
| --- | --- | --- |
| **Assignment concurrency limit** | 默认 **8**（1–32） | 同一个 Bot 并行跑几个任务；与「每单派全部专家」的延迟直接相关 |
| 开发者模式 | 默认关 | 出错时面板多出诊断块 |
| 自动接受群邀请 | 默认关 | 开=被拉群自动进 |
| 工具审批规则 | 默认按 DSH 权限 | 面板可对 Bot 的工具调用设白/黑 |
| SOUL / MEMORY 字数上限 | 有上限 | 记忆写太长会被截断——纪律要写「≤5 行」 |
| idle compaction | **默认开**（半个缓存寿命、≥15 分钟） | 空闲自动压缩上下文；`/compact` 手动 |
| 遥测 | **默认开** | `telemetry:false` 或 `DO_NOT_TRACK=1` / `BOTHARNESS_TELEMETRY=0` 关 |
| 备份/恢复 | — | 恢复后进入**安全模式：全部 Bot 暂停** |

### 4.2 每个 Bot：`Bot DM → Channel sidebar`

| 面板 | 关键点 |
| --- | --- |
| **Model（两张卡）** | **主模型 Main** + **任务模型 Task**。我们只配了主模型 → DB 里 `assignment` 段为空，**派活走的是默认模型**，这是「专家委派没按预期跑」的根因。Advanced 还能给候选模型卡和备注，让 Orchestrator 按任务选型；任务模型在**建单时锁定**。 |
| **Inbox** | 所有唤醒入这里（群消息、schedule、delegation），状态 Pending/Observed/Handled/Coalesced/Skipped |
| **Schedules** | 每 Bot **≤20 个启用**；分钟/小时/每天/每周/一次/cron + 时区；到点把**指令**投进 Inbox 并唤醒 Orchestrator；`Run now` 手动触发；**Lock** 后 Bot 不能改；暂停不回补，主机离线只补最近一次；<15 分钟间隔会提示成本 |
| **Workspace Grants** | 文件/目录授权 per-Bot；`Allow all file access for new work` **默认关**（安全默认）；`grant-write-set` 可批量设 |
| **External identities** | 把已连接的 IM 应用绑到这个 Bot；准入策略 `automatic`（默认）/`ask first`；可 mute / block / 允许未@普通消息 |
| **External connectors** | 把外部群 **Sync** 成本地频道；也能配「备用主动投递目标」 |
| **Memory** | `memory-file/history/diff/save`，`--expected-head` 比较交换（写冲突会拒），`--edit-id` 幂等 |
| **Sessions / Display** | 会话列表；布局与排序 |

## 五 CLI 全命令面（`node ~/.dsh/profiles/desktop/node_modules/deepseekbot/dist/deepseekbot.mjs <cmd>`）

**约束：单写者。DSH 桌面进程在跑时 `list/model-presets` 等一律返回 `lease-unavailable` → 必须先完全退出 DSH。**

| 组 | 命令 |
| --- | --- |
| 建 Bot | `create`（空白 / `--from-git` / `--from-zip` / `--from-dir`；`--persona[-stdin]`、`--core-memory[-stdin]`、`--preset`、`--memory-location`、`--home`、`--role`、`--avatar-file`、`--operation-key`）、`create-status` |
| 读 | `list`、`show <id>`、`model-plan`、`channels`、`channel-messages --limit --before`、`pairings`、`search` |
| 模型 | `model-presets`、`model-preset-create`（**必须同时给 orchestrator 与 assignment 的 provider+model+effort**）、`model-preset-apply --expected-revision` |
| 生命周期 | `pause` / `resume` / `update` / `human-name-set` / `channel-human-name-set` |
| 记忆 | `memory-snapshot` / `memory-file` / `memory-history` / `memory-diff` / `memory-save`（`--expected-head`、`--edit-id`） |
| 权限 | `grants`、`grant-revoke`、`grant-write-set` |
| 定时 | `schedules`、`schedule-create`（`--every/--daily/--weekly --weekdays/--once-date+--once-time/--cron` + `--timezone --locked`）、`schedule-update/delete/history/run-now/preview` |
| 凭据 | `secret-put`（**只走 stdin**）/ `secret-list` / `secret-unset` |
| 其他 | `channel-media-target/download`、`ssh-tunnel`、`bot-zip-upload|preview|import|import-status`、`bot-import-preview` |

**CLI 明写的硬线**：密钥**禁止走 argv**（出现 `--api-key/--token/--secret/--password` 直接 `secret-in-argv` 硬错，输出永不含密钥）；Bot 名字只是标签（重名＝再建一个，exit 0 不报错）。

## 六 飞书接入（WebSocket，无需公网端口）

1. 飞书开放平台建**企业自建应用** → 开「机器人」能力 → 拿 `App ID`/`App Secret`。
2. 权限（Tenant token scopes）：必备 `im:message.p2p_msg:readonly`、`im:message.group_at_msg:readonly`、`im:message:send_as_bot`、`im:message:readonly`；可选 `im:message.group_msg`（读群里所有普通消息）、`im:resource`（图片/文件）。
3. 事件订阅：**长连接（WebSocket）** + `im.message.receive_v1`。
4. DSH → `Settings → IM bots → Feishu/Lark` → 手填 App ID/Secret → `Bind and connect`（凭据本地加密）。
5. Bot DM → `External identities → + 绑定应用` → 选准入策略。
6. 群里 `@` 机器人或私聊验证；排错看 **Bot Inbox**；错误码 **99991672 = 缺 `im:message:readonly`**。
7. 进阶：`/pair` 后可把**工具审批卡发到手机**；外部群可 `Sync` 成本地频道。

## 七 与我这套东西的挂载点（对号入座）

| 我方能力 | 在 Tobs 上的挂法 | 现状 |
| --- | --- | --- |
| A1 结论→建任务出卡 | Bot 调本机 `http://127.0.0.1:8787/api/tasks` + `tools/advisory_to_task.py` | 接口就绪，Bot 未被要求调用（SOUL 缺指令） |
| A2 推卡 | Tobs 内回复 + `lark_send` 卡片 | 出站实测 1.62s；无自动触发 |
| A3 签字 | 网页 `/sign`；或飞书卡片按钮 + `im_sign` 白名单 | 已实现，未在 IM 上端到端跑过 |
| A4 T+7 回填 | 本机 `POST /api/tasks/<id>/outcome` | 已实现 |
| 主动提醒 | **Tobs Schedules**（到点唤醒 Orchestrator）→ 指令里让它查 `/api/reminders` 再发 | `bot_schedules=0`，未建 |
| 记忆 | Bot 自己写 `bot-memory/<bot>/log/`（我已把纪律写进 SOUL） | 仍未落笔 |
| 群聊留痕 | `tools/tobs_chat_export.py` 每日导出 jsonl 进 git | 已跑通 |

## 八 建议（按优先级，含理由）

**P0（今天就能做，改配置不改代码）**
1. **补任务模型**：面板 Model → 任务模型选 `opencode-go / deepseek-v4.1-flash / high`。不补，派活就按默认模型跑，观测到的成本/效果都不代表你的设定。
2. **建第一条 Schedule**：给「工作助理」建「每天 08:30：读 `http://127.0.0.1:8787/api/reminders`，把今日到期/逾期按六行格式发我」。这是**主动提醒**最小可验证单元（`bot_schedules` 0→1 即可验）。
3. **关遥测**（若不想外发）：`Settings` 里 `telemetry: false`。

**P1（本周，验证四条挂载点）**
4. 在 5 个 Bot 的 `SOUL.md` 里写清「什么时候必须调本机 API」——现在是**没有指令**，不是没有能力。
5. 飞书：按 §六 建应用并把 `工作助理` 绑上去；先只做「私聊 + 签字卡」，群聊后置。
6. 备份演练一次：`Settings → 备份`，确认恢复后 Bot 全暂停、记忆仓完整。

**P2（结构性，想清楚再做）**
7. `Assignment concurrency limit` 8 → 我们「每单派 5 个专家」，并发上限决定总延迟；如果要 ≤60s，先量一次 5 并行真实耗时再定 8 还是 16。
8. 跨设备：`memory-location` 指向 git 仓（已做）+ 每日 `tobs_chat_export` 进 git + `.botharness-backup` 定期存网盘。**sqlite 永不进 git**。
9. 桌面/浏览器能力（`computer`/`browser`/`cloudflared`）**先不开**：开了等于给 Bot 你的鼠标键盘和一台可远程的机器，收益不明、风险明确。

## 九 诚实边界

- `@botharness/core` / `im-provider` / `computer` 的 dist **未做代码级逆向**（体积大、打包态），下文结论来自 CLI 帮助 + 官方文档 + DB 实测。【三个只读子代理正在补这一层】
- 官方文档为 **1.6.x 版**，与本地 1.6.1 一致；但文档未覆盖的隐藏设置（如 wake policy 细项）只有面板可见。
- 本清单不含「我方还没实现的能力」，只描述 Tobs 自身。


---

## 十 IM 与面板（第二批深读，源码行号）

### 10.1 十四个 IM adapter

| 平台 | 入站 | 需公网端口 |
| --- | --- | --- |
| 飞书 / Lark（同一 adapter，仅 domain 差异） | SDK `WSClient` 长连接 | 否 |
| 企业微信·智能机器人 | 官方 WS 长连接 | 否 |
| 企业微信·自建应用 | **本机 HTTP 回调** 默认 `127.0.0.1:30987` | **是**（要 `callbackBaseUrl`） |
| 微信（个人） | iLink 长轮询 | 否 |
| 钉钉 / QQ / Slack / Telegram / Discord | 各自长连接或长轮询 | 否 |
| WhatsApp / Matrix / iMessage / Email | 各自机制 | 否 |
| office | 出站连公网 Connector | 否 |

**结论**：飞书是唯一同时满足「群聊 + 私聊 + 卡片按钮 + 文件双向 + 零公网端口」的通道；Lark 只换 domain，不需要第二套 adapter。

### 10.2 飞书 scope（以代码为准，与官方文档有出入）

代码里实际校验：`im:message.group_at_msg.include_bot:readonly`、`im:message:readonly`、`im:resource`、`application:app_slash_command:read/write`、`im:message.group_msg`。
**缺 `im:resource` 会报 99991672**（`feishu-channel.mjs:13-21`）——文档说缺 `im:message:readonly`，两处不一致，以代码为准先全给。

### 10.3 唤醒策略（比 schedules 更关键的一层）

`Channel sidebar → Wake policy`，来源分 9 类：`human-dm / bot-dm / group-mention / group-ordinary / group-invite / group-join-request / group-join-decision / assignment-report / assignment-lifecycle`；
模式 5 种：`immediate / digest / conditional / mentions / silent`；投递方式 `turn`（新回合）或 `steer`（插话）。
→ 「Bot 好像没理我」先看这一层，不要先去翻代码。

### 10.4 面板能改什么 / 不能改什么

能改（RPC 白名单）：`bot.alias.set / bot.bind / bot.delete / bot.disconnect / bot.model.set / bot.preset.set / bot.reconnect / bot.session.list / bot.settings.update / bot.voice.set / bot.workspace.set / bot.mailbox.update / bot.auth.start|poll`。
**面板改不了**：`state.json` 的 `seenMessageIds / sessions / watches / deferred`、`config.json` 的 host/端口/`callbackBaseUrl` —— 只能改文件。→ 我方若要暴露这些配置，必须先提 RPC 需求。

### 10.5 三条不能忽略的工程事实

1. 飞书发送节流 **250ms/条（5 QPS/会话）**，单回合步骤消息 200 条熔断；每会话一条串行队列。
2. 去重靠持久 `seenMessageIds`（上限 1000），重试每 key 最多 4 条待补发；429 → `CHANNEL_RATE_LIMIT`，不确定 → `DELIVERY_UNCERTAIN`（停止重试防重）。
3. **多实例互斥只在进程内**（`consumer-conflict`），Tobs 没有跨进程文件锁 → 我方多实例部署必须自己补锁。


---

## 十一 首个真实派活的执行日志解剖（2026-11-03 00:40–00:47）

会话目录：`~/.dsh/sessions/--D-work-~4EA7~54C1~7C7B-~4E2A~4EBA~4EA7~54C1-~4F01~5FAE~002B~98DE~4E66--/botharness-7606fe1b-…/session.v4.jsonl.zstd`（160 事件 / 19 步 / 30 次工具调用）

| 观测 | 数值 |
| --- | --- |
| 入站拼接 | `agent/inbox/spliced` 8 次、`user/message` 8 条（群里几条消息被拼进同一回合） |
| 工具分布 | `web_fetch` 12、`pwsh` 7、`web_search` 3、`write` 2、`compress` 2、`spawn_teammate` 1、`wait_agent` 1、`list_agents` 2 |
| 错误 | 5 条：**`tool call timed out after 30000ms` ×3**、**`BotHarness Session file path cannot be resolved` ×2** |
| 结束 | `turn/end → {"kind":"aborted","reason":{"kind":"user"}}` |

**结论（可复算）**：这次派活不是「失败」也不是「卡死」——它真的在干活（查网、写报告分片），被 **user abort** 终止在 step 19，因此 `latest_report_state=None`（没走到 `report_to_orchestrator`）。

**暴露的两个真问题**：
1. **工具 30 秒硬超时**：`web_fetch`/`web_search` 走公网慢（本机常 9–20s），3 次直接超时。→ Bot 不该用外部抓取，应该调本机脚本（`tools/rss_fetch.py`、`/api/context/ask`），本机调用毫秒级。
2. **Bot 内 spawn_teammate 不可用**：`BotHarness Session file path cannot be resolved` → Bot 模式下起子代理失败。→ 编排要让 Bot 走 Tobs 自己的 Delegation，而不是 DSH 的 `spawn_teammate`。
