# Bot 团队工作台 · 架构 v1（2026-10-19）

> 状态：M1.5 施工中（A7–A12 验收项）。本文是产品与工程的**唯一架构准绳**，后续实现偏离须走升级协议。
> 输入：一物两参考——**Octop**（多用户控制面，`venv/Lib/site-packages/octop/`）× **DSH**（执行运行时，0.2.1-rc.2）；产品意图与不变量见 `docs/OPTIMAL_TEAM_AND_UI_2026-10-18.md`、`team/gates.md`、`.junshi/ledger.md`。

---

## 0. 一句话

**不是聊天框，也不是 dashboard，而是一间看得见员工干活的事务所。**
架构上 = **控制面（本地服务）＋ 领域内核（core）＋ 运行态目录（runs/）＋ 执行运行时（DSH）＋ 渠道化界面（web）**；控制面与运行时分离，事件是唯一真值，交付卡是唯一产物。

---

## 1. 三个系统怎么对照

| 维度 | Octop（参考） | DSH（宿主/引擎） | 本产品（bot-team） |
|---|---|---|---|
| 定位 | 多用户 AI agent 控制面 | agent 运行时（会话/工具/模型/插件） | 单用户→团队的**工作台**（有 UI 的事务所） |
| 控制面 | 55 router REST+JWT（`api/app.py:202-268`） | 无 HTTP API，CLI/stdio 驱动 | 本地 stdlib 服务（`server/app.py`），REST + SSE |
| 引擎 | 全部委托 harness-agent/gateway/memory（`manager.py:430-435`） | 自带 | **不改 DSH**；M2 用 `dsh headless`/`sdk` 起会话，M3 插件内嵌 |
| 事件 | `trajectory_events` 追加日志 + SSE 双续传（`trajectory.py:144-236`） | `session.v4.jsonl.zstd` 会话日志（可 tail） | `runs/<task_id>/events.jsonl`（seq，append-only）+ SSE |
| 界面 | 虚拟 IM 渠道 `WebSocketChannel`（`ws_channel.py:32`） | Web GUI（插件化） | **界面即渠道**：一个任务=一个"线程"，事件流驱动舞台 |
| 历史 | 双层：投影写失败只 warn（`processor.py:1443-1454`） | 会话日志即真值 | 事件真值 + task/status 投影（投影坏可重放重建） |
| 身份 | users + JWT + user_scope（`usage.py:47-66`） | 本机单用户 | owner=老板（本机单用户 M1.5；多租户推迟 M3+） |
| 成本 | 每轮一行 usage 账本（`usage_record`，`processor.py:1116-1123`） | 会话内 | 每任务/每阶段一行 cost（`tools/cost_record.py`） |
| 定时 | cron 两段式（触发 `cron/trigger.py:75-105`、投递 `cron/job.py:96-130`） | — | T+7 回看两段式（`review_due.py`） |
| 数据 | DB 整数代理主键 + 业务 id UNIQUE（`001_initial.sql:1-10`） | 文件 | runs/<task_id>/ 目录 + ids.json 发号，业务 id 唯一 |

**可搬的 6 个机制**（都有行号证据，见上表）：①界面即渠道（一个 processor / 多入口）②双层历史+seq 续传+同 seq 幂等重发 ③每轮一行 usage 账本 ④cron 触发/投递两段式 ⑤控制面/运行时分离 ⑥关键工具白名单（`tool_catalog.py:11-21` 的 CRITICAL_TOOLS 思路 → 我们的门禁脚本不可被业务配置关掉）。
**不搬**：55 router 单体控制面、WS 弱续传（断线不重放）、每 agent 160MB memory.sqlite + markdown 双份记忆、`config_json` 万能字段。**octop.ui 不存在**，其 dashboard 是预构建 SPA 产物（`app.py:287-310` 静态托管），只能参考交互不能复用代码。

---

## 2. 总体架构

