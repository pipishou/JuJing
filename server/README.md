# server/ — 机器人团队本地服务

零依赖（仅 Python 标准库）的本地 HTTP 服务，为 `ui/index.html` 提供真数据接口，并托管 UI 静态资源。
默认监听 `127.0.0.1:8787`，只绑定本机回环地址。M1.5 起新增团队/数据上传/事件流/审批门/runner 适配。

## 启动

```powershell
# 在仓库根 JuJing/bot-team 下
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py

# 换端口 / 指定仓库根 / 指定签字身份白名单
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py `
    --port 8788 --root "D:\work\产品类\个人产品\bot-2\JuJing\bot-team" `
    --owner "老板" --allow-signers "老板,boss"
```

- `--host`（默认 `127.0.0.1`）、`--port`（默认 `8787`）、`--root`（默认脚本上级目录）；
  也可用环境变量 `BOT_HOST` / `BOT_PORT` / `BOT_ROOT`。
- 身份：`--owner`（默认 `老板`）、`--allow-signers`（默认 `老板,boss`，逗号分隔）。
  `/sign`、`/gate`、`/finalize` 的 `signer` / `confirmer` **必须**在 `--allow-signers` 白名单里，
  不走 env 兜底；越权返回 403。响应里带 `identity: {owner, allow_signers, source: "flag"}`。
- 启动后浏览器打开 `http://127.0.0.1:8787/`。页面先探测 `/api/health`：
  成功 → 真数据模式；失败（`file://` 打开、服务未启动）→ 自动退回静态演示，不报错。
