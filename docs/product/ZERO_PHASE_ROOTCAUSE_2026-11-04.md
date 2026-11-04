# 零期根因研究：为什么第一轮飞轮全是 0，以及外挂飞轮本身的病灶

> 取证时间 2026-11-04 10:20（本地）。所有数字来自 `runtime/events.jsonl`、`~/.dsh/botharness/botharness.db`（只读）、`runs/*/`、仓库 grep；无推演。

## 一 事实：飞轮四个齿全部空转

| 齿 | 期望事件 | 实测 | 证据 |
|---|---|---|---|
| 出卡（A1） | `bot_team_card_issued` | **0** | `runtime/events.jsonl` 572 条，按 event 统计无此键 |
| 签字（A3） | `bot_team_card_signed` | **0** | 同上 |
| 产物（F2.5） | `bot_team_artifact_saved` | **0** | 同上 |
| 回填（A4） | `bot_team_outcome_recorded` | **0** | 同上 |

572 条事件的真实构成：`msg_bot 372`、`inbound_group_mention 138`、`memory_change 23`、`msg_human 18`、`inbound_human_dm 12`、`reminder_issued 3`、`dialog_turn 2`、`dialog_opened 1`、`dialog_closed 1`、`assignment_stopped 1`、`assignment_lifecycle 1`。
时间跨度 `2026-11-02T17:03` → `2026-11-03T10:07`——**采集齿转得很欢，判断齿一次没动**。

## 二 根因：不是没跑，是门装在没人走的路上

### R1 锚点挂在我方执行器上，真实工作走 Tobs 执行器（结构性）
`core/anchors.card_issued()` 的唯一生产调用点是 `tools/run_task.py`（我方 `/run` 路径）。而 11-03 起用户的活是在 **Tobs 群聊里派给工作助理**，由 Tobs 的 Orchestrator 执行——**它从不经过我方 `/run`**，所以 A1 永不触发。同理 `card_signed` 挂在 `server/app.py` 的 `/sign`；Tobs 侧做完的活不会自己来我方签字，A3 也永不触发。

> 这是"门过宽/门错位"的经典形态（陷阱 #19）：先有门、后有路，路改道了门没跟。

### R2 Bot 契约里没有"回交"这条指令（可通过 grep 证伪）
`packs/expert_market/manufacturing-advisory-board/agents/*.md` 六个角色文件里，`8787`、`/api/`、`report_to_orchestrator`、`回交`、`汇报` 命中数**全为 0**。
→ 即使 Bot 想回交，它的提示词里没有任何人告诉它"找谁、调什么、交什么格式"。**契约缺口，不是模型能力缺口。**

### R3 产物层没有生产调用方
`tools/artifact_manifest.py` 的 `build()` 只出现在它自己的测试与文档里；`server/`、`tools/run_task.py`、`orchestration/` 都没有调用它。
→ 产物清单是"造好了但没接线"的孤岛。这也解释了为什么产物索引 0 件。

### R4 交付物不在 Tobs 的数据面上
`botharness.db` 里能查到消息、inbox 状态、memory 提交、assignment 生命周期，但**查不到 Bot 产出的文件**——产物落在 `primary_cwd`（如 `D:\work\产品类\个人产品\企微+飞书`）的磁盘上，DB 只留 `latest_report_state/summary`（该单三个字段全 `None`）。
→ 我方采集器只采"会话与生命周期"是**必然采不到交付物**的；这不是漏采，是采错了面。

### R5 唯一那条 assignment 还被人中止了
`assignments` 1 条：`title=家居外贸企微+飞书询价机器人可行性评估`、`bot_slug=bot-5f46e53b…`、`stop_state=stopped`、`latest_report_state=None`、创建 00:40:50Z → 更新 00:47:19Z（6 分 29 秒）。
解压对应 session 日志（`~/.dsh/sessions/.../botharness-7606fe1b…/session.v4.jsonl.zstd`，330,504B）得：**160 事件 / 19 步 / 30 次工具调用**，`turn/end = {"kind":"aborted","reason":{"kind":"user"}}`，另有 5 条工具错误（`tool call timed out after 30000ms` ×3、`BotHarness Session file path cannot be resolved` ×2）。
→ 该单**不是失败也不是卡死，是被用户中止在第 19 步**；但两个真缺陷暴露出来：工具 30s 硬超时（公网抓取必超）、Bot 内 `spawn_teammate` 不可用。