```
┌──────────────────────────── Web（ui/，单文件视觉资产 + api-adapter）────────────────────────────┐
│ 任务栏 │ 员工货架(6态) │ 班组编排(crew DAG) │ 舞台实况(事件流) │ 交付墙(交付卡+签字+回看) │ 接单/审批抽屉 │
└───────────────▲──────────────────────────────▲──────────────────────────────▲─────────────────┘
                │ REST(JSON)                   │ SSE(id/Last-Event-ID)        │ 静态回退(演示)
┌───────────────┴──────────────────────────────┴──────────────────────────────┴─────────────────┐
│ 控制面 server/（Python stdlib，127.0.0.1:8787，单进程多线程，无新依赖）                        │
│  /api/tasks, /api/tasks/<id>, /data, /events, /gate, /run, /sign, /finalize, /team, /packs      │
└───────────────▲──────────────────────────────▲──────────────────────────────▲─────────────────┘
                │ 调领域内核（纯函数/脚本）      │ 读写运行态                    │ spawn 子进程
┌───────────────┴──────────────┐  ┌────────────┴──────────────┐  ┌────────────┴──────────────┐
│ core/（领域内核）            │  │ runs/<task_id>/           │  │ 执行运行时（M2/M3）        │
│ ids·provenance·gates·pack    │  │ task.json status.json     │  │ DSH headless / sdk / 插件  │
│ team·events·gate·paths       │  │ events.jsonl report.json  │  │ 会话日志→事件投影           │
│ report_contract              │  │ signature.json gate.json  │  │ （M1.5 先 fixture/external）│
└───────────────▲──────────────┘  │ loop/ data/ run.log ids   │  └───────────────────────────┘
                │ 唯一数值源        └───────────────────────────┘
┌───────────────┴──────────────┐
│ packs/<id>/pack.json（行业包）│   team/（名册/班组/货架/角色卡/判据）   assets/（判据 YAML）
└──────────────────────────────┘
```

**依赖方向（硬约束）**：`ui → server → core → (packs, team, runs)`；`core` 不得 import server/ui；`ui` 不得直读 `runs/`（只能经 server）；数值只有一个源（`packs/<id>/pack.json`），YAML/JS 不得复制数值。

---

## 3. 关键机制设计

### 3.1 事件是唯一真值（借鉴 trajectory_events）
- `runs/<task_id>/events.jsonl` append-only，每行 `{seq, ts, kind, role, phase, text, ref, task_id}`；`kind ∈ user|speak|tool|think|status|artifact`（与 `orchestration/dsh-session-tape.mjs:11-16` 的 beat 四类对齐并扩展）。
- seq 单调递增、写入 `O_APPEND`+锁；读取按 seq 游标；**同 seq 重发幂等**（客户端按 seq 去重）。
- `status.json` / `task.json` 是**投影**，写失败只 warn；坏了可 `events.jsonl` 重放重建（M2 提供 `rebuild_status.py`）。
- SSE 帧 `id: <seq>` + `data: <单行JSON>`；断线用 `since=`（或 `Last-Event-ID`）续传；15s 心跳 `: ping`；**断线不取消运行中的任务**（借鉴 `ws.py:178-179`）。

### 3.2 界面即渠道（借鉴 ws_channel）
- 一个任务 = 一个线程；舞台不"播放动画"，而是渲染该线程的事件流投影（并行阶段同屏 = s1‖s2 两条链按 role 落座）。
- 轮询（2.5s）只作 SSE 不可用时的降级；静态 `tape-data.js` 只作离线演示回退。
- 座位由 `crew.steps` 动态生成：`role → employee → sprite_key`（修掉长 slug/短 key 错位），默认座位数 = 当前 crew 角色数（不再写死 4 人 5 拍）。

