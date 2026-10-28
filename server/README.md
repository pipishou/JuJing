# server/ — 机器人团队本地服务

零依赖（仅 Python 标准库）的本地 HTTP 服务，为 `ui/index.html` 提供真数据接口，并托管 UI 静态资源。
默认监听 `127.0.0.1:8787`，只绑定本机回环地址。M1.5 起新增团队/数据上传/事件流/审批门/runner 适配。

## 启动

```powershell
# 在仓库根 bot-team 下
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
- 启动后浏览器打开 `http://127.0.0.1:8787/`（会 302 到 `/ui/index.html`）。
  页面先探测 `/api/health`：成功 → 真数据模式；失败（`file://` 打开、服务未启动）→ 自动退回静态演示，不报错。
- 端口被占用时进程以退出码 2 结束并提示换端口，不会半启动。
- 所有子进程都用当前解释器的绝对路径（`sys.executable`）调用 `tools/` 下的脚本。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 302 → `/ui/index.html`（ui/index.html 用相对路径引同级 JS，在 `/` 下会解析成 `/api-adapter.js` → 404，真数据全断） |
| GET | `/ui/<path>` | 托管 `ui/` 静态资源（做了目录穿越防护） |
| GET | `/api/health` | `{ok, service, pack, version, industry, root, python, pid, time, owner, allow_signers, identity, runners}` |
| GET | `/api/packs` | 扫描 `packs/*/pack.json`，返回行业包列表 |
| GET | `/api/team` | `core.team.load_team(root)`：`{ok, team_id, employees, crews, shelf, roster, pack, warnings}`；core 缺失/读失败 → 500 `{ok:false,error}` |
| GET | `/api/scenes` | 场景种子（`core.scene.load_seeds`；core 缺失时回退 `packs/<pack>/scenes/*.json` 并用 §4.5.3 校验副本）→ `{ok,scenes,pack_id,generated_at,spec_version,contract_errors}`；每个场景附 `latest`（最近任务回看摘要）；`?include_rejected=1` 附带校验被拒的种子文件 |
| POST | `/api/scenes/derive` | 派生场景：body `{prompt?, metric_id?, data_ref?}` 三通道——`metric_id` 命中种子 → `channel:"preset"`；`prompt` → 501（由 M2 DSH 执行器 `s0_scene` 承接，server 不调 LLM）；`discovered` 需 `core.scene.discover`（缺 → 501）；无法派生 → 200 `{ok:false,cannot_derive:[...]}`。body 含 `spec` 对象 → 校验模式（`core.scene.validate`），非法 `spec_version`/缺必填 → 400 |
| GET | `/api/tasks` | 扫描 `runs/*/task.json`，返回任务摘要数组（含 state/step/signed/runner 等） |
| POST | `/api/tasks` | intake JSON（可含 `data_files`）；分配 task_id（201），写 `runs/<id>/task.json` + `status.json`（state=created），数据落 `runs/<id>/data/` |
| POST | `/api/tasks/<id>/data` | 表格模式：multipart/JSON 上传表格 → 转 T/T30（见下节）；兼容模式：追加已合规数据文件（同校验），返回 `{ok,files:[{name,sha256,size,path}],data_files,data_ready}` |
| GET | `/api/tasks/<id>` | `{task, report, status, loop, signature, signature_stale, gate, scene, outcome, outcome_pending, task_id, identity}`，缺失文件为 null；`signature_stale` = 签字指纹是否已与 report.json 现字节脱钩（gate 确认回写报告后为 true → 需重新签字）；DEC 原值在 `report.decision_log_entry`，T+7 回填单独放 `outcome` |
| GET | `/api/tasks/<id>/delivery` | 交付卡字段投影（`finding/scorecard/roi/decision_log/signature`）；`decision_log` = 报告原值，T+7 回填单独放 `outcome/outcome_pending` |
| GET | `/api/tasks/<id>/events?since=n[&stream=1]` | JSON 增量事件数组 / SSE（`text/event-stream`，`id: <seq>` + `data: <json>`，15s `: ping`，`since` 续传，非法 since → 400） |
| POST | `/api/tasks/<id>/run` | body `{runner}`：`fixture`(默认)/`external`/`dsh`；立即 202，后台线程调用 `tools/run_task.py`；已在跑 409 |
| GET | `/api/tasks/<id>/log?after=<n>` | 尾随 `run.log`（JSON lines），返回第 n 行（从 1 计）起的 `{n,ts,phase,line}` |
| POST | `/api/tasks/<id>/gate` | body `{action:"confirm"|"reject", confirmer, comment}` → `core.gate.confirm/reject`，返回 gate 对象 |
| POST | `/api/tasks/<id>/sign` | body `{signer}`，调用 `tools/sign_delivery.py`，返回 `{sha256,signer,signed_at,path}`；securityGate 未确认时附 `gate_pending:true` + `warning`（不阻断签字） |
| POST | `/api/tasks/<id>/finalize` | 调 `tools/finalize_delivery.py --allow-signers <白名单>`；拒绝返回 409 + 可读原因 |
| GET | `/api/tasks/<id>/outcome` | T+7 回填记录 `{ok,outcome_pending,outcome,decision_log_entry}`；未回填时 `outcome_pending=true`、`outcome=null`，`decision_log_entry` 为报告原值 |
| POST | `/api/tasks/<id>/outcome` | body `{observed_outcome, actual_action?, override_reason?, action_cost?, increment_value?, outcome_window?}`；仅 finalized 任务可回填（否则 409），幂等覆盖写 `runs/<id>/outcome.json`，**不改已签字 report.json 字节** |

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

