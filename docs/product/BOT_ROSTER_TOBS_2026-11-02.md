# 五个 Bot 的建置配置（Tobs 底座）

> 依据：`BOT_ROLES_2026-11-02.md`（边界与职责）+ 用户 m08498/m08518 拍板（团长=Tobs Orchestrator 兼任、5 专家全上、全员 `deepseek-v4.1-flash` + `reasoning=high`、每单可派全部专家、L0–L3 编排参考 dsh-bot）。
> 状态：`@botharness/tobs@1.6.0` 已装到 `desktop` profile，**需重启 DSH 才出现 deepseekbot 预设**；重启后按本文件逐个建 Bot。

## 一、Bot 清单

| # | Bot | emoji | 人格底稿 | 模型 | 能不能主动推 | 记忆 | 一句话职责 |
|---|---|---|---|---|---|---|---|
| 0 | 工作助理（团长） | 🧭 | `packs/expert_market/manufacturing-business-team`（IDENTITY/SOUL） | flash + high | **能**（唯一） | 独立 git 仓 | 接话、判级、派活、汇总、出卡 |
| 1 | AI 专家 | 🧠 | `persona/andrej-karpathy/distill` | flash + high | 否 | 独立仓 | 判断「这事该不该用 AI、用什么形态、怎么验」 |
| 2 | 产品专家 | 🍎 | `persona/steve-jobs/distill` | flash + high | 否 | 独立仓 | 一句话定义、减法、删需求 |
| 3 | 业务专家 | 🏭 | `persona/kazuo-inamori/distill` + `persona/foreign-trade-b2b/distill` | flash + high | 否 | 独立仓 | 算得清账吗、谁负责、交期与单证敢不敢承诺 |
| 4 | 内容操盘手 | 🎬 | `persona/manufacturing-content-operator/distill` | flash + high | 否 | 独立仓 | 情报→核验→选题→结构→标题→渠道→复盘→沉淀 |

> 业务专家一个 Bot 挂**两份人格**：内部经营问题走稻盛，外贸/交付/单证问题走外贸主题。建 Bot 时把两份底稿都塞进它的 persona 目录，并在它的 `IDENTITY.md` 写一句路由规则：「涉及境外客户/询盘/单证/交期承诺 → 用外贸口径；其余 → 用经营口径」。

## 二、团长（0 号）的四条硬约束（写进人格，不靠自觉）

1. **先判级再派活**：L0 自己答（≤10s）、L1 派 1 个（≤40s）、L2 派 2–3 个并行（≤90s）、L3 全员并行（≤150s）。**L3 只给涉及签字/对外/多部门的活**。
2. **汇总必须写「未采纳谁 + 为什么」**，否则视为未收敛。
3. **只回结论**：不转述专家原话，不复述过程。
4. **唯一有推送权**：每天主动消息 ≤3 条（早间今日/到点提醒/晚间一句复盘）；专家一律不许主动找用户。

## 三、建 Bot 的顺序与验证

1. 重启 DSH（关掉 GUI 再开）→ 确认出现 Tobs 面板与 `deepseekbot` 预设。
2. 先建 0 号工作助理：接飞书私聊 → 发一句「今天有什么」，能回「今日 3 件 + 逾期 N 件」即通（数据来自我方 `/api/workitems`）。
3. 再建 1–4 号专家（先不给它们飞书入口，只在群聊/委派中出现）。
4. 建一个「工作事项」群，把 5 个 Bot 都拉进去；@ 谁谁答。
5. 跑 D4 真实一单：一句话 → 团长判 L3 → 5 专家并行 → 出卡 → 签字，记录总时长与「未采纳谁」。

## 四、验收（这一节不许含糊）

| 编号 | 判据 | 通过线 |
|---|---|---|
| B1 | 团长判级正确 | 10 次抽样里 ≥8 次级别合理（不会全 L3） |
| B2 | 单次定案时延 | L0 ≤10s / L1 ≤40s / L2 ≤90s / L3 ≤150s（超时须标注「未咨询 X」） |
| B3 | 汇总含未采纳说明 | 10 张卡里 10 张都有 |
| B4 | 专家不主动打扰 | 24h 内除 0 号外无主动消息 |
| B5 | 一句话能否定案 | 3 轮内出卡比例 ≥80% |

## 五、诚实边界

- 全员 flash：便宜、快，但判断深度弱于 pro；对冲见 §11.3 三条（写死「必须给 1 条反证」、连续 2 次判不准单点升 pro、判据只看 T+7 命中）。
- Tobs 的 Bot 记忆是**普通 git 仓**：能看 diff、能手改、能回滚，但也意味着**写坏了要人修**，没有自动修复。
- 我方与 Tobs 的边界：Tobs 管身份/群聊/委派/定时/IM/浏览器；我方管决策卡/签字封存复核回填/工作事项账本/上下文检索/飞轮指标。**任何一方都不要越界重写对方的职责**。


## 六、4 号内容操盘手的「每日资讯推送」配置

