# 聚鲸飞轮 × Tobs：外挂采集与自优化完整方案 v1.0

> 2026-11-03 ｜ 依据：Tobs 1.6.1 全量代码/文档深读（`@botharness/core` 16.3 万行、`im-provider` 14 adapter、`ui`/`browser`/`computer`/`cloudflared`）＋ `botharness.db` 只读实测 ＋ 聚鲸工作流 v2.3.8 内核（六公理／四齿飞轮／双回流／三硬机检／append-only＋棘轮／五控制点）＋ 军师 D-058/D-059 拍板。
> 用户裁定：**派活归 Tobs；已读不算决策；不自建第二个前端。**

---

## 0 一句话方案

**Tobs 负责「让人和 Bot 干活」，我方负责「把干过的事变成可审计的决策，再让这些决策反过来改 Bot 的行为」。**
飞轮只咬三颗齿：**采集（只读）→ 判断（三锚点）→ 反哺（改配置，不改代码）**。一个字都不写进 Tobs 的库。

---

## 1 边界：谁拥有什么（不可谈判）

| 层 | 归属 | 我方是否碰 |
|---|---|---|
| Bot 身份/人格/记忆仓/SOUL | Tobs（`bot-memory` git 仓，由 Bot 自己提交） | 只读 + 写 SOUL 模块（经 `create`/`memory-save`，不直改库） |
| 群聊 / DM / @ 唤醒 / 派活 / 定时 | Tobs（`bot_schedules`、`wake-policy`、Orchestrator） | 只读采集；提醒**内容**由我方出 |
| IM 通道（14 个 adapter，飞书长连接） | Tobs | 不碰；我方只在需要时用 `lark_send` 作兜底 |
| 审批卡 / 权限 grant / 工具审批 | Tobs（三闸 + Inbox） | 不碰 |
| **决策证据链**（卡 / 封存 sha256 / 签字 / 复核 / 回填） | **我方** | 唯一写入方 |
| **事项账本 + 口径**（`runtime/workitems/current.json`、pack thresholds） | **我方** | 唯一写入方 |
| **上下文检索**（本机索引 + citations） | **我方** | 唯一写入方 |
| **飞轮指标**（N1–N3、M0–M7、命中率） | **我方** | 唯一写入方 |

**红线**：不写 `botharness.db`（单写者 lease，抢到就 `enterRecovery` → 全库只读）；不 patch 插件；不 fork 前端。

---

## 2 架构：三层 + 四闭环

```
┌──────────── 采集层（只读，可随时停）────────────┐
│ C1 DB 快照器  botharness.db ?mode=ro → 每日 jsonl │
│ C2 CLI 抽取器 channel-messages / assignments      │
│ C3 记忆观察器 bot-memory git log（只读）           │
│ C4 我方事件      /api/tasks /reminders /outcome   │
└───────────────────────┬───────────────────────┘
                        │ 归一（白名单 + 去噪 + decision_id）
┌───────────────────────▼───────────────────────┐
│ 判断层（唯一写入，append-only）                    │
│ J1 事件账本 runtime/events.jsonl（bot_team_*）     │
│ J2 决策卡 runs/<task>/report.json + seal          │
│ J3 三锚点：A1 建卡 / A3 签字 / A4 回填             │
└───────────────────────┬───────────────────────┘
                        │ 聚合
┌───────────────────────▼───────────────────────┐
│ 自优化层（只改配置与文本，不改代码）                 │
│ F1 指标台（命中率/采纳率/定案耗时/干扰预算）        │
│ F2 反哺器：SOUL 片段 / pack 阈值 / 提醒策略 / 路由  │
│ F3 双回流：卡（产品）+ 行（经验 WAL）               │
└───────────────────────────────────────────────┘
```

**四闭环**（每个都必须有「判据 + 反证信号」，否则不叫闭环）：

| 闭环 | 入口 | 判断点 | 反哺 | 指标 |
|---|---|---|---|---|
| 决策闭环 | 事项逾期/人工提问 | A1 建卡 → A3 签字 | pack 阈值、评分卡权重 | 采纳率 N1、定案耗时 N3 |
| 回看闭环 | T+7 到期 | A4 回填 observed | 命中率 → 口径校准 | 回看命中率 N2 |
| 提醒闭环 | 每日 `/api/reminders` | 人判「准/不准」 | 白名单、阈值、频次 | 值得率、误报率 |
| 记忆闭环 | Bot 会话结束 | 记忆 git 提交 | 反哺 SOUL 的「教训」段 | 记忆提交率、复犯率 |

