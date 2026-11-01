# 换电脑继续：从这里开始

- 仓库：`https://github.com/pipishou/JuJing.git`
- 分支：`bot-team`（产品代码线；`main` 是方法论 skill/docs，不含本目录）
- 远程：`origin/bot-team`
- 最后提交：以 `git log -1 --oneline` 为准

## 1. 在新电脑拉代码

```powershell
git clone -b bot-team https://github.com/pipishou/JuJing.git JuJing-bot-team
cd JuJing-bot-team
git log -1 --oneline
```

如果已经有仓库：

```powershell
cd JuJing-bot-team
git fetch origin
git checkout bot-team
git pull --ff-only origin bot-team
```

## 2. 建环境

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install PyYAML==6.0.3
.venv\Scripts\python.exe -c "import yaml; print(yaml.__version__)"
```

要求 Python 3.12+（`server/app.py` 用了 PEP 701 f-string）。

## 3. 启动 8787

```powershell
.venv\Scripts\python.exe -B server/app.py
# 另开一个窗口验证
curl http://127.0.0.1:8787/api/health
```

## 4. 装飞书命令桥（每台电脑一次）

源码在 `integrations/dsh-command-bot-team/`，运行时要装到 DSH desktop profile：

```powershell
$src = ".\integrations\dsh-command-bot-team"
$dst = "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-command-bot-team"
Copy-Item $src $dst -Recurse -Force
```

然后在 `~/.dsh/profiles/desktop/cordis.patch.yml` 末尾加：

```yaml
- id: bot-team-commands
  name: "dsh-command-bot-team"
```

重启 DSH 生效。飞书私聊发 `/today`，能返回今日 3 件即通。

> 飞书 bot 的 owner/allowlist、`appId`、`secretRef` 都是本机配置，不在 git 里。新电脑要重新接入飞书，或从旧电脑迁移 `~/.dsh/integrations/dsh-feishu/`（含凭据，走私密渠道，不要提交）。

## 5. 当前做到哪

- 注意力 OS Slice 1：`runtime/attention/ledger.jsonl` + `GET /api/v1/attention/metrics`
- Slice 2：WIP≤2、`/today`、`/queue`、`/park`、`/resume`、票的硬校验
- Slice 2.5：飞书命令桥（六命令直连 8787，不进模型）
- 文档：`docs/product/`、`docs/tech/`、`docs/ROADMAP_ATTENTION_2026-11-01.md`
- 测试：attention 6 + API 2 + WIP 6 + workitems 44 + `test_mvp_gate.py --selfcheck` PASS

## 6. 下一步（按顺序）

1. 重启 DSH，飞书验证 `/today`、`/queue`、`/bt-metrics`
2. 首单：**个人工作任务管理**，连续 3 个工作日
   - 见 `docs/T0_PERSONAL_WORK_FIRST_ORDER_2026-11-01.md`
   - 账本 ≥4 条真实事项；进行中 ≤2；每天 ≤10 分钟
3. 首单通过后做飞书日报推送
4. 第二场景：品质巡检（`docs/T0_FIRST_ORDER_DATA_REQUEST_2026-11-01.md`）
5. 第三场景：补料率归因

## 7. 不在 git 里的本地资产

以下内容**故意不入库**，明天要单独带走：

| 内容 | 路径 | 处理 |
|---|---|---|
| 项目决策账本/状态 | `.junshi/` | 私密渠道复制；不要提交 |
| 事项账本 | `runtime/workitems/current.json` | 私密复制；或新电脑重新录 |
| 真实数据/签名 | `runs/*/` | 不迁移，除非用户明确 |
| Python 环境 | `.venv/` | 新电脑重建 |
| 飞书凭据 | `~/.dsh/integrations/dsh-feishu/` | 私密迁移；不要提交 |

## 8. 提交前检查

```powershell
git status --short
.venv\Scripts\python.exe tests\test_attention.py
.venv\Scripts\python.exe tests\test_attention_api.py
.venv\Scripts\python.exe tests\test_attention_wip.py
.venv\Scripts\python.exe tests\test_workitems.py
.venv\Scripts\python.exe tests\test_mvp_gate.py --selfcheck
```
