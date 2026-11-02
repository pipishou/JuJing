# 注意力控制面：技术方案 v1.0

- 日期：2026-11-01
- 状态：提案（待用户拍板）
- 关系：在 `docs/tech/TECH_ROUTE_REVIEW_2026-10-30.md` 的 A 路线（本地优先模块化）上增量
- 新增 ADR：008 注意力预算、009 Mandate、010 Router–Worker
- 参考：`dsh-superagent-prototype` 的授权/订阅/observe/Router–Worker 分离；**不引入其运行时**

---

## 1. 目标与非目标

### 目标

1. 让 4–5 个并行 AI 任务变成 **1 个注意力队列**，而不是 4–5 个对话窗口。
2. 默认不打扰；只在单向门、被卡住、硬截止、决策就绪时出现。
3. 每个 Work Item 独立 worker 会话，团长只路由，不把全文塞进主上下文。
4. 授权（Mandate）与上下文（Subscription）分离；默认 `observe`。
5. 所有打断/停泊/决策可审计、可回放、可度量。

### 非目标

- 不做通用 Agent 平台、插件市场、多租户
- 不做自主 coding / 自动发布
- 不替换现有 8787 服务、JSONL 事件流、静态 UI
- 不安装 superagent；不升级 DSH（本机 `0.2.0-rc.2`，不满足其版本门）

---

## 2. 架构

```text
来源层
  Feishu / 文件 / 其他 AI 任务 / 日历 / 手动
        │
        ▼
  Source Adapter（归一化为 Trigger）
        │
        ▼
  ┌──────────────────────────────────────────┐
  │ Router（团长 / Attention Router）         │
  │  - 读 WIP、预算、mandate、focus 状态      │
  │  - 决策：ignore / observe / park / route  │
  │  - 不执行具体任务，不读全文日志            │
  └──────────────┬───────────────────────────┘
                 │ route
                 ▼
  ┌──────────────────────────┐      ┌─────────────────────┐
  │ Worker（每个 Work Item） │      │ Attention Ticket    │
  │ 独立会话 / 独立产物       │      │ 1 问 + 选项 + 推荐   │
  │ 返回 6 行 handoff         │      │ + 成本 + 截止        │
  └──────────────┬───────────┘      └──────────┬──────────┘
                 │                              │
                 ▼                              ▼
        Event Log / Snapshot            Feishu / UI 决策队列
                 │                              │
                 └──────────────┬───────────────┘
                                ▼
                    Gate（mandate / attention / delivery）
                                │
                                ▼
                      Decision / Outcome / Ledger
```

### 关键纪律

1. **Router 不干活**：只分类、路由、开票、收口；单次路由输出 ≤30 字。
2. **Worker 不打扰用户**：只回传六行；需要用户时交给 Router 开票。
3. **上下文单向流动**：Worker → Router 只传 handoff；Router 不把全量上下文回灌 Worker。
4. **授权独立**：Subscription 只提供上下文，不产生执行权；执行必须查 Mandate。
5. **默认 observe**：新来源/新任务/群聊先记录“本来会做什么”，不直接执行。

---

## 3. 领域对象

### 3.1 Work Item

```json
{
  "id": "WI-2026-1101-001",
  "title": "品质巡检退货率异常归因",
  "source": "feishu|file|other_ai|manual",
  "status": "parked|active|waiting|blocked|done",
  "wip_slot": 1,
  "impact": 4,
  "urgency": 3,
  "reversibility": "reversible|one_way",
  "owner": "user|worker:quality",
  "mandate_id": "MD-2026-1101-01",
  "next_action": "等待用户确认数据窗口",
  "next_review_at": "2026-11-02T09:30:00+08:00",
  "attention_cost_estimate_min": 5,
  "artifacts": ["runs/T0-.../report.json"],
  "created_at": "2026-11-01T09:00:00+08:00",
  "updated_at": "2026-11-01T09:20:00+08:00"
}
```

状态机：

```text
parked ──resume──▶ active ──wait_user──▶ waiting ──decided──▶ active
  ▲                  │                    │
  └──── park ────────┘                    └── timeout ──▶ parked
active ──worker_done──▶ done
active ──blocked──▶ blocked ──resolved──▶ active
```