---

## 3 采集：采什么、不采什么（白名单）

实测（2026-11-03 09:45）：人类消息 **17** 条 vs 机器消息 **1,204** 条（其中 `group-ordinary` 978、`group-mention` 226），`inbox_admissions` 中 **needs-repair 84**。

| 源 | 是否采 | 理由 | 落地 |
|---|---|---|---|
| `source_events.kind = human-message` | ✅ 采正文 | 唯一真人口径 | `bot_team_human_note` |
| `source_events.kind = assignment-lifecycle` | ✅ 采 | 派活成败证据 | `bot_team_assignment_*` |
| `source_events.kind = memory-change` | ✅ 采元数据 | 记忆在长 | `bot_team_memory_change` |
| `channel_records`（群/DM 元数据） | ✅ 只采 id/成员数/时间 | 不采正文 | `bot_team_channel_seen` |
| 我方 `/api/tasks`、`/sign`、`/outcome` | ✅ 全采 | **判断源** | `bot_team_decision_*` |
| `group-ordinary` 群闲聊 | ❌ 不采正文 | 98.6% 噪音 | 仅计数 |
| `self-record`（Bot 自述） | ❌ 不采 | 自说自话不入证据 | — |
| `inbox_admissions` 的 needs-repair | ⚠️ 只采失败计数 | 采集健康度指标 | `bot_team_inbox_repair` |

**采集实现**：复用已有 `tools/tobs_probe.py`（只读体检）+ `tools/tobs_chat_export.py`（按日 jsonl 导出），新增 `tools/tobs_collect.py`（白名单过滤 + 归一 + 幂等 upsert，按 `event_id` 去重）。

**容错**：DB 被 lease 占用 → 退到 CLI（`deepseekbot channel-messages --limit`）；两者都失败 → 写 `bot_team_collect_failed` 并跳过，**不阻塞**，连续 2 天失败即告警。

---

## 4 判断：三个锚点（已读不算）

| 锚点 | 触发 | 写什么 | 证据要求 |
|---|---|---|---|
| **A1 建卡** | 事项逾期/人工提问 → Orchestrator 派活 → 结论回传 | `runs/<task>/report.json`（卡型 attribution/review/content/weekly）+ 六维评分 + 硬护栏 | 每条 finding 带 factor + data_refs（含 sha256） |
| **A3 签字** | 人点「签」/IM 卡片 | `signature.json`（signer 白名单、字节级 sha256） | 判定必须 `SIGNABLE_VERDICTS=("output",)`；`no_output`/`reference_only` 直接拒签 |
| **A4 回填** | T+7 | `outcome.json`（observed / actual_action / attribution_method） | `observed_at - signed_at ≥ 6d`；只追加，不改已签字节 |

**为什么只认这三个**：飞轮的分母=「有判断的决策」，分子=「回看成立的决策」。已读/回复既无成本也无后果，算进去等于把噪声当样本（D-059 承重追问，用户已裁定不算）。

**decision_id 贯穿**：`task_id` = `decision_id`；事件、卡、签字、回填、采集到的 Tobs 派活记录全部带同一个 id，形成 `事项 → 派活 → 卡 → 签 → 回看` 五段可重放链路。

---

## 5 自优化：反哺只改四样东西

| 反哺对象 | 何时改 | 改什么 | 审批 |
|---|---|---|---|
| **SOUL 片段**（Bot 人格/纪律） | 记忆闭环每周 | 追加「教训 3 行」；淘汰「连续 3 次未命中」的旧规则 | 人审（改动 diff 可见） |
| **pack 阈值/口径** | 回看命中率 <60% 持续 2 周 | `pack.json` 的 thresholds / roi_anchors | 人审 + 版本号 +1 |
| **提醒策略** | 值得率 <50% 持续 1 周 | 白名单、`STALE_DAYS`、每日上限（现 3 条） | 自动（双向门），记 WAL |
| **模型路由** | 某 Bot 连续 2 次判不准 | 该 Bot 单点升 `deepseek-v4-pro`；其余保持 flash | 自动，记 WAL |

