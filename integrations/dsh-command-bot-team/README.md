# dsh-command-bot-team

DSH 斜杠命令桥：把飞书里的 `/today`、`/queue`、`/park`、`/resume`、`/ticket`、`/bt-metrics` 直接转到 bot-team 本地服务 `127.0.0.1:8787`。

命令是 **DSH Host command**，不产生模型消息、不消耗 token；飞书只负责把 `/...` 送进 dsh-im，dsh-im 调 Host 的 `commands.execute()`。

## 前置

8787 必须正在运行：

```powershell
cd D:\项目\JuJing-bot-team
.venv\Scripts\python.exe -B server/app.py
```

项目 `.venv` 已建好（Python 3.12.13 + PyYAML 6.0.3）。

## 安装到本机 DSH desktop profile

插件源在 `integrations/dsh-command-bot-team/`；DSH desktop profile 不通过 CLI 安装，按下面两步：

```powershell
$src = "D:\项目\JuJing-bot-team\integrations\dsh-command-bot-team"
$dst = "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-command-bot-team"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item "$src\package.json","$src\index.js" $dst -Force
```

在 `~/.dsh/profiles/desktop/cordis.patch.yml` 末尾追加：

```yaml
- id: bot-team-commands
  name: "dsh-command-bot-team"
```

重启 DSH 后生效。回滚：删掉上面两行，重启 DSH；插件目录可保留。

## 验证

1. `curl http://127.0.0.1:8787/api/health` 返回 200。
2. 飞书私聊发 `/today`，应返回今日 3 件 + WIP + 待拍板票。
3. 发 `/bt-metrics`，应返回过去 3 天打断/票/WIP 峰值；没有真实事件时显示“暂无真实事件（不伪造）”。

本机可先用 Node 冒烟（不需要 DSH）：

```powershell
$env:BOT_TEAM_API="http://127.0.0.1:8799"
node integrations/dsh-command-bot-team/smoke.mjs
```

## 命令

| 命令 | 作用 |
|---|---|
| `/today` | 今日最多 3 件 + WIP + 待拍板票 |
| `/queue` | 进行中 / 停泊全量 |
| `/park <事项ID>` | 停泊，释放 WIP 槽 |
| `/resume <事项ID>` | 恢复（WIP 已满会拒绝） |
| `/ticket 问题 \| 选项A \| 选项B \| 推荐 \| 截止` | 开一张待拍板票 |
| `/bt-metrics` | 3 天注意力基线 |

注意：飞书原生 `/` 面板只列 dsh-im 自带命令；本插件的命令**手动输入可用**，不保证出现在面板候选里。
