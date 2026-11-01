# ADR-009 Mandate 与 Subscription 分离

- 状态：建议（待用户确认）
- 日期：2026-11-01
- 关联：`docs/tech/ATTENTION_ARCHITECTURE_2026-11-01.md`

## 背景

当前飞书 owner-only + `canExecuteCommands=true` 只是粗粒度授权：知道“谁”，不知道“能做什么、做到什么强度、依据哪版规则”。上下文（任务背景、证据、历史）与执行权混在一起，容易越权。

## 决策

引入两个独立对象：

1. **Mandate**：授权凭证，字段 `scope / strength / policy_version / sender / expires_at`。
2. **Subscription**：上下文包，字段 `context_refs / summary / authority: none`。

规则：

- 收到 Subscription 不产生任何执行权；
- 执行前必须查有效 Mandate；
- 默认 `observe`，只写 would-do，不执行；
- `execute_one_way` 必须逐次显式确认，不能批量授予；
- 过期/越界/缺字段 → gate 拒绝。

## 备选与不选理由

- 只靠提示词约束：不可审计、可被绕过；不选。
- 只靠 owner 白名单：无法区分“能看”和“能改”；不选。
- 引入完整 RBAC/ABAC：T1 才需要；当前过重；不选。

## 后果

- 正面：授权可审计、可回放；observe 降低误操作；上下文投递不再等于授权。
- 负面：多一层对象与 UI；用户需要偶尔签发/续期。
- 失效边界：多用户/组织权限时，需扩展角色与策略版本。

## 验收

- 伪造 `accept` 无 mandate → 必须红；
- mandate 过期 → 必须红；
- `execute_reversible` 试图发布/删除 → 必须红；
- `Subscription.authority` 非 `none` → 拒绝。
