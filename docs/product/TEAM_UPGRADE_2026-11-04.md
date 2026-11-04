# 团队优化（2026-11-04）

依据：`FLYWHEEL_ROUND1_2026-11-04.md` §R2、`ZERO_PHASE_ROOTCAUSE_2026-11-04.md`（零期五根因）。
原则：**能落到文件的不写成口号；改人格属单向门，先提案后落地。**

## 一、已落地（有文件、有回执）

| # | 改动 | 落点 | 解决了零期哪条根因 |
|---|---|---|---|
| T1 | 回交契约：L0–L3 判级阈值、专家只回六行、定案后必须调 `POST /api/tasks/<id>/deliverable`、未定案不回交、汇总必须写「未采纳谁+为什么」、不写凭据 | `packs/expert_market/manufacturing-advisory-board/agents/jobs.md`（+739 字） | R2「Bot 契约里没有回交指令」 |
| T2 | 专家包同步到运行副本（24 + 15 文件，`[SAME]` 复核） | `.octop/expert_market/*` | 改源不改副本＝改了等于没改 |
| T3 | WAL 首条信号入库（`team.handback.contract`，evidence 指向根因文档） | `runtime/wal.jsonl` | 四齿第一齿：没有信号就没有后续 |

## 二、待落地面（提案已写，未执行）

| # | 提案 | 为什么不当场改 |
|---|---|---|
| T4 | 把 T1 的回交契约追加进 `bot-memory/<5 个 Bot>/bot-*/SOUL.md` | Bot 正在跑任务，此刻写会撞 Tobs 的 CAS 记忆提交（`memory-conflict`），等任务空闲窗口 |
| T5 | 6 个 Bot 补 Task model（`assignment`，现在全为 false，派活走 fallback） | 只能你在 Tobs 面板点：`Bot → Models → 任务模型`，选 `dsh-opencode-go / deepseek-v4.1-flash / high` |
| T6 | 3 类工具审批白名单（本项目目录读写 / 本机 8787 / git 只读） | 同上，面板项；白名单为空时每次调用都会来问你 |

## 三、模型分工（已确认，无需改动）

| 位置 | 模型 | 理由 |
|---|---|---|
| 主模型（orchestrator） | `dsh-opencode-go / deepseek-v4.1-flash / high` | 判级、派活、汇总都在这一层 |
| 任务模型（assignment） | 同上（待 T5 补） | 补上后派活不再走 fallback，费用与行为可预期 |

## 四、团队编制（不变）

5 个 Bot + 1 个默认 Tob：工作助理（团长）/ AI 专家（卡帕西）/ 产品专家（乔布斯）/ 业务专家（稻盛 + 外贸 + 芒格三套底稿，一席三用）/ 内容操盘手（主题蒸馏）。**不加人**——公理 3：编制不产生复利，纪律才产生。

## 五、验收（本条改动怎么算生效）

- T1：Bot 下一次定案后，`runtime/events.jsonl` 出现 `bot_team_card_issued`，且 `runs/<task>/report.json` 存在 → 算回交通
- T2：`sync_expert_pack.py --check` 两包 `[SAME]`
- T3：`flywheel_promote.py summary` 显示 `signals ≥1`
- T5：`tobs_probe.py` 的 `bots[].assignment` 变 `true`