### 3.2 Attention Ticket

```json
{
  "id": "AT-2026-1101-003",
  "work_item_id": "WI-2026-1101-001",
  "kind": "decision|input|blocked|review",
  "question": "先查机台还是先查产品型号？",
  "options": [
    {"id": "A", "label": "先查机台", "cost": "2 天", "risk": "覆盖窄"},
    {"id": "B", "label": "先查型号", "cost": "3 天", "risk": "证据弱"}
  ],
  "recommendation": "A",
  "cost_if_wrong": "低：可回头查型号",
  "deadline": "2026-11-01T18:00:00+08:00",
  "status": "open|deferred|snoozed|decided|dropped|suppressed",
  "created_at": "2026-11-01T10:00:00+08:00"
}
```

硬约束：**一问、2–3 选项、有推荐、有截止、有反悔代价**；缺任一不得开票。

#### 3.2.1 票状态机与迁移表（2026-11-02 P0-1 定稿）

实现：`core/tickets.py`；事实源为 append-only 的 `runtime/attention/tickets.jsonl`，
`list_tickets()` 按事件折叠出当前状态。`suppressed` 不是持久票状态，而是
配额/focus 命中时返回的草稿（见 3.2.2），不进入待裁决队列。

| 当前状态 | 动作（HTTP） | 新状态 | 账本事件 |
|---|---|---|---|
| open | `defer {until}` → 200 | deferred | `attention_ticket_deferred` |
| open / deferred / snoozed | `decide {choice,note}` → 200 | decided | `attention_ticket_decided` |
| open / deferred | `snooze {until?}`（默认 +1h）→ 200 | snoozed | `attention_ticket_snoozed` |
| deferred / snoozed | `resume` → 200 | open | `attention_ticket_resumed` |
| open / deferred / snoozed | `drop {reason}` → 200 | dropped | `attention_ticket_dropped` |
| decided / dropped | 任意动作 → **409** | 不变 | 不写（返回明确原因） |

- `decide` 必须能吃掉 `deferred`/`snoozed`：票推迟后仍可直接裁决，不必先 `resume`。
- `open` 是唯一“待用户裁决”状态；`/today` 的 `tickets` 只列 open，
  `tickets_deferred` 额外列 deferred+snoozed（摘要一行，不展开）。
- 非法迁移一律 `TicketConflict`（HTTP 409），消息含当前状态与允许状态，
  例如 `票 AT-… 状态是 decided，不允许裁决（允许：open/deferred/snoozed）；decided/dropped 是终态`。

#### 3.2.2 每日配额、one_way 与 focus

- 配额：同一 `created_by`（agent）+ 本地日期最多 `DAILY_INTERRUPT_LIMIT=3` 次普通打断，
  从票账本 `attention_ticket_opened` 计数；第 4 张不静默丢弃，写
  `attention_ticket_suppressed`（含 `reason=daily_quota`、`used/limit`、票草稿），
  响应 `200 {suppressed:true, reason, detail}`，并在 `/today` 的
  `suppressed_today` 与 brief 中留一行摘要。
- `one_way=true`（单向门）不计数、不抑制；仍需完整票字段。
- focus：同一时刻只允许 1 个 focus item（`runtime/attention/focus.json`）。
  已 focus 时再开 focus → 409；显式 `override=true` 才覆盖并记
  `attention_focus_started`（`override=true`、`previous_item`）。focus 期间普通票被抑制（`reason=focus_active`），
  `one_way` 仍可穿透。`DELETE /api/v1/attention/focus` 或到 `ends_at` 自动结束。

### 3.3 Mandate

```json
{
  "id": "MD-2026-1101-01",
  "sender": "ou_8627598b3bf0276dbdd86b4f4c0ced1f",
  "scope": ["WI-2026-1101-001", "WI-2026-1101-002"],
  "strength": "observe|propose|execute_reversible|execute_one_way",
  "policy_version": "attention-os/1.0",
  "expires_at": "2026-11-01T23:59:59+08:00",
  "created_at": "2026-11-01T09:00:00+08:00"
}
```