| 项 | 配置 |
|---|---|
| 触发 | Tobs 定时任务：**08:30 主推** / **12:30 补漏（默认静默）** / **20:30 一句话复盘** |
| 数据源 | RSSHub 公网实例（主 `hub.slarker.me`，备 `rsshub.ktachibana.party`）→ `tools/rss_feeds.json` 四个源 → `tools/rss_fetch.py --limit 10 --per-source 3`（19 项测试全绿）；newsboard/dsh-news 插件因 github 被墙与版本不兼容未装 |
| 判断依据 | 我方 `context_index` 索引（对齐在办事项）+ 该 Bot 的 6 个心智模型 |
| 推送渠道 | 飞书私聊（Tobs IM），卡片式；不进群、不 @ 全体 |
| 条数 | 主推 **10 条/天**（前 3 条完整六行，第 4–10 条各一行摘要），补漏 ≤2 条/天 |
| 交互 | 每条附 `不相关 / 深挖 / 记下` 三词，回词即记账 |
| 停推条件 | 连续 3 天「不相关」> 60% → 停推等新订阅源 |
| 人格契约 | `packs/expert_market/manufacturing-advisory-board/agents/content-operator.md` 的「每日情报推送」节 |

> 为什么不写 cron 脚本：脚本能按时发，但**不知道该发哪 10 条、也不知道哪条与你今天在办的事有关**。判断权在这个 Bot 身上，定时权在 Tobs 身上，采集权在 RSSHub/看板上——三方各干一件，不互相顶替。

## 七、前端主入口（2026-11-02 用户拍板）

| 位次 | 界面 | 说明 |
|---|---|---|
| **主入口** | **Tobs 面板（DSH 内）** | Bot 身份/群聊/委派/任务卡/审批卡都在这里；未重启 DSH 看不到，重启后生效 |
| 备选 | 我方工作台 `http://127.0.0.1:8787/ui/index.html` | 工作事项台、决策台、任务工作台、执行舞台、交付墙；作为"看账本/看证据链"的备用视图，不再当主界面 |
| 通道 | 飞书私聊（Tobs IM） | 手机上的一等公民：`/today` 看今日、点卡签字、回一句改状态 |

判断依据：**面板跟人走，账本跟证据走**。日常操作在 Tobs 面板（Bot 在哪你在哪），需要核对证据/翻历史时才回 8787。

## 八、人格底稿现状（2026-11-02 复验）

| 角色 | 人格底稿 | sha | 状态 |
|---|---|---|---|
| 工作助理（团长） | 无独立人格文件（由 Tobs Orchestrator 兼任） | — | 待重启后建 Bot |
| AI 专家 | `persona/andrej-karpathy/distill`（7 模型/8 启发式，含可验证性） | 7186ad00 | ✅ |
| 产品专家 | `persona/steve-jobs/distill`（6 模型 + 后见之明校验） | fcdcdc0f | ✅ |
| 内容操盘手 | `persona/manufacturing-content-operator/distill` | c7c05174 | ✅ |
| 业务专家 · 稻盛 | `persona/kazuo-inamori/distill`（6 模型/8 启发式） | b4e77fa3 | ✅ |
| 业务专家 · 外贸 | `persona/foreign-trade-b2b/distill`（主题蒸馏，不挂人名） | a98ca72b | ✅ |
| 业务专家 · 芒格 | 女娲现成 `munger-perspective`（零成本，按需接入） | — | 未接线 |

**口径**：业务专家是**一个席位、三套底稿**（稻盛/外贸/芒格），按任务类型切换，不拆成三个 Bot——拆了会把「每单派全部专家」的并行成本乘 3，而 MVP 阶段收益不成立。

复验命令：`python -B tools/rebuild_persona.py --check`（5 个 sha 全 OK）、`python -B tests/test_advisory_pack.py`（六角色全绿）、`python -B tests/validate_structure.py`（PASS）。

## 九、建 Bot 填表清单（可直接抄进 Tobs 表单）

> 用途：Tobs 建 Bot 表单逐字段要填什么。所有 Bot 共用：**模型 `deepseek-v4.1-flash`**（opencode-go 套餐）、**思考强度 `high`**、**语言 中文**、**工具 默认全开（除计算机控制在首周只读）**。

### 0 号 工作助理（团长）

| 字段 | 填什么 |
|---|---|
| 名称 | 工作助理 |
| emoji / 头像 | 🧭（像素头像选「向导/罗盘」类） |
| 一句话定位 | 你的工作管家：接住每句话，判级、派活、汇总、出卡。 |
| 人设来源 | 本仓 `packs/expert_market/manufacturing-business-team/`（IDENTITY.md + SOUL.md） |
| 记忆 | 独立 git 仓 `bot-team-memory/assistant` |
| 主动推送 | **开**（每天 ≤3 条：早间今日 / 到点提醒 / 晚间复盘） |
| 群聊 | 「工作事项」群 + 飞书私聊 |
| 首条自检 | 发「今天有什么」→ 应回「今日 3 件 + 逾期 N 件」 |

### 1 号 AI 专家

