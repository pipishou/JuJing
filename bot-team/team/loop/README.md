# 四段闭环（检索 → 分析 → 决策 → 闭环）

> 定位：把军师相位 0–5 里"想清楚"的部分，固化成四张可机检的卡 + 一条状态机。
> 目的：让每次决策都能回答四个问题——**证据够不够？分析有没有漏？谁拍的板？结果回来了吗？**

## 0. 状态机

```text
draft ──G1 证据门──> researched ──G2 分析门──> analyzed ──G3 决策门──> decided
                                                              │
                                          ┌───────────────────┴──────────────┐
                                          ▼                                  ▼
                                      delivered                        rejected / superseded
                                          │
                                     G4 闭环门（T+7）
                                          ▼
                                       closed
```

- **G1 证据门**：无证据卡不分析；每条承重结论至少 1 张 A/B 级证据。
- **G2 分析门**：选项 ≥2、判据带权重、至少 1 条反面证据、有推翻条件。
- **G3 决策门**：决策记录必须有 `approval`（谁/何时/方式）+ `reversal_conditions` + `review_due`。
- **G4 闭环门**：到期必须回填 `actual_outcome` 与 `verdict`（hit/miss/partial）；未回填不得关单。

## 1. 四张卡

| 卡 | 文件 | 关键字段 | 机检 |
|---|---|---|---|
| 证据卡 | `evidence.jsonl` | `id/query/source/quote/score/grade/freshness_days/stance` | URL 必填、quote ≤200 字、grade∈A/B/C、score≥1.0 |
| 分析卡 | `analysis-*.yaml` | `options/criteria(weight)/weighted_scores/sensitivity/assumptions/disconfirming/premortem` | 选项≥2、权重和≈1、至少 1 条反面证据 |
| 决策记录 | `decisions.jsonl` | `id/question/chosen/confidence/reversibility/approval/reversal_conditions/review_due` | 必带证据/分析 ID、review_due 必填 |
| 闭环记录 | `closures.jsonl` | `decision_id/actual_outcome/verdict/lessons/action` | verdict∈hit/miss/partial；miss 必须写 action |

Schema 见同目录 `*.schema.yaml`；完整示例见 `example-decision.json`。

## 2. 四段怎么做（判据）

### 检索
- 先问"这个结论值得 5 厘钱吗"；能读本地码/文档的不联网。
- 一条 query 一次检索；同 query 走缓存；结果写证据卡（不是复制一段话进正文）。
- 证据分级：A=官方/一手/可复算；B=权威媒体/研究；C=社区/二手。**C 级不能单独承重。**
- 时效：市场类 30 天复核；技术/产品类 90 天；财务/合规类 7 天。

### 分析
- 选项地图：每项写"一句话/证据/成本/风险/可逆性"；至少 2 个选项，含"不做"。
- 判据权重：权重和 = 1.0；加权分只用于排序，不替代判断。
- 敏感性：只改权重最大的 1–2 个判据，结论是否翻转？翻转就写"条件结论"。
- 反面证据 + pre-mortem：假设 6 个月后失败，最可能的 3 个原因。
- 红海/空白判断必须带置信度，禁止把"组合空白"写成定论。

### 决策
- 一条记录只做一个决定；**否决也是结果**，写 `rejected` 与理由。
- 必须写：选了什么、放弃什么、置信度、可逆性、审批人/时间/方式。
- 必须写：**推翻条件**（什么信号出现就重开）与 `review_due`。
- append-only：错账用新记录 `supersedes`，不删旧行。

### 闭环
- 到期扫描：`review_due` 到期的决策先呈现"实际结果如何"；不回填不继续新决策。
- 回填：实际结果、指标、证据、命中/未命中、教训、动作（keep/adjust/retire）。
- 棘轮：hit → 固化为默认判据；miss → 升级为检查项；连续 3 次 miss → 退役该条。
- 指标：命中率、证据覆盖率、决策周期、推翻率、单决策成本。

## 3. 与方法论对齐（AI智能经营模型 v1.3）

| 方法论 | 四段卡落点 | 说明 |
|---|---|---|
| L0 感知 μ±2σ / A·B·C 数据质量 | 证据卡 `data_quality_grade` + `assets/perception_rules.yaml` | 先检测再归因；空值率 10%/30% 三级降级 |
| L1 六步排除 + 六维评分卡 | 分析卡 `attribution_method/degradation_level/scorecard` | 评分卡 100 分制、硬护栏、80/60/40 阈值 |
| L2 建议 + 决策契约 | 分析卡 `decision_contract` + `assets/decision_contract_refill.yaml` | 行动集合/成本/护栏/负责人 |
| 决策日志四组分类 | 决策记录新增 `entity_id/metric/context/recommendation/actual_action/override_reason/outcome_window/action_cost/increment_value/calibration_version` | 不另造日志格式 |
| 评估信息 + 失败模式 + CSOLR | 闭环记录 `increment_value/attribution_method/scorecard_snapshot/failure_mode/calibration_version` | 前 30 条冷启动等权；≥50 条再校准 |

## 4. 与现有团队/ UI 的接法

- 现有舞台 = **过程证据**（谁/何时/做了什么）；四段卡 = **决策证据**（凭什么/选了啥/结果如何）。
- 交付卡增加四个链接：证据卡集、分析卡、决策记录、闭环记录。
- UI 新增四台（P1）：检索台 / 分析台 / 决策台 / 回柜台；先出 JSON 与卡片，不急着做新页面。
- Git 仓库只放 schema/模板/示例；**真实 evidence/decisions/closures 不入公开库**。

## 4. 命令与文件（后续实现）

```text
team/loop/
├─ README.md
├─ evidence-card.schema.yaml
├─ analysis-card.schema.yaml
├─ decision-record.schema.yaml
├─ closure-record.schema.yaml
└─ example-decision.json

# 计划中的执行器（P1）
python tools/loop.py evidence --query "..." --url ... --quote ...
python tools/loop.py analyze --question "..." --options options.yaml
python tools/loop.py decide --analysis A-... --chosen O2 --review-due 2026-10-25
python tools/loop.py close --decision D-... --actual "..." --verdict hit|miss|partial
```