规则：

- `observe`：只写“would do”，不执行
- `propose`：可准备方案/草案，不落执行
- `execute_reversible`：可执行可逆动作
- `execute_one_way`：必须逐次显式确认，不能批量授予
- 过期/越界/缺字段 → gate 拒绝

### 3.4 Subscription

```json
{
  "work_item_id": "WI-2026-1101-001",
  "context_refs": ["docs/T0...", "runs/.../evidence.json"],
  "summary": "3 行背景 + 证据索引",
  "authority": "none"
}
```

`authority: none` 是字段级不变量：**投递上下文不授予任何权力。**

### 3.5 Attention Event

```json
{
  "event_id": "ae-...",
  "ts": "2026-11-01T10:00:00+08:00",
  "type": "interrupt_issued|suppressed|deferred|parked|resumed|focus_started|focus_ended|ticket_opened|ticket_decided|mandate_issued|mandate_revoked",
  "work_item_id": "WI-...",
  "ticket_id": "AT-...",
  "cost_min": 5,
  "value": 4,
  "reason": "one_way|blocked|deadline|decision_ready",
  "user_rating": "worth_it|not_worth_it|null"
}
```

---

## 4. 模块落点

```text
core/
  attention.py      # AttentionBudget, WipLimit, InterruptScore, QuietHours
  mandate.py        # Mandate dataclass + issue/validate/revoke
  router.py         # Router.route(trigger) -> RouteDecision
  workitems.py      # 新增/封装现有 workitems 逻辑，补 attention 字段
  tickets.py        # AttentionTicket open/decide/defer/expire
  gates.py          # 增加 mandate_gate / attention_gate / wip_gate
server/
  api/attention.py  # /api/v1/attention/*
  api/mandates.py   # /api/v1/mandates/*
  api/workitems.py  # park/resume/defer
workers/
  attention.py      # 定时聚合、队列老化、日报
  dispatch.py       # 每 Work Item 一个 worker 会话
adapters/
  feishu.py         # 薄适配：只做消息归一化/发送，不做路由
runtime/
  attention/budget.json
  attention/ledger.jsonl   # 投影缓存（非事实源，可由 events.jsonl 重建）
  attention/tickets/current.json
  workitems/current.json
```

原则：`core` 不 import HTTP；`server` 不直接写文件；`workers` 不 import `server.http`；适配器只做协议转换。

---

## 5. API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/attention/today` | 今日 3 件事 + WIP + 票 + focus；超过 3 件必给「另有 N 件未显示」；`?include=paused` 追加 paused |
| GET | `/api/v1/attention/queue` | 完整队列（含停泊/等待/老化/paused） |
| GET | `/api/v1/attention/focus` | 当前 focus 状态 |
| POST | `/api/v1/attention/focus` | `{item, minutes: 90, override?}` 开始免打扰；已有 focus 时需 override |
| DELETE | `/api/v1/attention/focus` | 结束免打扰 |
| POST | `/api/v1/workitems/{id}/park` | 停泊，释放 WIP |
| POST | `/api/v1/workitems/{id}/resume` | 恢复；非停泊项（含外部 paused）→ 409 |
| POST | `/api/v1/attention/tickets` | 开票（校验一问/选项/推荐/截止；配额/focus 命中→200 suppressed） |
| POST | `/api/v1/attention/tickets/{id}/decide` | `{choice, note}`；open/deferred/snoozed→decided，终态 409 |
| POST | `/api/v1/attention/tickets/{id}/defer` | 推迟到指定时间（→deferred） |
| POST | `/api/v1/attention/tickets/{id}/snooze` | 稍后（→snoozed，默认 +1h） |
| POST | `/api/v1/attention/tickets/{id}/resume` | deferred/snoozed→open |
| POST | `/api/v1/attention/tickets/{id}/drop` | open/deferred/snoozed→dropped |
| GET | `/api/v1/mandates` | 当前有效授权 |
| POST | `/api/v1/mandates` | 签发 |
| POST | `/api/v1/mandates/{id}/revoke` | 撤销 |
| GET | `/api/v1/attention/metrics` | 打断/决策/切换/节省时间 |