**双回流**：每次「卡被签字」或「回看证伪」必须产出一对——① 产品侧卡（含用户可见效果 + 可机判定言）；② 经验侧 WAL 行（带 clause 锚点）。只写 WAL 不写卡 = 教训没变成要修的东西。

**棘轮**：判据只升不降（阈值改动需 `pack` 版本递增 + 账本行），证伪的旧条款进「已知失效」清单，不删。

---

## 6 指标与机检（防"自嗨飞轮"）

### 6.1 指标（每项都有分母，样本不足即 `null`）

| 指标 | 定义 | 判据 | 现状 |
|---|---|---|---|
| N1 采纳率 | 签字通过的建议中，实际执行数 / 总数 | ≥60% | 无数据（outcome=0） |
| N2 回看命中率 | `observed` 与 `expected_result` 方向一致 / 已回看数 | ≥70%，样本 ≥8 才出数 | 无数据 |
| N3 定案耗时 | 首条提醒 → 签字的中位时长 | ≤60s | 上次 13.0s+11.8s 两轮 |
| 提醒值得率 | 判「准」/ 总提醒 | ≥50% | 无数据（`bot_schedules=0`） |
| 记忆提交率 | 有 `memory-change` 的 Bot / 6 | ≥80% | 11 次提交（部分 Bot） |
| 采集健康度 | `needs-repair` 占比 | <2% | 84/1240 ≈ 6.8%（🔴） |

### 6.2 机检（每条都能故意造假被测出来）

| 门 | 负例（必须红） |
|---|---|
| 白名单采集 | 塞一条 `group-ordinary` → 采集器必须拒绝写事件 |
| 判断源唯一 | 伪造「已读」事件 → 不得计入决策数 |
| 签字不可绕 | `verdict=no_output` 或 `reference_only` → `/sign` 必须 409 |
| 回填时间 | `outcome` 在签字后 <6 天 → 409 |
| 样本不足 | 命中率样本 7 条 → 指标必须 `null`，不许出数 |
| 采集故障 | DB 只读打开失败 → 必须写 `bot_team_collect_failed` 且不中断主链 |

---

## 7 实施：六个批次（合计 8–11 人日）

| 批次 | 内容 | 人日 | 出口判据 |
|---|---|---|---|
| **F1 采集归一** | `tools/tobs_collect.py`（白名单 + 幂等 + 容错）+ 6 条负测试 | 1.5 | 连跑 3 天 0 脏事件、needs-repair 不进账本 |
| **F2 三锚点接线** | A1/A3/A4 全写 `bot_team_decision_*`；`decision_id` 贯通 | 1.5 | 一张卡完整链路可 `grep` 出 5 段 |
| **F3 指标台** | `tools/flywheel_report.py`（N1–N3 + 值得率 + 记忆率 + 采集健康度），样本门 | 1.0 | 无数据时全 `null`，有数据时口径可复算 |
| **F4 反哺器** | 四类反哺（SOUL 片段/pack 阈值/提醒策略/模型路由）+ WAL 双回流 | 2.0 | 每次改动留 diff + WAL 行 + 账本行 |
| **F5 Tobs 侧生效** | 补 `assignment` 任务模型；建 2 条 Schedule（08:30 提醒、周一 15:00 周卡）；关遥测 | 1.0 | `bot_schedules=2`、`assignments.assignment` 非空 |
| **F6 跑两周** | 每日 1 次真实使用 + T+7 回看 | 2.0 | N1/N2 出数（样本 ≥8） |

**顺序不可换**：F1 不起，F3 无数据；F2 不起，F3 分子恒 0；F5 不起，F4 的提醒反哺无对象。

---

## 8 验收（V1–V6，全部可机检）

1. **V1** `tobs_collect.py` 连跑 3 天，账本零 `group-ordinary` 正文（grep 断言）。
2. **V2** 任取一张已签卡，能按 `decision_id` 还原「事项→派活→卡→签→回看」五段（脚本可跑）。
3. **V3** `flywheel_report.py` 在 0 数据时输出全 `null`；注水 7 条样本后仍 `null`（样本门生效）。
4. **V4** 反哺器改一次 pack 阈值 → 产生 pack 版本 +1、WAL 行、账本行三件。
5. **V5** `bot_schedules=2` 且 `assignments.assignment` 非空（Tobs 侧真实生效）。
6. **V6** 连续 14 天，每天 ≥1 次真实使用；N1/N2 出数且分母可复算。