- 端口被占用时进程以退出码 2 结束并提示换端口，不会半启动。
- 所有子进程都用当前解释器的绝对路径（`sys.executable`）调用 `tools/` 下的脚本。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/`、`/index.html` | 返回 `ui/index.html` |
| GET | `/ui/<path>` | 托管 `ui/` 静态资源（做了目录穿越防护） |
| GET | `/api/health` | `{ok, service, pack, version, industry, root, python, pid, time, owner, allow_signers, identity, runners}` |
| GET | `/api/packs` | 扫描 `packs/*/pack.json`，返回行业包列表 |
| GET | `/api/team` | `core.team.load_team(root)`：`{ok, team_id, employees, crews, shelf, roster, pack, warnings}`；core 缺失/读失败 → 500 `{ok:false,error}` |
| GET | `/api/tasks` | 扫描 `runs/*/task.json`，返回任务摘要数组（含 state/step/signed/runner 等） |
| POST | `/api/tasks` | intake JSON（可含 `data_files`）；分配 task_id（201），写 `runs/<id>/task.json` + `status.json`（state=created），数据落 `runs/<id>/data/` |
| POST | `/api/tasks/<id>/data` | 追加数据文件（同校验），返回 `{ok,files:[{name,sha256,size,path}],data_files,data_ready}` |
| GET | `/api/tasks/<id>` | `{task, report, status, loop, signature, gate, task_id, identity}`，缺失文件为 null |
| GET | `/api/tasks/<id>/events?since=n[&stream=1]` | JSON 增量事件数组 / SSE（`text/event-stream`，`id: <seq>` + `data: <json>`，15s `: ping`，`since` 续传，非法 since → 400） |
| POST | `/api/tasks/<id>/run` | body `{runner}`：`fixture`(默认)/`external`/`dsh`；立即 202，后台线程调用 `tools/run_task.py`；已在跑 409 |
| GET | `/api/tasks/<id>/log?after=<n>` | 尾随 `run.log`（JSON lines），返回第 n 行（从 1 计）起的 `{n,ts,phase,line}` |
| POST | `/api/tasks/<id>/gate` | body `{action:"confirm"|"reject", confirmer, comment}` → `core.gate.confirm/reject`，返回 gate 对象 |
| POST | `/api/tasks/<id>/sign` | body `{signer}`，调用 `tools/sign_delivery.py`，返回 `{sha256,signer,signed_at,path}` |
| POST | `/api/tasks/<id>/finalize` | 调 `tools/finalize_delivery.py --allow-signers <白名单>`；拒绝返回 409 + 可读原因 |

统一错误形态：`{"ok": false, "error": "..."}`，配合 4xx/5xx（400 参数/穿越/超限，403 越权身份，
404 未知任务，409 状态冲突/审批门未过，413 body 过大，422 任务缺字段，501 dsh 未实现，503 工具未落盘）。
所有写盘走 `core/paths.safe_*` / 原子写，任务数据只在 `runs/<task_id>/` 下。

### 数据文件上传（`data_files` / `files`）

- `[{name, content, encoding?}]`：`content` 为纯文本（`encoding:"text"/"utf-8"`）或
  `encoding:"base64"`（或 `base64:true`）。
- 限额：**单文件 ≤ 8MB，总量 ≤ 24MB**，超限 → 400（附具体 bytes）。
- 文件名经 `core.paths.safe_relative` 净化：绝对路径、盘符、`..`、控制字符 → 400。
- 每个文件必须能 `json.loads` 且通过 T/T30 schema 校验（`meta.field_contract`、`meta.schema_version`、
  `records` 非空），否则 400；通过后写 `runs/<id>/data/<name>`，sha256 记入 `task.data_manifest`。

### runner 的诚实边界（A12）

- `fixture`（默认）：把 `runs/demo-result-2026-10-19.json` 拷成 `outdir/result.json`，再以 `--demo`
  跑 `run_task.py`；报告 `is_demo=true`、页面标注「演示数据」。**fixture 结果永远不得标成真实交付**，
  `/finalize` 会被 `core.gates` 以「demo 报告/签名不得作为真实交付」拒绝（409，预期行为）。
- `external`：服务端先把状态置 `running/waiting_result`，等外部执行者写出 `outdir/result.json`
  （最长 600s），然后以真实模式（`--real`）跑 `run_task.py`；超时 → `failed` 并带原因。
  真实数据一律走 `data` + `external`。
- `dsh`：本批只占位，返回 501 +「M2 实现」说明。

### 事件流（`/events`）

- JSON：`?since=n` 返回 `seq > n` 的事件数组（元素含 `seq, ts, phase, role, agent, kind, line, meta?`）。
- SSE：`?stream=1&since=n`，首帧 `: connected`，每事件 `id: <seq>` / `data: <json>`，
  空闲每 15s 一行 `: ping`；`since` 严格校验（非整数/负数 → 400）。
- 数据源：`core.events.iter_events(run.log, since)`；core 缺失时退回解析 `run.log`。

## 与 tools/ 的契约

- `POST /run` 会先探测 `tools/run_task.py --help`（按文件 mtime 缓存），
  优先使用 `--task-json runs/<id>/task.json --outdir runs/<id>` 入口；
  若仍是旧版 CSV 入口，则在能拼出等价参数时回退旧 CLI，拼不出返回 422 并写 `failed`。
- `run.log` 与 `status.json` 由 `run_task.py` 单写；服务只读 `run.log`，
  仅在 `/run` 子进程退出后读 `status.json` 做兜底（异常退出补写 `failed`），
  以及 `/sign`、`/finalize` 成功后更新状态。server **不双写** run.log。
- 状态机：`POST /run` 交给工具跑到 `assembled`（服务端不代签，避免不可逆 gate 把任务标 failed）；
  `POST /sign` → `awaiting_sign/signature_written`；`POST /gate confirm` 写 `gate.json` 并更新报告
  `security_gate.status=confirmed`；`POST /finalize` 通过 `core.gates.check_finalize` 后 → `signed/finalized`、
  `decision.status=approved`、`approval.by` 即签名人。
- 签名返回的 `sha256` 与 `tools/sign_delivery.py` 对 `runs/<id>/report.json` 的整文件哈希一致（A6）。
- demo 报告签字：`/sign` 对 `is_demo=true` 报告自动加 `--allow-demo`（可练签留痕）；
  但 `core.gates` 仍拒其 finalize，不会把演示结果当真实交付。
- 所有工具脚本缺失或报错时返回明确 JSON 错误，不抛裸栈。

## 停止

前台运行按 `Ctrl+C`；后台/隐藏窗口启动时按端口杀进程：

```powershell
Get-NetTCPConnection -LocalPort 8787 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
```

## 自测

```powershell
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py --port 8787  # 后台
Invoke-RestMethod http://127.0.0.1:8787/api/health
Invoke-RestMethod http://127.0.0.1:8787/api/team
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B tests/validate_structure.py
```

UI 侧说明见 `ui/api-adapter.js` 顶部的注释与 `ui/README.md`。
