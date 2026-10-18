# 门与纪律（团队运行约束）

## 权限分级（L1）

- 只**读**数据、**算**归因、**写**报告与决策日志；
- 任何**不可逆动作**（停线／改生产参数／外部花费，白名单见 `assets/irreversible_actions.yaml`）只**建议**不执行，且必须过 securityGate 二次确认，确认前状态＝「待确认，不执行」。

## 人工门（两级，硬要求）

1. **预派发确认（G0）**：host 出 DAG 计划后先呈用户确认，点头才派发；
2. **securityGate**：建议含不可逆动作时单独呈请二次确认；被绕过＝本次交付失败，停下修复。

## 降级规则（`assets/degradation_rules.yaml`）

- 空值率 > 10% → 转人工（二级降级）；
- 候选归因 ≥ 2 → 必须补反事实检验；
- 连续 3 次归因失败 → 暂停并说明，不硬编结论。

## 封闭世界（CRP，`assets/prompt_crp.yaml`）

- 只在 `assets/factors_table.yaml` 的 11 类因素内归因；表外问题拒答或转人工，不越界编造。
- 数据缺失写 `missing`，不补数、不插值、不猜。

## 产出契约（s5 必填）

- `finding`：≤2 个候选归因，每个带 confidence + evidence；
- `cannot_attribute`：**强制非空**（数据质量／表外因素／上报口径）；
- `scorecard`：六维（`assets/scorecard_six_dim.yaml`）；
- `recommendation`：action + roi(cost/avoided/anchor) + irreversibility；
- `decision_log_entry`：按 `assets/decision_log.schema.yaml` 四分类 + clause/trace。

## 交付前机检（先于「done」）

```
python tests/validate_structure.py
python tests/validate_report.py runs/demo-report.json
```

两条都非零退出 ⇒ 不得声称交付完成。