---

## 9 反证信号（出现即停改方向）

1. 连续 5 个工作日人类判断事件 <5 条 → 飞轮不成立，降级为「人肉周报」（0.5 人日方案）。
2. `needs-repair` 占比连 3 天 >5% → 采集器不可靠，回退到 CLI-only 采集。
3. Tobs 升级（1.6.x → 1.7）后 schema 变更 → 采集器连续 2 天 0 事件 → 冻结 DB 采集，改 CLI 面。
4. 反哺改 SOUL 后 2 周内命中率无变化 → 反哺无效，改为人工调。
5. 用户连续 3 天不用 → 是需求问题，不是架构建模问题（停止投入）。

---

## 10 不做什么（防膨胀）

1. 不做 Tobs 前端第二套（8787 只作内部工具）。
2. 不做向量库/RAG（BM25 + citations 已够，`40,958` 块 17–69ms）。
3. 不做多 Bot 编排引擎（Tobs 的 L0–L3 + Orchestrator 已覆盖）。
4. 不做自动执行（只到建议 + 签字，`runner=dsh` 恒 501 冻结）。
5. 不采集群闲聊正文、不采集 Bot 自述。

---

## 11 与既有决策的衔接

| 决策 | 关系 |
|---|---|
| D-058 工具服务层 | 本方案 = 该层的**飞轮实现细节**；A1/A3/A4 即其挂载点 |
| D-059 白名单采集 | 本方案 §3 是其落地规格；"已读不算"已并入 §4 |
| D-054 tobs 1.6.1 升级 | 采集需带 schema 探测，防升级断流 |
| M0–M7 判据 | N1–N3 是 M4/M6/M7 的度量扩展，不替代 |

---

## 12 假设账本

- H1：Bot 能被 SOUL 纪律驱动去调本机 API（**未验证**，现状 0 次）→ 由 F2 验收。
- H2：`assignment` 任务模型缺失只影响兜底质量（**部分证伪**：派活成功但无 report）。
- H3：`source_events` 的正文足以支撑回看（人类消息仅 17 条，样本量偏小）。
- H4：反哺 SOUL 能在 1–2 周内改变命中率（**未验证**，Tobs 侧没有 A/B 机制）。

---

## 13 成本

- 钱：0（本机采集 + 本地模型调用按 `opencode-go` 计费，单次决策 ¥0.0056 量级）。
- 时间：8–11 人日，含两周真实跑单。
- 风险：样本量（人类信号 17 条）→ 前两周指标大概率 `null`，这是**正确行为**，不许注水。

---

# v1.1 修订（2026-11-03 用户四条指令）

> 本版**覆盖** §4 锚点表、§7 F5 行、§3 采集源表；其余条款不变。

## 14.1 派活不动（F5 减项）

`assignment` 任务模型**不补、不催**；Tobs 的 L0–L3 派活按现状跑（走 fallback 已能用）。
F5 从「补任务模型 + 建 2 条 Schedule」缩为**一件事**：确认 Bot 能调本机 API（一条 `/api/reminders` 取回即可）。人日 1.0 → **0.5**，总工期 8–11 → **7.5–10.5 人日**。

## 14.2 取消定时任务：改为「你主动发起即触发」

不建 `bot_schedules`（现为 0，保持 0）。提醒的触发源改为：

| 触发源 | 机制 | 延迟 | 需要 Tobs Schedule |
|---|---|---|---|
| **A0 你主动发起**（默认，唯一入口） | 你一说话/建任务 → 工作助理现取 `/api/reminders` → 直接答「今天到期 N 件、最该做的那件」 | 0（同一轮） | ❌ 不需要 |
| 可选的本地日检 | 我方 `tools/daily_check.py` 每天首次打开工作台时算一次（无推送、只有你一进来才看到） | 你登录时 | ❌ 不需要 |

**代价（明写）**：没有「它主动找你」了——你不动，它不响。这与「MVP=你每天主动用一次」的判据一致；等 F6 跑完两周、你确实每天都用，再决定是否加回定时。

