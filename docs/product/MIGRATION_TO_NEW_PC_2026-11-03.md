# 换电脑迁移：专家团 / 记忆 / 项目进度

> 场景：另一台个人电脑**已装好 DSH + Tobs 1.6.1**，要把现在这台的五个 Bot、记忆、账本搬过去。
> 结论先说：**代码与记忆走 git，Bot 身份与模型计划必须在新机重建**——Tobs 的 Bot 定义存在本机 `botharness.db`，不在 git 里。

## 一、三类资产，三种处置

| 资产 | 在哪儿 | 迁移方式 |
|---|---|---|
| 代码 + 文档 + 人格底稿 | `bot-team` 仓（分支 `bot-team`） | `git clone` |
| 项目文档 | `JuJing` 仓（`main`） | `git clone` |
| 五个 Bot 的记忆 | `bot-memory` 私有仓（`assistant/ ai-expert/ product-expert/ business-expert/ content-operator/`） | `git clone` |
| 账本事实源 | `bot-team/runtime/workitems/current.json` | **不在 git**（.gitignore），需手工拷或走 `_state-sync` 仓 |
| 事件流 | `bot-team/runtime/events.jsonl` | 同上（可选，历史证据） |
| 上下文索引 | `bot-team/runtime/context/index.jsonl`（70MB） | **不拷**，新机重建 |
| Bot 身份/模型计划/授权 | `C:\Users\<你>\.dsh\botharness\botharness.db` | **不可搬**，用脚本重建 |
| API Key | DSH 凭据服务 / `bot-team/.env.local` | 手工重填 |
| 飞书 App 凭据 | Tobs 面板 `Settings → IM bots`（本地加密） | 手工重填 |

## 二、新机迁移步骤（按顺序，别跳）

### 0. 先完全退出 DSH
Tobs 的 CLI 是单写者模型，DSH 在跑时任何写命令都返回 `lease-unavailable`。任务管理器确认没有 `DeepSeek Harness.exe`。

### 1. 拉代码
```bash
git clone https://github.com/pipishou/JuJing.git
cd JuJing && git checkout bot-team     # 或 clone 后切分支
git clone https://github.com/pipishou/bot-memory.git
```
> `JuJing` 的 `main` 只有方法论 skill 与 docs；`bot-team` 分支才是产品代码。

### 2. 拷两个事实源（非 git）
```bash
# 旧机
scp runtime/workitems/current.json  <新机>:/path/bot-team/runtime/workitems/
scp runtime/events.jsonl            <新机>:/path/bot-team/runtime/     # 可选
```
`current.json` 是 50 条工作事项的唯一事实源；丢了等于账本归零（M0 判据会红）。

### 3. 重填密钥
```bash
cp .env.example .env.local      # 然后填 BOT_LLM_KEY（opencode-go 的 oc_sk_…）
```
> `.env.local` 已被 .gitignore 排除，**永远不要提交**。

### 4. 重建五个 Bot
```bash
bash tools/dsh_create_bots.sh
```
脚本会读 `packs/expert_market/manufacturing-advisory-board/agents/*.md` 当人格底稿、把 `--memory-location` 指到 `bot-memory/<目录>`、并自动把 `{{占位符}}` 中和成 `【占位符】`（Tobs 只注册 `provider`/`model` 两个变量，带 `{{}}` 会直接报 `unknown prompt variable`）。

若 `BOT_MEMORY_DIR` 不是默认路径，先设环境变量：
```bash
BOT_MEMORY_DIR=/path/bot-memory bash tools/dsh_create_bots.sh
```

### 5. 面板里补三样（脚本管不了）
1. **模型**：每个 Bot 两张卡——主模型 Main + 任务模型 Task，都选 `opencode-go / deepseek-v4.1-flash / high`；分配上限（Assignment concurrency）默认 8。
2. **遥测**：`Settings → 遥测` 关掉（或 `DO_NOT_TRACK=1`）。
3. **飞书**（要用才配）：`Settings → IM bots → Lark/Feishu` 填 App ID/Secret；scope 必须有 `im:message.group_at_msg.include_bot:readonly`、`im:message:readonly`、`im:resource`（**缺 `im:resource` 报 99991672**）；事件 `im.message.receive_v1`。

