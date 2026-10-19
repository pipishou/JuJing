# server/ — 机器人团队本地服务

零依赖（仅 Python 标准库）的本地 HTTP 服务，为 `ui/index.html` 提供真数据接口，并托管 UI 静态资源。
默认监听 `127.0.0.1:8787`，只绑定本机回环地址。

## 启动

```powershell
# 在仓库根 JuJing/bot-team 下
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py

# 换端口 / 指定仓库根
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py --port 8788 --root "D:\work\产品类\个人产品\bot-2\JuJing\bot-team"
```

- `--host`（默认 `127.0.0.1`）、`--port`（默认 `8787`）、`--root`（默认脚本上级目录）均可配置；
  也可用环境变量 `BOT_HOST` / `BOT_PORT` / `BOT_ROOT`。
- 启动后浏览器打开 `http://127.0.0.1:8787/`。页面会先探测 `/api/health`：
  成功 → 真数据模式；失败（`file://` 打开、服务未启动）→ 自动退回原有静态演示，不报错。
- 端口被占用时进程以退出码 2 结束并提示换端口，不会半启动。
- 所有子进程都用当前解释器的绝对路径（`sys.executable`）调用 `tools/` 下的脚本。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/`、`/index.html` | 返回 `ui/index.html` |
| GET | `/ui/<path>` | 托管 `ui/` 静态资源（做了目录穿越防护） |
| GET | `/api/health` | `{ok, service, pack, version, industry, root, python, pid, time}` |
| GET | `/api/packs` | 扫描 `packs/*/pack.json`，返回行业包列表 |
| GET | `/api/tasks` | 扫描 `runs/*/task.json`，返回任务摘要（含 state/step/signed 等） |
| POST | `/api/tasks` | 入参为 intake JSON；分配 task_id，写 `runs/<id>/task.json` + `status.json`（state=created） |
| GET | `/api/tasks/<id>` | `{task, report, status, loop, signature, task_id}`，缺失文件为 null |
| POST | `/api/tasks/<id>/run` | 立即 202，后台线程调用 `tools/run_task.py`；已在跑返回 409 |
| GET | `/api/tasks/<id>/log?after=<n>` | 尾随 `run.log`（JSON lines），返回第 n 行（从 1 计）起的 `{n,ts,phase,line}` |
| POST | `/api/tasks/<id>/sign` | body `{signer}`，调用 `tools/sign_delivery.py`，返回 `{sha256,signer,...}` |
| POST | `/api/tasks/<id>/finalize` | 调用 `tools/finalize_delivery.py`；失败返回 409 + 原因 |

统一错误形态：`{"ok": false, "error": "..."}`，配合 4xx/5xx。生产任务写盘均在 `runs/<task_id>/` 下。

## 与 tools/ 的契约

- `POST /run` 会先探测 `tools/run_task.py --help`（按文件 mtime 缓存），
  优先使用新的 `--task-json runs/<id>/task.json --outdir runs/<id>` 入口；
  若当前 `run_task.py` 仍是旧版 CSV 入口，则在能拼出等价参数时回退旧 CLI，
  拼不出（例如没有原始 CSV）时返回 422 并写 `status.json` 的 `failed`，服务不崩。
- `run.log` 与 `status.json` 由 `run_task.py` 单写；服务只读 `run.log`，
  仅在 `/run` 子进程退出后读取 `status.json` 做兜底（异常退出时补写 `failed`），
  以及 `/sign`、`/finalize` 子进程成功后更新状态。
- 签名返回的 `sha256` 与 `tools/sign_delivery.py` 对 `runs/<id>/report.json` 的整文件哈希一致，
  可直接用于验收 A6 的哈希核对。
- 状态机：`POST /run` 不自动签字（除非 intake 里显式给 `signer`），默认停在
  `assembled/awaiting_sign`；`POST /sign` 写 `signature.json` 后状态为 `awaiting_sign/signature_written`；
  `POST /finalize` 通过 `core.gates.check_finalize` 后才是 `signed/finalized` 且 `decision.status=approved`。
- **securityGate 契约缺口（M1 known）**：`assemble_report.py` 对含不可逆动作的报告写
  `security_gate.status="pending_user_confirmation"`，但当前 `core/ tools/` 没有「人工确认 securityGate」
  的 CLI/API 入口；因此 `/finalize` 对这类报告会按设计返回 409（`securityGate 未确认`）。
  只有报告不含不可逆动作、或确认状态已被写入时，`/finalize` 才能 approved。
- demo 报告（`is_demo: true`）签字会被 `sign_delivery.py` 拒绝，`/sign` 返回 400 并透传原因。
- 所有工具脚本缺失或报错时返回明确的 JSON 错误，不抛出裸栈。

## 停止

前台运行按 `Ctrl+C`；后台/隐藏窗口启动时按端口杀进程：

```powershell
Get-NetTCPConnection -LocalPort 8787 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
```

## 自测

```powershell
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py --port 8787  # 后台
Invoke-RestMethod http://127.0.0.1:8787/api/health
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B tests/validate_structure.py
```

UI 侧说明见 `ui/api-adapter.js` 顶部的注释与 `ui/README.md`。