兼容：现有 `/api/tasks`、`/sign`、`/finalize` 不改；新增统一 `/api/v1`，旧路径保留。

---

## 6. Gate 与负测试

### 6.1 `mandate_gate`

动作执行前：

```python
def require_mandate(action, work_item):
    m = find_mandate(work_item.id)
    if not m or m.expired:
        raise GateError("MANDATE_MISSING")
    if action.one_way and m.strength != "execute_one_way":
        raise GateError("ONE_WAY_NOT_AUTHORIZED")
    if action.irreversible and not action.explicit_confirm:
        raise GateError("EXPLICIT_CONFIRM_REQUIRED")
    return m
```

负测试：

- 伪造 `accept` 但无 mandate → 必须红
- mandate 过期 → 必须红
- `execute_reversible` 试图发布/删除 → 必须红
- `execute_one_way` 批量授权 → 必须红

### 6.2 `attention_gate`

开票/打断前：

- 开放票 >3 → 第 4 张进入队列，不发送
- focus 中且非单向门 → 不发
- 票缺 `recommendation`/`deadline`/`options` → 拒绝
- 票含两个问题 → 拒绝
- 无状态变化的“进度汇报” → 拒绝

负测试：

- 第 4 次主动消息 → 被抑制，写 `suppressed`
- focus 内普通票 → 被抑制
- 伪造“单向门”绕过 focus → 需要 `one_way` 字段 + 审计，不能只靠文案

### 6.3 `wip_gate`

- 活跃 Work Item >2 → 新任务只能 `parked`
- 停泊需记 `reason` 和 `resume_at`
- 用户说“全都要” → 返回队列视图，不破 WIP；由用户显式选择停泊对象

### 6.4 `delivery_gate`

保留现有签字/真实数据门；注意力层不降低交付标准。

---

## 7. Router–Worker 协议

### Router 输入

- Trigger：来源、文本、时间、发送者
- 当前 WIP/预算/focus
- 开放票与队列摘要
- 有效 mandate 列表
- Work Item 索引（标题/状态/下次回看，不读全文）

### Router 输出

```json
{
  "decision": "ignore|observe|park|route|ticket",
  "work_item_id": "WI-...",
  "worker": "quality|refill|report|null",
  "ticket_id": "AT-...|null",
  "reason": "one_way|blocked|deadline|decision_ready|wip_full|ambiguous",
  "router_text": "≤30 字"
}
```

### Worker 六行 handoff

```text
目标：
结论：
证据：
风险：
开放问题：
下一步：
```

Router 只读六行，不读 worker 全量日志；需要细节时按引用读取。

### 为什么这样能并行

- 每个 worker 有自己的上下文，不会互相污染
- Router 的上下文只装“索引 + 六行”，不会被 4–5 个任务全文撑爆
- 用户只和 Router 对话，不需要记住每个 worker 的状态

---

## 8. 飞书交互

### 8.1 主动消息

- 每天最多 3 条主动消息（可配置）
- 只发：今日 3 件事、单向门、被卡住、硬截止
- 不发：进度播报、token 消耗、agent 聊天、重复提醒
- 每条主动消息必须是一张票（见 3.2）

### 8.2 命令

`/today`、`/next`、`/queue`、`/focus 90`、`/park <id>`、`/resume <id>`、`/mandate`、`/brief`、`/stop`（当天静默）。

### 8.3 权限

- 当前 owner-only：`allowlist` 仅 `ou_8627...`
- 群聊/非 owner 默认 `observe`，不执行
- 所有命令和授权写审计事件

---

## 9. 实施切片

### Slice 1：账本与基线（0.5–1 天，不改行为）

- `core/attention.py` 数据结构 + `runtime/attention/ledger.jsonl`
- 记录现有对话/提醒/任务切换（只读埋点）
- `/api/v1/attention/metrics` 返回基线

验收：能回答“过去 3 天打断了多少次、你处理了几张票、WIP 峰值多少”。

