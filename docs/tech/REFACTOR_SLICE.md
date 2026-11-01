# 重构切片 · bot-team 第一版

> 目标：在不改变现有行为的前提下，把 183 KB 单体拆成可维护的服务，并落角色/插件契约。
> 原则：先加护栏，再拆行为；每个 PR 可回滚；URL/报告格式/数据目录不变。

## 0. 基线（每个 PR 前后都要跑）

```bash
cd D:\项目\JuJing-bot-team
python -m tests.test_core_m15
python -m tests.test_m15_acceptance
python -m tests.test_mvp_gate --selfcheck
python -m tests.test_m1_acceptance
```

预期：全 0；`test_core_m15` 188 项；`test_mvp_gate --selfcheck` 1 正控 + 10 负控 PASS。

## 1. 前三个 PR（先做，不改行为）

### PR-1 可复现底座

- 新增：`pyproject.toml`、`requirements.lock`、`scripts/dev.ps1`、`docs/tech/MIGRATION.md`
- 不改：任何 `.py` 行为
- 验证：新建 venv → `scripts/dev.ps1` → 跑基线四条全绿
- 回滚：删新增文件即可
- 退出判据：干净目录 15 分钟内跑通基线

### PR-2 路由契约护栏

- 新增：`tests/test_api_contract.py`（覆盖 16 个现有 `/api/*` 的 method/path/状态码/关键字段）
- 新增：`server/http/router.py`、`server/http/response.py`（只做路由注册/JSON 响应，不移动业务）
- 不改：`server/app.py` 的 URL 与响应格式
- 验证：契约测试 + 基线全绿
- 回滚：app.py 恢复原状，删新文件
- 退出判据：任何路由变更都会让契约测试红

### PR-3 入口瘦身

- 新增：`server/api/{tasks,workitems,dialog,scenes,reminders,team,packs}.py`
- 修改：`server/app.py` 只保留启动/依赖装配/静态资源，< 300 行
- 不改：URL、字段、状态码、文件格式
- 验证：契约测试 + 基线 + 手工 UI 冒烟（建任务/上传/运行/签字）
- 回滚：保留旧 `app.py` 分支 tag，可 revert
- 退出判据：`app.py` < 300 行；所有旧 API 绿

## 2. 后四个 PR（按顺序，可暂停）

### PR-4 服务层

- 新增：`server/services/{task,workitem,dialog,reminder,scene}_service.py`
- 规则：`api` 不直接读写文件；`services` 不 import HTTP
- 验证：服务层单测 + 契约测试
- 退出判据：`api` 层无 `open()/Path.write` 直接调用

### PR-5 角色/插件契约

- 新增：`team/roles/*.yaml` 5 份（4 specialist + lead）、`packs/*/plugin.yaml` 3 份、`core/contracts.py` loader + schema
- 修改：`/api/team`、`/api/packs` 从 YAML 读；UI 5 席位由数据生成
- 验证：契约正/负测试（缺字段/未知字段/越权工具必须拒绝）
- 退出判据：改 YAML 即可换角色/接插件，不改 Python

### PR-6 LLM 网关

- 新增：`core/llm_gateway.py`、`core/providers/{deepseek,fake}.py`
- 修改：`core/llm.py` 与业务调用统一走 gateway
- 验证：fake provider 覆盖成功/超时/非法 JSON/预算超限
- 退出判据：业务不直接 import provider；每次调用有 purpose/model/tokens/cost/prompt_version

### PR-7 状态护栏

- 新增：`core/event_store.py`（单写锁、原子写、快照、坏尾行容忍）、`tools/migrate_state.py`
- 修改：`core/eventlog.py` 走 event_store
- 验证：并发 10 写不丢/不半行；kill 后恢复；迁移脚本副本跑通
- 退出判据：状态可回放，坏尾行不阻塞启动

### PR-8 API v1 + SSE + 访问边界

- 新增：`/api/v1/*` 兼容层、`/api/v1/events/stream`、token 中间件（默认关闭）
- 修改：UI `api-adapter.js` 指向 v1；静态页不变
- 验证：断线重连、401/200、CORS 白名单
- 退出判据：本机可用；非本机默认拒；加 token 后可审计

## 3. 停止条件（任一命中即停，回看 ADR）

- 任一基线测试红，且不是测试本身要更新；
- 旧 URL/字段/报告格式被迫改变；
- `app.py` 拆完后仍 >800 行；
- 角色/插件契约需要改 3 次以上；
- JSONL 出现丢事件/半行且无法用快照恢复；
- 需要引入 FastAPI/React/DB 才能继续（说明切片选错了，不是加框架）。

## 4. 不做

- 不重写 UI；
- 不迁 Octop 主栈；
- 不把 DSH 嵌入产品；
- 不做多租户/插件市场；
- 不在本批改业务逻辑（角色内容可以先占位，但契约/路由先落）。