### 3.3 身份与信任链（这是产品的"可信"所在）
- `owner` 默认「老板」；`--allow-signers` 默认「老板,boss」，**fail-closed**：未配置或不在名单 → 拒绝，不靠环境变量兜底。
- 合法交付链：`decision.status=proposed` → `POST /sign`（signer∈白名单，对 report 整文件 sha256 签名）→ `POST /gate`（若 `security_gate_requested`/不可逆：`confirmed_by` 非空且∈白名单）→ `POST /finalize`（状态机+task_id 一致+sha 一致+gate confirmed）→ `approved`，`approval.by=signer`。
- `gate.json` history append-only；不可逆动作**只建议不执行**（`team/gates.md:6,11`）；G0 预派发仍是第一道人工门。
- 门禁脚本（core/gates.py、finalize_delivery.py）是 CRITICAL 资产：**业务配置不得关闭**（借鉴 CRITICAL_TOOLS）。

### 3.4 数据与发号
- `runs/<task_id>/`：`task.json` `status.json` `events.jsonl` `data/`（用户输入 T/T30 原始文件）`result.json`（workflow 结果）`report.json` `signature.json` `gate.json` `ids.json` `loop/`（证据/分析/决策/闭环四卡）`run.log`（旧格式兼容）。
- 发号：`ids.json` 按任务命名空间 + 全局序列卷 `runs/.task_seq.json`，`O_CREAT|O_EXCL` 锁 + `os.replace` 原子写；业务 id `T-/DL-/D-/A-/E-/C-YYYYMMDD-NNN`。
- 报告 provenance：`data_refs` 一律 `[{path, sha256, size}]`；`_sha256` 是报告整文件哈希；`scorecard.total/verdict` 必须由 `score_scorecard` 计算并进报告。
- 用户真实数据进来后**不改数、不插值**；缺失写 `missing`（`gates.md:22`）；封闭世界 11 类因素外拒答（`gates.md:21`）。

### 3.5 成本与回看（借鉴 usage/cron 两段式）
- 每任务每阶段一行 cost（模型调用/token/金额/来源），汇总 `by_day/by_agent/by_task`；默认**只观测不硬限**（不设硬上限铁律），超观测线只告警。
- T+7 回看：触发（到期扫描）与投递（生成回看请求/事件）两段分离；未回填 `actual_outcome` 不得 `closed`（`team/loop/README.md`）。

### 3.6 与 DSH 的边界
- **不改 DSH 引擎**；M1.5 执行器 = `fixture`（演示，报告 `is_demo=true`）/`external`（等 `result.json`，适合手工/外部脚本）；`dsh` 返回 501（M2 实现）。
- M2 执行器：`dsh --profile headless "<task prompt>" --json` 或 sdk stdio JSON-RPC 起会话；会话日志/事件投影为 `events.jsonl`；任务取消/进度由 server 轮询/订阅映射。
- M3 形态：DSH Web 插件（`dsh.client.inject` + `cordis.patch.yml`，参照 `dsh-plugin-marketplace` 结构）整页/面板内嵌；反壳（不出现模型名/token/轨迹、浅色锁定）。

---

## 4. UI 架构（四区 + 信息层级）

| 区域 | 数据来源 | 空态/错态 | 关键交互 |
|---|---|---|---|
| 顶栏任务栏 | `GET /api/tasks`（状态点/标题/时间） | 无任务→引导接单；服务离线→静态演示角标 | 新建任务（G0 抽屉）、切换任务 |
| 员工货架 | `GET /api/team`（employees+shelf 6 态+badges） | coming/not_ready 显式「为什么不能招」（不许空壳，`shelf.yaml:10,14`） | 搜索、详情、雇佣（入班组） |
| 班组编排 | `GET /api/team`（crews + dag） | 不足 2 人/套件未就绪→不可入组 | 选班组、看 crew 步骤与角色 |
| 舞台实况 | `GET /api/tasks/<id>/events?stream=1` | 无事件→空舞台；SSE 断→回轮询→静态回放 | 播放/暂停/定格、点击角色看台词、并行阶段同屏 |
| 交付墙 | `GET /api/tasks/<id>/delivery` + report | 草稿/待签/待确认/已批/已拒五态 | 交付卡（finding≤2/cannot/ROI/下一步）、签字、打回、导出、T+7 回看 |
| 接单抽屉 | `POST /api/tasks` + `/data` | 文件不合 schema→字段级错误 | 工厂/产线/指标/窗口/基线/目标 + T/T30 上传 + runner 选择 |
| 审批门抽屉 | `GET/POST /api/tasks/<id>/gate` | 未确认→finalize 禁用并说明原因 | 确认/拒绝 + 确认人（默认老板）+ 备注 |