**状态（2026-11-01）：已实现。** 代码 `core/attention.py`；API `GET /api/v1/attention/metrics`；测试 `tests/test_attention.py`（6 项）+ `tests/test_attention_api.py`（2 项）全 PASS；无历史事件时返回 0 且 `data_quality.history=false`，不伪造基线。
**修订（2026-11-02，P0-4）：** 原实现只写 `ledger.jsonl`、metrics 只读 `events.jsonl`，导致指标恒 0；已改为单一事实源，见 §16。

### Slice 2：WIP + 日报 + 票（1–2 天）

- `wip_gate`、`attention_gate`、`tickets.py`
- `/today`、`/queue`、`/park`、`/resume`
- 飞书日报一条；第 3 个任务强制停泊

验收：4 个任务时，第 3 个进入 parked；日报只列 3 件；票缺推荐被拒。

**状态（2026-11-01）：已实现。** `core/attention.py`（WIP≤2、`today/queue/park/resume/enforce_wip`）、`core/tickets.py`（一问/2–3 选项/推荐/截止硬校验）、`server/app.py` 六个端点；`tests/test_attention_wip.py` 6 项 PASS；手工 8799 调 `/today`、`/queue`、`/tickets` 均 200。飞书日报先以 `/today` 的 `brief` 字段产出，真实推送待 IM 适配器。

### Slice 3：Focus + 降频（0.5–1 天）

- `/focus`、`suppressed` 事件、每日预算
- 连续“不值得”降频

验收：focus 内普通票不发；单向门可穿透；第 4 次打断被抑制。

### Slice 4：Mandate + observe（1–2 天）

- `core/mandate.py` + `mandate_gate`
- 飞书 `/mandate`；默认 observe
- 负测试：伪造 accept/过期/越界

验收：无 mandate 的执行请求必红；owner 可签发当天授权；过期自动失效。

### Slice 5：Router–Worker（2–3 天）

- `core/router.py`、`workers/dispatch.py`
- 每 Work Item 独立 worker 会话；六行 handoff
- Router 不再读全文

验收：4 个任务并行时，Router 上下文仍 < 单任务全文的 1/3（以 token 计量）。

### Slice 6：其他 AI 来源（后续）

- 手动/剪贴板/文件/其他 AI 工具的任务卡导入
- 只做“任务状态同步”，不做通用集成市场

---

## 10. 度量与观测

```text
attention_metrics = {
  wip_peak, interrupts_issued, interrupts_suppressed,
  tickets_opened, tickets_decided, decision_latency_p50,
  defer_count, park_count, queue_age_p95,
  user_rating_worth_it_ratio,
  attention_minutes_saved_self_report
}
```

注意：

- 不能用“消息数”证明价值
- 不能用“任务数”证明价值
- 必须同时看“用户自报负荷”和“事件账本”
- 每周回看一次；连续 3 天负荷 ≥4 分 → 降频

---

## 11. 与 superagent 的边界

