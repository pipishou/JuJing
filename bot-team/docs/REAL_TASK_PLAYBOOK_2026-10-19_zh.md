# 真实任务开跑手册（2026-10-19）

> 用途：明天用真实工厂数据跑第一次"补料率归因"。本手册只写**可执行步骤 + 判据 + 止损**，不写愿景。
> 前提：任务包 v0.3.0 已在本机验证；结构/报告校验全绿；`orchestration/team-workflow.mjs` 支持 `args.data_dir`。

## 0. 出发前（10 分钟）

1. 拿到数据文件（Excel/CSV/导出均可），确认至少有：日期、产线、补料率（或可算补料率的分子/分母）、产量。
2. 确认时间窗：T（异常窗口，如近 7 天）与 T-30（基线窗口，如前 30 天）。
3. 确认联系人：数据负责人、产线负责人（G0 要签字确认口径）。
4. 在 `index.html` 点「新建任务」生成 `task-*.json`（或手工复制 `templates/task-intake.example.json`）。
5. 记录本次 DSH 会话日志路径（回放与审计用）。

## 1. 数据合同审计（不过不跑）

按 `team/employees.yaml` 的 11 因素逐项标注 `available / partial / missing`：

| 因素 | 最低要求 | 缺失处置 |
|---|---|---|
| 日期/产线 | 完整、连续 | 缺失即停 |
| 补料率 | T 与 T-30 可算 | 缺失即停 |
| 产量 | 日级 | partial 可跑，报告标注 |
| 模具状态 | 至少主模 | partial → 进 cannot_attribute |
| 保养记录 | 上次保养日期/周期 | missing → 三级降级"暂停并说明" |
| 班次/班组 | 日级 | partial 可跑 |
| 物料批次 | 批次号 | missing → 不归因物料 |
| 环境（温湿度） | 日级 | partial 可跑 |
| 换模记录 | 时间/模具 | missing → 不进排除 |
| 停机记录 | 时长/原因 | missing → ROI 不算停机 |
| 上报人 | 是否换人 | 换人必须写进 cannot_attribute |

**判据**：11 项中 `missing ≤ 2` 且核心三项（补料率/模具/保养）不缺失，才允许进归因；否则按 `assets/degradation_rules.yaml` 降级并写清原因。

## 2. 生成 T / T-30 JSON

复制 `demo/material_refill_T.json` 的结构，把 `records` 换成真实日级记录；字段名不要改（工作流按固定字段读）。建议：

```text
runs/task-20261019-B/
├─ material_refill_T.json
├─ material_refill_T30.json
├─ task-intake.json
└─ README.md            # 数据来源/口径/缺失项/负责人
```

跑工作流时传：

```json
{ "base": "D:\\项目\\bot\\bot-team-dsh", "data_dir": "D:\\项目\\bot\\bot-team-dsh\\runs\\task-20261019-B" }
```

## 3. G0（人门，不可跳过）

必须由用户确认 3 个字段 + 6 条断言：

- 字段：**对象**（工厂/产线）、**窗口**（T 与 T-30）、**目标**（期望回落值/时限）。
- 断言：① 数据合同已审；② 缺失项已列；③ 不可逆动作只呈请不执行；④ finding ≤2；⑤ cannot_attribute 非空；⑥ 交付后 T+7 回看。

## 4. 跑团队与验收

1. DSH 会话里用 `workflow` 工具执行 `orchestration/team-workflow.mjs`，`args` 带上 `data_dir`。
2. 产物写 `runs/task-.../report.json`，跑：
   ```powershell
   python tests/validate_report.py runs/task-.../report.json
   ```
3. 报告必须含：finding（≤2）+ cannot_attribute（≥1）+ scorecard 六维 + recommendation（含 ROI 三锚点）+ decision_log。
4. 交付卡在 UI 里点「签字确认」；记录签字人与时间。
5. 记 T+7 回看：复查 7 日均值是否 ≤目标；回填 `actual_result`。

## 5. 留下什么（复盘用）

| 证据 | 位置 | 用途 |
|---|---|---|
| 任务 JSON | `runs/task-.../task-intake.json` | 对象/窗口/目标 |
| 数据合同审计 | `runs/task-.../data-contract.md` | 缺失项与降级 |
| 报告 + 校验输出 | `runs/task-.../report.json` + 校验日志 | 交付质量 |
| 决策日志 | `runs/task-.../decision-log.jsonl` | 可追溯 |
| 会话 tape | `runs/task-.../tape.json`（`dsh-session-tape.mjs`） | 过程回放 |
| 用户反馈 | ledger 复盘栏 | 效果与改进 |

## 6. 止损线（触发即停，不硬跑）

- 核心三项数据缺失 → 停，输出"数据不足，需补 X"；
- 数据合同缺失 > 2 项 → 三级降级，只出快照不出归因；
- 归因结果无法排除明显混杂（换人/换料） → 全部进 cannot_attribute，不硬给主因；
- 建议含不可逆动作（换模具/停线） → 只呈请 securityGate，不执行；
- 报告校验 FAIL → 不交付，先修数据或契约。

## 7. 明天要回答的三个问题（复盘）

1. 真实数据下，finding 是否仍然 ≤2 且可解释？
2. cannot_attribute 是否诚实（没有为了好看硬归因）？
3. 老板/产线负责人是否愿意在交付卡上签字？不签的真实原因是什么？

> 备注：以上是"跑一次"的手册；产品化（接单/记忆/成本/多用户）另见 `PRODUCT_BENCHMARK_2026-10-18_zh.md`。