视觉与可读性约束：浅色外壳 + 深色舞台内胆 + 原创像素 chibi；**不出现模型名/token/轨迹**；G0 验收=5 秒内说出「谁在做什么、交付了什么」；字号 ≥12px、可键盘操作、事件用事件委托替代 21 处 inline onclick（M1.5 后清）。

---

## 5. API 契约（控制面）

| 方法/路径 | 作用 | 关键约束 |
|---|---|---|
| GET `/api/health` | 存活/版本/pack | 无鉴权（本机） |
| GET `/api/packs` | 行业包列表 | 读 pack.json 单源 |
| GET `/api/team` | 名册/班组/货架/roster/persona | 违规进 warnings/invalid 不抛 |
| GET `/api/tasks` | 任务列表+汇总 | 服务端 join status |
| GET `/api/tasks/<id>` | 任务详情（task/status/data_files） | 404 明确 |
| POST `/api/tasks` | 接单（含 data_files） | schema 校验；超限 400；路径安全 |
| POST `/api/tasks/<id>/data` | 追加数据文件 | 同上 |
| GET `/api/tasks/<id>/log?after=n` | 旧格式日志（兼容） | 降级用 |
| GET `/api/tasks/<id>/events?since=n\|stream=1` | 增量/SSE 事件流 | seq 续传；非法 since→400 |
| POST `/api/tasks/<id>/run` | 执行（runner: fixture/external/dsh） | 202 + 后台线程；fixture 必须 is_demo=true |
| POST `/api/tasks/<id>/sign` | 签字 | signer∈白名单；返回 report sha256 |
| POST `/api/tasks/<id>/gate` | 审批门确认/拒绝 | confirmer∈白名单；写 gate.json+report |
| POST `/api/tasks/<id>/finalize` | 交付定稿 | 状态机+task_id+sha+gate；不满足 409+reason |
| GET `/api/tasks/<id>/delivery` | 交付卡数据 | 只读投影 |
| GET `/ui/*` | 静态前端 | 后端缺失时前端静态回退不崩 |

错误约定：400 参数/路径/schema；404 不存在；409 状态冲突（未签/门未确认/已批）；500 带 `error` 与 `detail`（不吞异常）。

---

## 6. 团队模型（四表 + 六态 + 四卡）

- `team/employees.yaml`：员工唯一 id、显示名、sprite_key（显式映射）、category（deliver/quality/resident）、abilities、state。
- `team/shelf.yaml`：6 态 `coming(无 SKILL.md 不上架)/not_ready(依赖未就绪)/craft(只做工序)/resident(常驻)/retired(退役≠删除)/active(可入班组)` + badges；状态变更须同步班组可用性（`shelf.yaml:48-51`）。
- `team/crews.yaml`：crew ≥2 接力、只引用 employee id；五步 DAG `s1‖s2 → s3 → s4 → s5`。
- `team/roles/*.md`：persona 四件套（preset/persona/skills/sample）→ 员工档案页数据源。
- 四卡：`draft → researched(G1) → analyzed(G2) → decided(G3) → delivered → closed(G4)`，`rejected/superseded` 一等状态（`team/loop/README.md:8-18`）；交付卡 schema v0.2 首屏四块 + 签字三栏 + followup 7 天（`team/delivery-card.schema.yaml:15-39`）。

---

## 7. 行业包与"通用判据 + 行业包"标品路径