| 字段 | 填什么 |
|---|---|
| 名称 | AI 专家 |
| emoji | 🧠 |
| 一句话定位 | 判断这事该不该用 AI、用什么形态、怎么验收。 |
| 人设来源 | `packs/expert_market/manufacturing-advisory-board/persona/andrej-karpathy/distill`（7 模型 / 8 启发式） |
| 记忆 | `bot-team-memory/ai-expert` |
| 主动推送 | **关**（只能被团长委派或群内 @） |
| 群聊 | 只在「工作事项」群里被 @ 时回答 |
| 输出纪律 | 只回六行；必须给 1 条反证 |

### 2 号 产品专家

| 字段 | 填什么 |
|---|---|
| 名称 | 产品专家 |
| emoji | 🍎 |
| 一句话定位 | 一句话定义、做减法、删需求。 |
| 人设来源 | `persona/steve-jobs/distill`（含「后见之明」校验段） |
| 记忆 | `bot-team-memory/product-expert` |
| 主动推送 | 关 |
| 群聊 | 「工作事项」群 |
| 输出纪律 | 先答「这条能不能删」，再答「留什么」 |

### 3 号 业务专家

| 字段 | 填什么 |
|---|---|
| 名称 | 业务专家 |
| emoji | 🏭 |
| 一句话定位 | 算得清账吗、谁负责、交期与单证敢不敢承诺。 |
| 人设来源 | **两份**：`persona/kazuo-inamori/distill`（内部经营）+ `persona/foreign-trade-b2b/distill`（外贸/单证/交付） |
| 路由规则 | 在它的 IDENTITY.md 写死：涉及境外客户/询盘/单证/交期承诺 → 外贸口径；其余 → 经营口径 |
| 记忆 | `bot-team-memory/business-expert` |
| 主动推送 | 关 |
| 群聊 | 「工作事项」群 |

### 4 号 内容操盘手

| 字段 | 填什么 |
|---|---|
| 名称 | 内容操盘手 |
| emoji | 🎬 |
| 一句话定位 | 情报→核验→选题→结构→标题→渠道→复盘→沉淀，一条龙。 |
| 人设来源 | `persona/manufacturing-content-operator/distill` |
| 记忆 | `bot-team-memory/content-operator` |
| 主动推送 | **开**（仅资讯：08:30 主推 10 条 / 12:30 补漏静默 / 20:30 复盘一句） |
| 群聊 | 飞书私聊为主，不进群 |
| 定时任务 | 三个（`08:30` / `12:30` / `20:30`），时区 Asia/Shanghai |
| 数据源 | `tools/rss_feeds.json`（RSSHub 主 `hub.slarker.me`、备 `rsshub.ktachibana.party`）+ `tools/rss_fetch.py --limit 10 --per-source 3` |

### 建完后的三步验收

1. 0 号私聊发「今天有什么」→ 10 秒内回「今日 N 件 / 逾期 M 件」（数据来自 `/api/workitems`）。
2. 群里 @0 号说「帮我定一下这周先做哪三件」→ 它应判级（L1/L2）、@对应专家、汇总并写「未采纳谁」。
3. 4 号次日 08:30 是否真推 10 条（首日允许人工触发一次验证格式）。

---

## 九、记忆仓与建 Bot 填表值（2026-11-02 16:20）

记忆仓：`https://github.com/pipishou/bot-memory`（**私有**），本地 `D:/work/产品类/个人产品/bot-2/bot-memory`，首提交 `9292948`。
三层记忆：核心事实 `<bot>/MEMORY.md`／日期日志 `<bot>/log/YYYY-MM.md`／临时便签 `<bot>/notes/`。

建 Bot 时各字段按下表填（人格文件路径相对 `bot-team/packs/expert_market/manufacturing-advisory-board/`）：

| # | 名称 | 一句话定位 | 人格文件 | 记忆目录 | 模型/强度 | 可主动推 |
|---|---|---|---|---|---|---|
| 1 | 工作助理（团长） | 每天开局、判级派活、收敛拍板 | `agents/jobs.md`（团长不加人格，用中立语气；人格仅用于产品判断） | `assistant/` | deepseek-v4.1-flash / high | ✅ 每天 ≤3 条 |
| 2 | AI 专家 | 模型能力边界与可验证性 | `agents/karpathy.md` | `ai-expert/` | flash / high | ❌ |
| 3 | 产品专家 | 该删什么、第一秒体验 | `agents/jobs.md` | `product-expert/` | flash / high | ❌ |
| 4 | 业务专家 | 经营账 + 外贸口径双路由 | `agents/kazuo-inamori.md`（主）/ `agents/foreign-trade-b2b.md`（外贸任务切换） | `business-expert/` | flash / high | ❌ |
| 5 | 内容操盘手 | 每日 10 条情报 → 选题 → 标题 | `agents/content-operator.md` | `content-operator/` | flash / high | ✅ 仅 08:30 情报 |

建 Bot 的通用提示词（每个都粘同一段，只改角色名）：
```
你是「<角色名>」。人格与纪律已写在你的人格文件里，先读它，再回答。
记忆目录：bot-memory/<bot>/（核心事实 MEMORY.md、日期日志 log/、临时便签 notes/）。
铁律：只回六行；结论必须带出处或标[推断]；不许替我拍板；不确定就说不确定。
```