### 上传表格 → 转 T/T30（`table` 模式，B9）

`POST /api/tasks/<id>/data` 有两种 body：

1. **表格模式**（multipart/form-data 或 JSON）：上传业务台账（`.csv/.xlsx/.xls`，≤8MB），服务端
   子进程调用 `tools/ingest_task_data.py` 转成 `material_refill_T.json` / `material_refill_T30.json`。
   **服务不 import pandas**——venv 无需安装任何第三方包（Excel 读取用 venv 自带的 openpyxl/xlrd）。
   字段：`table`（文件，必需）、`field_map`（可选 YAML 文本或文件，≤64KB）、`line`（可选产线号，
   不传则沿用任务创建时的 `line`；两者都没有时返回 400，避免多产线台账混入导致重复日期）、
   `t_start`/`t_end`/`base_start`/`base_end`（必需 ISO 日期 `YYYY-MM-DD`，窗口起点 ≤ 终点）。
2. **数据文件模式**（JSON `{files:[...]}` / `{data_files:[...]}`）：M1 兼容路径，追加已合规的
   T/T30 JSON（见上一节），行为不变。

multipart 示例（Windows `curl.exe`）：

```powershell
curl.exe -X POST "http://127.0.0.1:8787/api/tasks/T-20261020-001/data" `
  -F "table=@D:\data\日台账.csv" `
  -F "field_map=@templates\field-map.example.yaml" `
  -F "line=B" `
  -F "t_start=2026-10-05" -F "t_end=2026-10-11" `
  -F "base_start=2026-09-05" -F "base_end=2026-10-04"
