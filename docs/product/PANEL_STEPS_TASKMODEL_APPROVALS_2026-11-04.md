# 面板操作手册：给 6 个 Bot 补任务模型 + 3 类工具审批白名单

> 2026-11-04 · 只操作 Tobs 面板，不改它的代码与数据库。所有验证都走只读探针。
> 现状（实测）：6 个 Bot（Tob / 工作助理 / AI 专家 / 产品专家 / 业务专家 / 内容操盘手）的
> `modelPlan.assignment` **全部缺失**；`tool_approval_rules` 表 **0 行**。

---

## 一、为什么要补「任务模型」

Tobs 的模型分配是**两张卡**：

| 卡 | 作用 | 现状 |
|---|---|---|
| 主模型 Main / orchestrator | 对话、判级、汇总、派活决策 | 已配 `dsh-opencode-go / deepseek-v4.1-flash / high` |
| 任务模型 Task / assignment | **派出去的子任务用哪个模型跑** | 全缺 → 引擎走 fallback |

后果：主模型和子任务同模型同强度时，成本翻倍且看不出差异；引擎日志里
`assignmentDefault` 为空，派活会走隐性兜底（不可复现）。

---

## 二、操作步骤（每个 Bot 一次，共 6 次）

1. 左侧栏点 Bot 头像 → 进该 Bot 的会话。
2. 右上角 **设置（Settings）** → 左侧分区里找 **模型（Models）**。
3. 该页有两张卡：
   - **主模型 / Main model** —— 已填好，**不要动**。
   - **任务模型 / Task model** —— 空。点它 → 选：
     - Provider：`dsh-opencode-go`
     - Model：`deepseek-v4.1-flash`
     - 思考强度：`high`
4. 保存（Save / Apply）。**不需要重启 DSH**，保存即生效。
5. 6 个 Bot 重复一遍：Tob、工作助理、AI 专家、产品专家、业务专家、内容操盘手。

> 若界面里「任务模型」是 Advanced 下的候选卡列表：把
> `dsh-opencode-go / deepseek-v4.1-flash / high` 加进去并设为默认；
> 候选卡里的「备注」写一句用途（如「日常派活，长任务用」），Orchestrator 会读。

### 验证（只读，不写库）

```bash
python -B JuJing/bot-team/tools/tobs_probe.py --pretty
```

期望：`bots[].assignment` 从 `false` 变 `true`，且 `provider=dsh-opencode-go / model=deepseek-v4.1-flash / reasoning=high`。

---

## 三、3 类工具审批白名单

`tool_approval_rules` 现在是 0 行 ⇒ 每个工具调用都走 Bot 的默认审批策略（`ask`），
人在不在都要过一遍，这就是「任务跑到一半停住」的高发点。

只放行**三类无副作用**的调用，其余保持 `ask`：

| # | 类别 | 允许什么 | 为什么安全 | 拒绝什么 |
|---|---|---|---|---|
| 1 | 本机只读 API | `GET http://127.0.0.1:8787/api/*`（获取工作事项、提醒、候选） | 只读我方服务，不触发写 | `POST` 除 `/api/tasks/*/deliverable` 外一律不放行 |
| 2 | 工作区内文件读 | 读 `bot-team/`、`bot-memory/`、`runs/` 下的文件 | 这些是它自己的工作产物 | 读 `~/.dsh/.credentials.yaml`、`*.key`、`*.pem`、`.env*` |
| 3 | 只读检索 | `web_search` / `web_fetch`（公开页面） | 无本地副作用 | 任何带登录态/内网的抓取 |

### 操作步骤

1. 左侧栏底部 **设置（Settings）**（全局，不是单个 Bot）。
2. 找 **工具审批 / Tool approval** 分区。
3. 新增规则（每条：工具名或前缀 + 允许/拒绝 + 作用域）：
   - 规则 1：工具 `fetch`（或 `http_request`）匹配 `GET http://127.0.0.1:8787/api/` → **Allow**
   - 规则 2：工具 `read/write` 匹配 `bot-team/`、`bot-memory/`、`runs/` → **Allow**
   - 规则 3：工具 `web_search`、`web_fetch` → **Allow**
   - 兜底：其余 → **Ask**（不要改成 Allow all）
4. 若界面只有 Bot 级开关（`browserAccess` / `computerAccess`）而没有逐工具规则：
   把这两个**保持关闭**，改用「人不在时的审批代批」模板（`ask-before-acting`），
   被拒的调用会落进 Bot Inbox，前缀 `autoreject:`，可以一键放行重试。

### 验证

```bash
python -B JuJing/bot-team/tools/tobs_probe.py --pretty   # 看 tool_approval_rules 计数
```

期望：`tool_approval_rules` 从 0 变成 3（或按你实际条数）。

---

## 四、两条纪律

1. **不改 Tobs 代码、不写它的库**。上面全部是面板操作；我方只读它（`?mode=ro`）。
2. 白名单只放「只读」。任何 `POST`、任何写盘、任何浏览器/电脑操作，保持 `ask`。

## 五、做完之后的下一步（我这边）

- R2-C 确认页：8787 前端「候选任务」区，一键是/不是 → 建单出卡。
- R2-D 记忆固化：`tools/memory_commit.py`（30 分钟一次 `commit -am`，不 push）。
- T4 SOUL 追加：等你这批任务空闲窗口再写（避免撞 Tobs 的 CAS 记忆提交）。