官方原型：[`deepseek-ai/dsh-superagent-prototype`](https://github.com/deepseek-ai/dsh-superagent-prototype)，2026-10-31 公开，MIT，研究原型（非正式产品）。其 [000 Philosophy](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/000-philosophy.md) 与 [002 Authorization](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/002-authorization.md) 是本文治理不变量的主要参照；不复刻其运行时。

| 借 | 不借 |
|---|---|
| Mandate 与 Subscription 分离 | 单 GitHub 仓库定位 |
| observe 默认、accept 显式 | tmux / Windows 常驻 |
| code gate + model eval 分离 | danger-full-access 运行 |
| Router 只路由、Worker 独立会话 | 其运行时/依赖 |
| 意图不明静默忽略 | 研究原型的稳定性承诺 |
| 提示词/团队 skill 版本化 | 以 star 数当验证 |

**结论：只借治理不变量，不接运行时。** 本机不满足其 DSH 版本门，升级是单向门；不冒这个险。

---

## 12. 验收

技术验收：

- 6 组负测试全绿（mandate/attention/wip/focus/one-question/no-status-ping）
- 现有 29 个测试不回归
- `/today`、`/queue`、`/park`、`/focus`、`/mandate` 全链可用
- 事件账本 append-only，可回放

产品验收（两周）：

- WIP ≤2；打断 ≤3/天；决策 ≤3/天
- “值得”≥70%；连续 3 天不打开 = 失败
- 至少 1 个真实场景在 ≤3 次打扰内完成
- 用户主观负荷不高于使用前

---

## 13. 风险

| 风险 | 对策 |
|---|---|
| Router 判断错，藏了重要事 | 每日队列可见；停泊老化提示；一键 `/queue all` |
| 用户绕过 WIP | 返回停泊选择，不硬拒；记录破例 |
| 假单向门 | 单向门清单 + 审计 + 用户“不值得”反馈 |
| Mandate 滥用 | 最小 scope、过期、逐次确认、负测试 |
| 指标作弊 | 事件 + 自报 + 周复盘三方对账 |
| 实现过重 | 切片交付；Slice 1 不改行为，先拿基线 |

---

## 14. Backlog（官方 superagent 对照，不阻塞 MVP）

以下三项来自官方原型 2026-10-31 公开版。当前方案已有字段/概念，但缺运行规则；先入 backlog，Slice 1–2 不实现。

### B-01 Policy version + revalidation（P1）

- **现状**：`Mandate.policy_version` 已有字段，但没有“策略变更后旧 Mandate 怎么办”的规则。
- **要补**：owner 白名单、accept mode、规则、scope 模板任一变更 → 递增 `policy_version`；live Ticket 标记 `needs_revalidation` 并通知，**不立即撤销**；下次 resume/execute 前由 `mandate_gate` 重验 sender/scope/strength/rules；失败 → `mandate_revoked`，Ticket 结束，等待中的消息回 Eval。
- **验收**：负测试——策略收紧后旧 Mandate 仍尝试执行 → 必红；重验通过后可继续；变更不影响无关 Ticket。
- **成本/风险**：0.5–1 天；误撤销导致工作中断 → 先标记、后重验，不即时撤销。

### B-02 Follow-up ownership（P1）

- **现状**：`Subscription` 只提供上下文；但一条追问到达时，没有“归哪个 Ticket/Worker”的明确规则，容易回主会话重新排队。
- **要补**：入站消息先分类 `new | followup`；带 `ticket_id` 的 follow-up 直接交给该 Ticket 的 Executor，**跳过 Eval、不产生新 Mandate**；Executor 只能在原 scope 内处理，越界只 handback 一次；主会话只收通知，不接管；用户 `stop` 永远优先。
- **验收**：负测试——follow-up 不能新开 Ticket/新 Mandate；handback 只一次；stop 后消息不再进旧 Ticket。
- **成本/风险**：0.5–1 天；消息归属错 → 保留“退回 Eval”兜底，不静默丢。

### B-03 Panel only-needs-person（P2）

- **现状**：UI 仍以任务列表为主，健康任务也占首屏。
- **要补**：首屏只显示 `needs_person=true` 的项（待决策/被卡/单向门/硬截止）；每项必须有 `why_now` + `action_required`；健康任务折叠到“全部”；无动作项不得进首屏。
- **验收**：注入 1 个健康任务 + 1 个无 `why_now` 的伪项 → 首屏都不出现；`/queue all` 可展开。
- **成本/风险**：0.5 天；藏掉重要但未标记的任务 → 每日队列兜底 + 老化提示。

**排序**：B-01 → B-02 → B-03；都在 Slice 1–2 之后，先用真实首单验证“注意力 ROI”，再补治理。

---

## 15. 飞书命令桥（Slice 2.5，已实现）

**为什么不走模型**：`/today` 是高频、确定性查询；走模型既慢又花 token，还会把“命令”混进对话历史。用 DSH Host command 直接调 8787，命令和结果都不进模型请求。

**实现**：`integrations/dsh-command-bot-team/`（本地 DSH desktop profile 插件，`ctx.commands.register()`）

| 命令 | 调用的 API |
|---|---|
| `/today` | `GET /api/v1/attention/today` |
| `/queue` | `GET /api/v1/attention/queue` |
| `/park <id>` | `POST /api/v1/workitems/<id>/park` |
| `/resume <id>` | `POST /api/v1/workitems/<id>/resume` |
| `/ticket 问题 \| 选项A \| 选项B \| 推荐 \| 截止` | `POST /api/v1/attention/tickets` |
| `/bt-metrics` | `GET /api/v1/attention/metrics?days=3` |

**依赖**：8787 必须在跑；项目 `.venv`（Python 3.12.13 + PyYAML 6.0.3）可用 `.venv\Scripts\python.exe -B server/app.py` 启动。命令处理器 6s 超时，服务不可用时返回明确错误，不静默。

**安装**：插件源在仓库；本机已复制到 `~/.dsh/profiles/desktop/node_modules/dsh-command-bot-team/`，并在 `cordis.patch.yml` 末尾加了 `bot-team-commands` 行。重启 DSH 后生效。飞书原生 `/` 面板只列 dsh-im 自带命令，本插件命令需手动输入，但能执行。

**验证**：`BOT_TEAM_API=http://127.0.0.1:8799 node integrations/dsh-command-bot-team/smoke.mjs` 已 PASS（today/queue/bt-metrics 均返回 success；无真实事件时显示 0，不伪造）。

**边界**：这是本机 owner-only 桥；不解决 8787 常驻（DSH/电脑关机即不可用），也不替代飞书日报推送。日报推送下一步单独做。

---

## 16. 单一事实源与并发写（P0-4 / P0-5，2026-11-02）

### 16.1 账本：events 是事实，ledger 是投影

```text
事实源   runtime/events.jsonl        # core.eventlog.emit() 追加，bot_team_* 前缀
投影缓存 runtime/attention/ledger.jsonl  # 只由事件流重算，可随时删除
```

- attention 动作统一走 `attention.append(...)` → `core.eventlog.emit()` 写 `bot_team_attention_*`（如 `attention_interrupt`、`attention_ticket_opened`、`attention_task_switch`、`attention_snapshot`）。提醒/票/任务状态仍复用 `bot_team_reminder_issued`、`bot_team_item_created`、`bot_team_item_closed`，metrics 按去前缀短名统一口径。
- `metrics()` **只读事件流**，不读 ledger；`data_quality` 标明 `source_of_truth="events"`、`ledger_role="projection"`。
- 重建（任何人、任何机器）：
  - 纯函数：`attention.project_ledger(events)` / `attention.rebuild_ledger(events, write=False)`
  - 落盘：`attention.rebuild_ledger()` 或 `python -B core/attention.py --rebuild --root .`
  - 重建幂等；删掉 ledger 后行内容与删除前逐字节一致（测试 `tests/test_attention_ledger.py`）。
- 测试禁止裸写 `runtime/events.jsonl` 造假：写事件走 `emit()`/`attention.append()`，纯函数测试显式传 events 列表。

### 16.2 并发：文件锁覆盖整个读-改-写

- 锁原语 `core/filelock.py`：Windows `msvcrt.locking(LK_NBLCK)`、POSIX `fcntl.flock(LOCK_EX|LOCK_NB)`；锁文件 `<target>.lock` 常驻，进程崩溃由内核释放；超时抛 `LockTimeout`（绝不静默跳过锁）。
- workitems：`tools/workitems_store.mutate(fn, path)` 在锁内 load→fn→save；封装 `update_locked / add_locked / delete_locked / merge_locked`。`save()` 是乐观写：磁盘 revision ≠ 本进程 load 时 → `StoreLockError`（拒绝覆盖），后台路由一律走 `mutate`。
- 发号：`core/ids._FileLock` 复用同一 `FileLock`，超时转 `IdError`。
- 压测（`tests/test_workitems_concurrency.py`）：20 线程更新 → revision 2..21 全出现、最终 21；20 线程新增 → 20 个唯一 id；8 进程新增 → 8 个唯一 id；20 线程/8 进程发号 → 无重号；占锁时 `update_locked(timeout=0.1)` → `StoreLockError`、`allocate` → `IdError`；裸 `load→save` 并发 → 明确报错而非丢更新。
- 纪律：**不要**在并发路径上手写 `load()` 后直接 `save()`；用 `mutate()`/`*_locked()`。单写者初始化（测试夹具、一次性导入）可直接 `save()`。

