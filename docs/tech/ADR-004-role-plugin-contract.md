# ADR-004 角色与插件契约

- 状态：建议（待用户确认；**第一优先级**）
- 日期：2026-10-30

## 背景

第一版产品主线是“5 个可见席位 + 可配置角色 + 行业插件”，但当前角色散在 `team/`、`core/team.py`、`packs/*/pack.json`，没有统一契约。先写死 5 个角色，后续换角色/接行业会返工。

## 决策

用 **YAML 声明 + 代码加载 + schema 校验**，角色与插件都不硬编码。

### `team/roles/<id>.yaml` 最小字段

```yaml
id: work-assistant
display_name: 工作助理
seat: specialist          # lead | specialist
responsibilities: [...]   # 一句话职责，3–5 条
inputs: [message, meeting, file, event]
outputs: [workitem, reminder, brief]
tools: [read_workitems, create_workitem, send_reminder]
permissions: [read_local, write_local]
escalation:
  auto: [boss_roi, cross_dept_blocker, irreversible_decision]
  manual: true            # 可被 @团长
model_policy: default
prompt_ref: prompts/work-assistant.md
contract_version: "1.0"
```

### `packs/<id>/plugin.yaml` 最小字段

```yaml
id: manufacturing
name: 制造业内容包
version: 0.1.0
industry: manufacturing
roles: [business-expert]
data_sources: [csv, excel, manual]
metrics: [refill_rate, return_rate]
workflows: [quality-inspection, pmc-scheduling]
assets: [factors, scorecard]
permissions: [read_data, write_report]
compat: {core: ">=1.0 <2.0"}
```

### 事件封套

```json
{
  "schema_version": "1.0",
  "event_id": "evt_...",
  "type": "workitem.created",
  "occurred_at": "2026-10-30T12:00:00+08:00",
  "actor": "work-assistant",
  "subject": {"kind": "workitem", "id": "wi_..."},
  "correlation_id": "corr_...",
  "causation_id": "evt_...",
  "payload": {}
}
```

### 规则

1. 未知字段拒绝（防静默漂移）；缺必填拒绝；
2. `contract_version` 与 core 兼容矩阵校验；
3. 工具权限在加载时校验，越权即拒绝；
4. 团长是 `seat: lead`，不占 specialist；4 个专业角色 + 1 lead；
5. 插件只能声明角色/资产/工作流，不能直接改 core；
6. 契约变更走 ADR + 兼容版本，不静默改字段。

## 备选与不选理由

- 代码里写死 5 角色：最快，但换角色/行业必返工；不选。
- 直接上 JSON Schema + 生成代码：当前规模不值得；先用轻量校验，后续可升级。
- 角色即 prompt：不可编排/不可权限控制；不选。

## 后果

- 正面：角色可换、插件可接、UI 可从 YAML 生成；契约可测试。
- 负面：多一层加载/校验；YAML 写错会在启动时失败（这是好事）。
- 失效边界：角色 >15 或插件 >5 时，考虑注册表/数据库；当前不需要。

## 验收

- 5 个角色 YAML 全部通过 schema；
- 删除必填字段/塞未知字段/越权工具，必须被负测试拒绝；
- 新增一个最小插件（不改 core）能被加载并出现在 `/api/team`；
- UI 的 5 个席位由 YAML 生成，不写死。