```

JSON 示例（二进制 xlsx/xls 用 base64；纯文本 CSV 也可 `encoding:"text"`）：

```powershell
$body = @{
  table = @{
    name = "日台账.xlsx"
    content = [Convert]::ToBase64String([IO.File]::ReadAllBytes("D:\data\日台账.xlsx"))
    encoding = "base64"
  }
  line = "B"
  t_start = "2026-10-05"; t_end = "2026-10-11"
  base_start = "2026-09-05"; base_end = "2026-10-04"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/tasks/T-20261020-001/data" -Method Post `
  -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

成功返回（`files` 即 `core.provenance.refs_record` 的 sha256+size 清单）：

```json
{"ok": true, "task_id": "T-20261020-001", "t_records": 7, "t30_records": 30,
 "files": [{"path": "runs/T-20261020-001/data/material_refill_T.json", "sha256": "…", "size": 3201},
           {"path": "runs/T-20261020-001/data/material_refill_T30.json", "sha256": "…", "size": 11994}],
 "data_files": ["runs/T-20261020-001/data/material_refill_T.json",
                "runs/T-20261020-001/data/material_refill_T30.json"],
 "data_refs": [{"path": "runs/T-20261020-001/data/material_refill_T.json", "sha256": "…", "size": 3201}, …],
 "data_ready": true, "field_map": "runs/T-20261020-001/data/field-map.yaml",
 "table": {"name": "日台账.csv", "line": "B", "t_start": "2026-10-05", "t_end": "2026-10-11", …}}
```

- 转换结果记入 `task.json` 的 `data_files` / `data_manifest` / `data_refs`（sha256+size），并写
  `status.task.data_ready=true`；之后 `POST /run` 可直接消费这些数据（无需再走 CSV 入口）。
- 失败统一 `{"ok":false,"error":"..."}` + 单行可读原因（缺必需列、窗口内无数据、文件损坏/编码无法
  识别、字段映射 YAML 非法、超时等），**不吐 traceback**；数据级错误 400，工具/环境错误 500。
- 限额：表格 ≤8MB、字段映射 ≤64KB、子进程超时 120s；同一任务重复上传会覆盖同名转换结果并更新 sha256
  （`/data` 在任务 `state=running` 时返回 409，避免运行中替换数据）。

字段映射 YAML 样例（见 `templates/field-map.example.yaml`，可只映射实际用到的列）：

```yaml
version: "1.0"
source_columns:          # canonical 字段 ← 台账里的列名
  date: 日期
  production_line: 产线
  refill_rate: 补料率
  output_volume: 产量
  mold_status: 模具状态
  maintenance_records: 保养记录
  shift_team: 班次
  material_batch: 物料批次
  ambient_env: 环境
  mold_change_records: 换模记录
  downtime_records: 停机记录
  reporter: 填报人
notes: 台账列名可自由命名，只要在 source_columns 里声明映射即可。
```

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

### T+7 回填（`/outcome`，B17）

- 判定：任务必须已 finalized（`status.state=signed` 或 `loop/decision.json` 的 `decision.status=approved`），
  否则 409；未 assembled 无 `report.json` 同样 409。
- 字段：`observed_outcome`（必填，非空字符串，记录 T+7 窗口实测值）以及可选的
  `actual_action / override_reason / action_cost / increment_value / outcome_window`；字段合法性由
  `core.report_contract.validate_outcome` 统一校验，非法 → 400 + 可读原因（不吐 traceback）。
- 落盘：`runs/<id>/outcome.json`（原子写，含 `submitted_at/submitted_by/report_sha256/decision_id/decision_log_entry`），
  重复提交**幂等覆盖**并保留最近 20 次 `history`；`status.json.step=outcome` 记录 sha256。
- 审计：`run.log` 追加一条 `kind=status / phase=followup` 事件（`core.events.append_event`），
  `GET /api/tasks/<id>/events` 可见；事件写入失败不回滚回填，只记日志。
- **不改已签字的 `report.json` 字节**：报告是唯一事实源，回填字段独立存 `outcome.json`，
  API 用 `outcome/outcome_pending` 单独暴露（`GET` 不合并、不伪造 DEC 字段）；`POST /outcome`
  的响应里额外返回合并视图 `decision_log_view(report, outcome)` 供回显，但不回写。
  因此 A6 的签字哈希对 `report.json` 仍然成立；闭环导出仍以 report + outcome 两份事实源为准。
- 闭环匹配：`write_loop_bundle.py` 用 `decision_log_entry.id` 对齐 `loop/decision.json.id`。

### 场景 API（B18，设计文档 §4.5）

- 种子来源：`core.scene.load_seeds(root)`（core 就绪时）；缺失/异常时回退读
  `packs/<pack>/scenes/*.json`，并用 server 内的 §4.5.3 校验副本（`_validate_scene_spec`，
  TODO：与 `core.scene.validate` 合并）逐条校验，被拒条目不进 `scenes`，原因见
  `contract_errors` / `?include_rejected=1` 的 `rejected`。
- `scenes[].latest`：按 `metric_id`/`metric.name` 匹配最近任务，读 `runs/<id>/report.json`、
  `runs/<id>/scene.json`（实例）、`loop/decision.json`、`outcome.json`，返回
  `{task_id,title,state,updated_at,is_demo,has_report,decision_id,verdict,total,
  current_value,control_line,scene_instance,advice{action,irreversibility,roi},
  confidence,cannot_count,next_step,review_due,review_status,actual_outcome,increment_value}`；
  无任务时 `latest=null`，前端显示「暂无归因任务」+ 接入单入口。
- `POST /api/scenes/derive` 状态码纪律：参数非法 400；无法派生（spec 校验不过）→ **200**
  `{ok:false,cannot_derive:[{code,reason}...],rejected:[...]}`（前端回退最近合法 preset 并直说原因）；
  `spec_version` 未知/缺必填 → 400（连 spec 都不合法，不进入派生）；501 只用于未落盘的通道
  （`user_prompt` / `discovered`）；`GET /api/scenes/derive` → 405。
- 任务详情 `GET /api/tasks/<id>` 增 `scene` 字段：`runs/<id>/scene.json` 存在时返回场景实例
  `{scene_id,spec_version,origin,spec?...}`，否则 `null`；前端决策卡优先用实例，缺失时从
  `/api/scenes` 的 spec + 报告字段降级渲染。

## 与 tools/ 的契约

- `POST /run` 会先探测 `tools/run_task.py --help`（按文件 mtime 缓存），
  优先使用 `--task-json runs/<id>/task.json --outdir runs/<id>` 入口；
  若仍是旧版 CSV 入口，则在能拼出等价参数时回退旧 CLI，拼不出返回 422 并写 `failed`。
- `run.log` 与 `status.json` 由 `run_task.py` 单写；服务只读 `run.log`，
  仅在 `/run` 子进程退出后读 `status.json` 做兜底（异常退出补写 `failed`），
  以及 `/sign`、`/finalize` 成功后更新状态。server **不双写** run.log。
- 状态机：`POST /run` 交给工具跑到 `assembled`（服务端不代签，避免不可逆 gate 把任务标 failed）；
  `POST /gate confirm` 写 `gate.json` 并更新报告 `security_gate.status=confirmed`；
  `POST /sign` → `awaiting_sign/signature_written`；`POST /finalize` 通过 `core.gates.check_finalize` 后 →
  `signed/finalized`、`decision.status=approved`、`approval.by` 即签名人。
- **含不可逆动作的报告，顺序固定为 `gate confirm → sign → finalize`**：`/gate confirm` 会回写
  `report.json`（`core/gate.py::_apply_to_report`），报告字节一变，先前签下的指纹即失效，
  `/finalize` 必然报 `signature hash mismatch`。因此 `/sign` **不阻断**——securityGate 未确认时
  仍可签字（A6 契约：含未确认门的真实报告允许签字），但响应体带 `gate_pending: true` + `warning`
  提示「确认门会回写报告使本次指纹失效，届时需重新签字再收口」；fail-closed 发生在 `/finalize`
  （`core.gates.check_finalize` 拒收未确认门的报告）。若历史任务已处于「签字在前」的死局，
  重新 `/sign` 即可恢复（`/finalize` 的 409 原因含恢复指引）。
- `/finalize` 的 409 原因可读：`signature hash mismatch` 会附带「报告在签字后被变更——securityGate
  确认会回写 report.json，使旧指纹失效；请重新 POST /api/tasks/<id>/sign 再收口」。
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
# 上传台账 → T/T30（先 POST /api/tasks 建任务拿 task_id）
curl.exe -X POST "http://127.0.0.1:8787/api/tasks/T-20261020-001/data" `
  -F "table=@D:\data\日台账.csv" -F "line=B" `
  -F "t_start=2026-10-05" -F "t_end=2026-10-11" `
  -F "base_start=2026-09-05" -F "base_end=2026-10-04"
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B tests/validate_structure.py
```

UI 侧说明见 `ui/api-adapter.js` 顶部的注释与 `ui/README.md`。