- 通用件（M1 已建）：core 的 ids/provenance/gates/pack/report_contract/team/events/gate/paths；与行业无关。
- 行业包 `packs/<id>/pack.json`（唯一机器源）：`thresholds`（补料基线/目标、单元格空值率、T30 窗口）、`roi_anchors`（停机/换模/保养等金额）、`irreversible_actions`、`six_dims`、`crew_id`、占位/不可逆词表。
- 制造业包 `packs/manufacturing/` 是第一套模板；换行业 = 新增 `packs/<id>/` + `team/` 行业资产，core/server/ui 不动。
- 判断资产：`assets/*.yaml` 判据必须被代码消费（`factors_table`、`scorecard_six_dim` 已消费；其余进 prompt/结构校验，M2 逐步接机检）。

---

## 8. 演进路线

| 阶段 | 目标 | 判据 |
|---|---|---|
| M1（已完成） | 真报告上屏 + 签字哈希一致 | A1–A6 全绿 |
| **M1.5（施工中）** | 真实数据接单 → 多任务 → 事件流 → 审批门 → 团队真数据 | A7–A12 全绿 + A1–A6 回归 |
| M2 | DSH 执行器（headless/sdk）跑真会话、事件从会话日志投影、取消/进度、记忆面板、成本行、T+7 回看、多任务并发队列 | 真任务端到端（无 fixture）+ 事件真值可重放 |
| M3 | DSH Web 插件形态 / 多租户、反壳、团队标品发布 | 插件内嵌验收 + 发布门 |

**不做（负边界）**：不做聊天框/通用 IM；不改 DSH 引擎；M3 前不做多租户；不引入重型前端框架与构建链（保留单文件视觉资产）；不复制 octop 的 55-router 单体。

---

## 9. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| 假真实 | fixture 报告当真实 | `is_demo` 强制 + UI「演示数据」角标 + A12 机检 |
| 门被绕过 | 未确认 gate/伪 signer | fail-closed 白名单 + 状态机 + A9 对抗用例 |
| 事件流退化成动画 | 轮询猜阶段 | 事件真值 + seq 续传 + 同 seq 幂等（A10） |
| 多任务串台 | 单 currentTaskId | 任务命名空间 + `runs/<id>` 隔离 + A11 |
| 身份错位 | 长短 id 混用 | team.py `SPRITE_BY_ID` + 员工 id 唯一校验 |
| 数值漂移 | YAML/JS 复制金额 | pack.json 单源 + grep 机检（¥800/500/1200 零命中） |
| 文档漂移 | docs 两副本、版本/路径过时 | `JuJing/docs` 定稿、`bot-team/docs` 工作副本；M1.5 收尾合一并清 dist 引用 |

---

## 10. 证据索引

