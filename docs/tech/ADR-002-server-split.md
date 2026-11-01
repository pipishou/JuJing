# ADR-002 服务拆分

- 状态：建议（待用户确认）
- 日期：2026-10-30

## 背景

`server/app.py` 183,574 bytes / 4,063 行 / 142 个函数 / 6 个类 / 16 个 `/api/*` 路径。角色、A/B 路由、插件接入都会继续加逻辑；单体已接近不可维护。

## 决策

按“入口薄、用例厚、适配器隔离”拆成四层，URL 与响应保持不变：

```text
server/
  app.py            # 只做启动/依赖装配
  http/             # 路由表、请求解析、响应/错误格式
  api/              # 按资源分：tasks, workitems, dialog, scenes, reminders, team, packs
  services/         # 用例：TaskService, WorkitemService, DialogService, ReminderService...
  workers/          # 定时提醒、事件轮询、长任务
core/               # 领域对象/规则（eventlog, scene, gates, llm...）
```

规则：

1. 先加“路由契约测试”，再移动代码；每个 PR 行为不变；
2. `api` 不直接读写文件，统一经 `services`；
3. `services` 不依赖 HTTP 对象；入参/出参用 dataclass 或 TypedDict；
4. `workers` 不 import `http`；
5. 旧 URL 保持，新增统一 `/api/v1` 前缀时用兼容层。

## 备选与不选理由

- 微服务：单用户、单机，网络/运维成本无收益；不做。
- 直接重写：29 个测试与现有行为是资产；重写会丢回归保护；不做。

## 后果

- 正面：单文件缩小；角色/插件有落点；测试可按层跑。
- 负面：短期 import 路径变化多；需要 2–3 个 PR 的机械工作。
- 失效边界：拆到 4 层后仍超过 500 行/文件，说明用例边界没切对，回看 ADR。

## 验收

- `server/app.py` < 300 行；
- 所有旧 API 的契约测试绿；
- 任一 `services` 模块可脱离 HTTP 单测；
- `python -m tests.test_mvp_gate --selfcheck` 仍 PASS。