### 6. 起本机服务
```powershell
python -m server.app --port 8787 --root <bot-team 路径>
```
（或跑 `server/install_services.ps1` 装成 Windows 服务）

### 7. 验收清单（逐条打勾，缺一条不算迁完）
- [ ] `node .../deepseekbot.mjs list` 看到 6 个 Bot（默认 Tob + 五个）
- [ ] `curl 127.0.0.1:8787/api/health` → `pack: workops`
- [ ] `curl 127.0.0.1:8787/api/workitems` → `total: 50`、`overdue_rate` 有数
- [ ] `curl 127.0.0.1:8787/api/reminders` → 有硬提醒
- [ ] 面板里给「工作助理」发一句话，能收到回复
- [ ] `python -B tools/tobs_probe.py --pretty` → `persona_bots: 6`
- [ ] `cd bot-memory && git status` 干净

## 三、之后的日常同步（两台都在用）

| 东西 | 规则 |
|---|---|
| 代码/文档 | 改完 `git push`；另一台 `git pull` |
| 记忆（bot-memory） | 每台机器只由本机 Tobs 写；换机前 `git pull --rebase`，冲突按章节合（按天/按月切分，冲突面很小） |
| 账本（current.json） | **同一时刻只让一台上服务**；另一台只读。两台都要写就得先选主（推荐：固定一台当「账本主」） |
| 事件流 | 同理，别双写 |
| 索引/缓存 | 各机自建，不进 git |

## 四、诚实边界

1. **Bot 身份不能 git 化**：`persona_bots` 在 `botharness.db`，Tobs 没有「导出整个 Bot（含记忆）」的一键命令；`bot-zip-import` 走的是导入通道，导出需从面板走，跨机仍需第 4 步重建。
2. **记忆是「让 Bot 自己写」才长出来的**：`MEMORY.md` 现在只有纪律，`log/` 里要靠每天收尾写 3 行——迁移搬的是记忆文件，不会搬记忆行为。
3. **双机同时跑会抢 SQLite 单写锁**：两台都开 Tobs 且都写记忆，会撞 `memory-conflict`；一台为主。
4. **本次未验证**：`bot-zip` 导入通道未实测；新机脚本未在真实第二台机上跑过（本机只有一台），第 7 节清单是判据不是结果。

## 五、不能全同步时，至少「知悉」（v1 已实现）

两机不共享 Tobs 库，但同一支团队要读到同一份项目上下文 → 用**知悉层**（单向、只读）：

- 主机器跑 `python tools/sync_shared.py`，把「产物索引 + 关键事件时间线 + 项目快照」写进 `bot-memory/_shared/`（`artifacts-index.jsonl` / `timeline.jsonl` / `PROJECT_STATE.md` / `README.md`），然后 `git push`。
- 副机 `git pull` 即可读，**不要手改**（下次生成会覆盖）。`README.md` 只在首次生成，之后不覆盖。
- 只导出索引与 sha256，不含产物正文、不含凭据（`secret/token/password/api_key` 类字段递归丢弃）、不含账本（账本只能一台主）。
- 事件白名单：`card_issued / card_signed / outcome_recorded / artifact_saved / review_recorded / memory_change`；`group-ordinary`、`self-record` 不采。
- 判据：`tests/test_sync_shared.py` 25 项（凭据丢弃、坏行容忍、空输入不炸、README 不覆盖、dry-run 不落盘）。

现状（2026-11-03 首次生成）：产物 0 件、时间线 23 条、出卡/签字/回填各 0——因为 F2 锚点刚接上，真实卡还没跑；数字会随真实使用长。