## 三 为什么"零期"是必然，而不是运气差

`M0`（账本 ≥40 条）成立，但 `M1`（每天一次对话）、`M4`（每周一卡签字）、`M5`（T+7 回填）全部依赖 A1/A3/A4，而这三个锚点的触发条件在 11-03 之后的生产路径上**一个都不满足**：

```
真实链路：用户 → Tobs 群聊 → Orchestrator 派活 → 子会话干活 → 产物落磁盘 → 结束
我方门：                ↛（无调用）      ↛（无契约）      ↛（无扫描）   ↛（无签字）
```

飞轮不是"没长出来"，是**长在另一条轨上**。先修轨道，别急着加齿。

## 四 外挂飞轮自身的病灶（不只是零期）

1. **观测面选错**：采消息采得再全，也采不到"判断"。判断只发生在 A1/A3/A4——而这三个点全在我方进程内。外挂要成立，观测面必须是**双方都认的事件面**（我方 events + Tobs `assignments` + workspace 磁盘）。
2. **零硬预算 + 无告警**：`issued=0` 只是一个 `null`，没有任何红/告警。飞轮该对"连续 N 天无判断事件"报警，而不是安静地输出空报告。
3. **反哺没有触发源**：反哺要改 SOUL/阈值/提醒策略/模型路由，但它依赖"判断样本"——零期时反哺没有任何输入，于是飞轮退化成"采集器 + 报表"。这是**设计顺序错了**：应先有判断通路，再有反哺。
4. **判据依赖我方 `/sign`，而签字动作可能发生在 Tobs 审批卡**：一旦用户习惯在 Tobs 里点审批，我方 `/sign` 永不触发，M4 恒红——判据绑错了载体（应绑"事实"不绑"我的接口"）。
5. **采集与判断混在一个报告里**：592 条采集 vs 0 条判断出现在同一张周报，读者会误以为"跑起来了"。需要 `signal_quality` 分级（零期/弱信号/可判断），这正是 R1-C 要做的。
6. **单写者与采集的耦合**：采集只读是对的（`?mode=ro`、实测 20 次读最坏 3ms、0 报错），但**写入侧（反哺）与 Tobs 的 CAS 记忆提交会撞**（`memory-conflict`），必须在 Bot 空闲窗口写——这条约束目前只写在文档里，没有机检。

## 五 结论与修订建议（不做硬上限）

**结论**：零期不是"飞轮失败"，是"飞轮的输入口接在了没人经过的管子上"。修三处即可转起来：

- **P0-1 把 A1 挪到 Tobs 事件面**：监听 `assignment-lifecycle`（stopped/completed）与 `inbox` 的 `assignment-report`，**事件到达即尝试建卡**（读 session 日志 / workspace 产物），不再依赖我方 `/run`。
- **P0-2 给 Bot 写回交契约**：`jobs.md` 收尾四步里显式写「调本机 `POST /api/tasks/<id>/deliverable`（curl 一行）+ 附产物路径」，并在 Tobs 侧建一条 Schedule 或 wake-policy 保证收尾必执行。**这是唯一能保证 A1/A3 有输入的动作。**
- **P0-3 产物层接到 workspace**：`artifact_manifest.build()` 改为扫 `primary_cwd`（Tobs 的 workspace），由 P0-1 在建卡时顺带调用。

**对 R1 三批的修订（按你的四条拍板）**：
- R1-A 版式：**去掉 120 字硬上限**，改为"要求"（超长只提示，不拒）；反例仍必红（结构缺失才红）。
- R1-B 交付：**页面内交付卡**（不推 IM），`deliverable.md` + style_lint + 页面渲染三件；推送降级为可选。
- R1-C 零期诚实：周报加 `phase=zero|weak|judgeable` 与"缺哪条通路"的具体原因行，禁止空报告。
- 新增 **R1-D 通路（P0-1/2/3 合并）**：这是本轮真正的"根因修复"，排在 A/B/C 之前或并行。

## 六 诚实边界

- 未验证：Tobs 是否在 `assignment` 结束时自动触发任何外部回调（未见 `assignment-report` 入站记录，`inbox_admissions` 里该 reason 为 0）。
- 未验证：Bot 子会话是否有权限调 `curl 127.0.0.1:8787`（工具白名单/审批策略未实测）。
- 未做：把方案改成代码（本轮只出研究，按你的第 4 条"先不急实施"）。