- Octop：`venv/Lib/site-packages/octop/`（`api/app.py:202-268`、`gateway/…/ws_channel.py:32`、`trajectory/live.py`、`chat/trajectory.py:144-236`、`processor.py:1116-1123,1443-1454`、`repos/usage.py:145-396`、`cron/trigger.py:75-105`、`001_initial.sql:1-10`）
- DSH：`D:\tools\deeseek-harness\resources\app.asar\dsh\`；会话日志 `C:\Users\kf-41\.dsh\sessions\...\session.v4.jsonl.zstd`；CLI `dsh.cmd`（headless/sdk/web/plugin）；插件结构参照 `C:\Users\kf-41\.dsh\profiles\web\node_modules\dsh-plugin-marketplace`
- 产品：`docs/OPTIMAL_TEAM_AND_UI_2026-10-18.md:72-85`、`team/gates.md:6,11,21,22,27`、`team/shelf.yaml:6-51`、`team/delivery-card.schema.yaml:15-39`、`team/loop/README.md:8-32`
- 审计：`audit/2026-10-19-deep-audit.md`、`audit/M1-build-spec.md`、`audit/M1.5-build-spec.md`、`audit/M1-acceptance-2026-10-19.md`

---

## 附录 A · Octop 深度调研：10 个可搬模式 + 8 个反模式（同日夜二次取证）

> 取证对象：`venv/Lib/site-packages/octop` v1.0.1 全 infra 层 + `.octop/octop.db` schema 抽查。只读。

| # | 可搬模式 | octop 出处 | bot-team 落点 |
|---|---|---|---|
| 1 | 会话键→单线程（禁分叉） | `infra/gateway/threads.py:165` | 一个任务=一条线程，`task_id` 即会话键；任务表不派生第二线程 |
| 2 | 权威+投影双写 | `repos/thread_messages.py:231`（`append_if_ready`，status≠ready 返回 0） | `events.jsonl` 真值 + `task.json/status.json` 投影；投影失败只 warn 不阻塞 |
| 3 | 追加式事件账本 | `migrations/012:3`（trajectory_events） | `events.jsonl` seq；回放/审计/断点续传的底座 |
| 4 | 流内 usage 抽取去重 | `usage_record.py:251`（按 call_id 去重） | `cost_record` 每调用去重，防流重放双计（我们曾实测重跑 double-count） |
| 5 | 时间窗×维度聚合 | `repos/usage.py:215` | 成本页 `by_day/by_agent/by_task`；配额默认只观测不硬限 |
| 6 | 事件级 HITL 暂停 | `processor.py:722-769`（下一条消息即批复） | `gate.json` = 事件级暂停，**持久化落盘**（不学其内存 TTL 30min） |
| 7 | per-session 串行锁 | `cron/delivery.py:83` | 同一 task 的 run 与事件写入串行（M2 加 run 锁；现 O_APPEND 锁） |
| 8 | WS 订阅 + 断线不取消 | `api/routers/chat/ws.py:95`、`ws_hub.py:73` | SSE 断线不取消运行任务；重连按 since 续流 |
| 9 | SSE 断点续播 | `chat/trajectory.py:144` | `since`/`Last-Event-ID` 续传 + 同 seq 重发幂等 |
| 10 | 专家=目录快照 | `experts/publish.py:27`（排除运行时目录） | 角色包 = manifest + persona + skills + sample；便于团队标品分发 |

| # | 反模式（不照搬） | 原因 | bot-team 对策 |
|---|---|---|---|
| 1 | 进程内单例（WSHub/HitlPendingStore/CronManager） | 重启丢暂停态、不能水平扩展 | gate/任务状态落盘 JSON；M3 若上服务再考虑外部存储 |
| 2 | 16 个手写迁移 + SQLite/PG 双份 + `ensure` 二次建表 | 双源真相、返工 | 暂不引 DB；runs 目录 + JSON 契约 |
| 3 | 三种 id 并存（integer PK / TEXT / ULID） | v7 才大手术 | 单一业务 id（T-/DL-/…）+ 发号器 |
| 4 | AgentManager 125KB + GlobalProcessor 59KB 单体 | 领域边界糊 | core 保持纯函数小模块，禁止跨层耦合 |
| 5 | 专家/技能包/市场三层生态 + 包内置资源 | 升级耦合、体量膨胀 | `packs/` + `team/` 目录外置，判据入 pack.json |
| 6 | desktop/mobile 远控（ADB、H.264、输入注入） | 安全面大、与主线无关 | 不做 |
| 7 | 无 RLS 的"多租户"（owner 列 + 187 处 require_permission 断言） | 隔离靠应用层，易漏 | M3 前明确单用户；真多租户必须 DB 层 tenant_id/RLS |
| 8 | `thread_messages` 与 `trajectory_events` 双份消息体（5496 vs 1445 行） | 存储冗余 | 只留 events 真值 + 投影，投影可重建 |

**对 v1 的修订（据此）**：① §3.1/§3.2 补「投影写失败 warn、可重放重建」「断线不取消」；② §3.3 的 gate 必须落盘持久化（事件级 HITL），不得用进程内存；③ §4 任务栏 = 服务端读模型聚合，前端不做状态拼接；④ §8/§9：M2 若引入 DB 必须 tenant_id/RLS，不复制 55-router 单体与三层生态。