## 14.3 产物管理与采集（新增第 5 段：**产物**）

每张卡背后必须有一套产物，且产物本身要进采集——否则飞轮只知道「结论」，不知道「依据」。

**落点约定**（`runs/<task_id>/`）：

```
report.json            # 决策卡（已签字节）
signature.json         # 签字（sha256 针对 report.json）
outcome.json           # T+7 回填
artifacts/             # ← 新增：本次任务的产物
  manifest.json        # 清单：name/type/size/sha256/source/created_at/is_demo
  <原始文件>            # 上传的 xlsx / 截图 / 导出的 md / 生成的图
loop/                  # 四卡
```

**manifest.json 规格**（唯一事实源，机器可读）：

| 字段 | 说明 | 机检 |
|---|---|---|
| `name` | 文件名（不含路径） | 必填、不得含 `..` |
| `type` | `table`/`image`/`doc`/`json`/`log` | 枚举外即红 |
| `size` | 字节 | 与实际文件一致 |
| `sha256` | 内容哈希 | 与文件现算一致（LF 归一） |
| `source` | `upload`/`tool`/`bot`/`api` | 枚举外即红 |
| `created_at` | ISO 时间 | 必填 |
| `is_demo` | 演示件标记 | demo 不得进真实回看 |

**采集**：新增事件 `bot_team_artifact_saved`（每产物一条，带 `task_id`/`path`/`sha256`/`type`/`size`），由 `tools/artifact_manifest.py` 在写盘时发。飞轮据此算三件事：①产物完整率（有 manifest 的卡 / 总卡）②产物复用率（同一 sha256 被 ≥2 任务引用）③证据缺口（finding 引用的 refs 是否都在 manifest 里）。

**归档纪律**：单任务产物目录 >50MB 时只留 manifest + 外部引用路径；产物永不覆盖（同名自动加序号），删除只能整任务删且要留 `bot_team_artifact_purged` 事件。

## 14.4 签字归属：定为「甲＝留我方」

`/sign` + 字节级 sha256 + signer 白名单留在我方（Tobs 审批卡不承担签字语义）。
理由三条（都有证据）：①Tobs 审批卡签的是"允许执行某工具"，不是"认可这张卡的结论"；②我方 `/sign` 已有 100+ 负例与 `SIGNABLE_VERDICTS=("output",)` 白名单；③已签 report.json 字节不变是 T+7 回填的前提（D-019/D-020 已实现）。

## 14.5 修订后的批次与验收

| 批次 | 内容 | 人日 | 出口判据 |
|---|---|---|---|
| F1 | `tobs_collect.py` 白名单采集 + 6 负测试 | 1.5 | 3 天 0 脏事件 |
| F2 | A0/A1/A3/A4 接线，`decision_id` 贯通 | 1.5 | 一张卡 grep 出 6 段链（含产物） |
| F2.5 | **`artifact_manifest.py` + `bot_team_artifact_saved`** | 1.0 | 每卡有 manifest，sha256 可复算 |
| F3 | `flywheel_report.py`（N1–N3 + 值得率 + 产物完整率/复用率） | 1.5 | 无数据全 `null` |
| F4 | 四类反哺 + WAL 双回流 | 2.0 | 每次改动留 diff+WAL+账本 |
| F5 | Tobs 侧最小生效（一条 `/api/reminders` 取回） | 0.5 | 日志有 Bot 调用痕迹 |
| F6 | 跑两周真实单 | 2.0 | N1/N2 出数（样本≥8） |

新增验收：
- **V7** 任取一张卡，`artifacts/manifest.json` 的 sha256 与文件现算一致，且 finding 引用的 refs ⊆ manifest。
- **V8** 故意删一个 manifest 条目 → 产物完整率必须下降、且 `flywheel_report` 报 `artifact_gap > 0`（负例）。
- **V9** `bot_schedules` 恒为 0 时系统仍可用（证明不依赖定时器）。

## 14.6 反证信号补充

6. 连续 3 天「你发起后没有任何一张卡被建」→ A0 通道形式太重伤，改一句话直出结论（砍卡）。
7. 产物 manifest 覆盖率 <80% → 说明采集接线没落到写盘点，回退到「只在交付时扫目录」。
