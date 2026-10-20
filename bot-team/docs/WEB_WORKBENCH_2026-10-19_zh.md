# Web 工作台（v0.11.2 · legacy 参考）

> legacy 说明（v0.12.0）：v0.12.0 起 CSV/Excel 上传与复核签字已并入主工作台（`server/app.py` + `ui/index.html`）；`tools/task_server.py` + `ui/workbench.html` 保留为 legacy 参考，不再作为主入口。迁移对照见文末「迁移到主工作台」。
>
> 目标：把「真实 CSV/Excel → 数据合同 → 归因报告 → 人工签字 → 交付卡」做成**工作电脑上双击可用**的本地界面，不再只靠命令行。
> 边界：工作台只做 s5（数据收口/报告门/签字），s1–s4 的归因结果仍由 DSH workflow 产出 `workflow-result.json`；不把 Agent 逻辑搬进浏览器。

## 为什么不用 Flask/FastAPI

本机 Python 3.9.7 的 Flask 因 jinja2 版本不兼容无法导入，FastAPI 未安装。工作台只用 **Python 标准库 `http.server` + 原生 JS**，不新增依赖，迁移到工作电脑不会卡在装包。

## 启动（legacy，端口 8765）

> v0.12.0 起日常使用请走主工作台：`python -B server/app.py` → `http://127.0.0.1:8787/`（见 `README.md` 与 `ui/README.md`）。本节仅作 legacy 复现。

```powershell
cd <bot-team 仓库根>
python tools\task_server.py --port 8765
# 打开 http://127.0.0.1:8765/workbench
# 舞台原型：http://127.0.0.1:8765/  （uvicorn 不需要）
```

默认只绑 `127.0.0.1`，**没有鉴权**；不要加 `--host 0.0.0.0` 暴露到局域网/公网。

## 使用顺序

1. **新建任务**：任务 ID、产线、T 窗口、基线窗口、目标/预期、上下文、负责人、签字人。
2. **上传文件**：
   - `CSV / Excel`（必填；11 因素，日期列必须能解析）
   - `字段映射 YAML`（可选；首行/列名不一致时用 `templates/field-map.example.yaml` 改）
   - `workflow-result.json`（必填；DSH workflow 产出的 `{goal, attribution, advice}`）
3. **运行收口**：服务端调用 `tools/run_task.py --defer-sign`，依次 ingest → 合同 → real 报告 → 报告门 → 四卡 → 评分卡；结果写入 `runs/<task-id>/`。
4. **看报告**：交付摘要、归因候选、不可归因/证据缺口、六维评分卡、建议/决策、来源与角色覆盖、运行日志。
5. **签字**：填签字人 → `sign_delivery` → `finalize_delivery`；`decision.status=approved` 后任务锁定，不能换文件/重跑/重复签字。

## 状态机

```
created → uploaded → running → ready → approved
                         ↘ failed（可修文件后重跑）
```

- `ready`：报告已生成、待签字；不能换文件、不能重跑。
- `approved`：已签字；文件/运行/签字全部锁死；要改就新建任务。
- `failed`：保留 `run.log`，修数据/字段映射后可重跑。

## API（本机）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 健康检查、版本、runs 目录 |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks` | 建任务（JSON） |
| GET | `/api/tasks/<id>` | 任务详情（状态/报告/文件/日志） |
| POST | `/api/tasks/<id>/files` | multipart 上传 csv/field_map/workflow_result |
| POST | `/api/tasks/<id>/run` | 运行 `run_task`（同步，300s 超时） |
| GET | `/api/tasks/<id>/report` | 读 `report.json` |
| GET | `/api/tasks/<id>/log` | 读 `run.log` |
| POST | `/api/tasks/<id>/sign` | 签字并 finalize |
| GET | `/api/tasks/<id>/file?name=...` | 下载已上传文件/报告/签名/loop 卡 |

## 安全与数据

- 上传单文件上限 25MB；只接受 csv/xlsx/xls/yaml/yml/json。
- 任务 ID 白名单（字母数字/-/_），路径穿越被拒。
- 所有真实数据只落 `runs/<task-id>/`，已在 `.gitignore` 忽略；**不要 `git add -f`**。
- 服务端没有登录/权限；工作电脑本机使用，或放到受控内网后再加反向代理鉴权。
- `workflow-result.json` 中的结论/分数是 AI 产出；报告门与签字只校验格式/数据/证据，不替业务判断真伪。

## 测试

```powershell
python tests/web_api_smoke.py
# HTTP 建任务 → 上传 → run_task → ready → 签字 approved → 重复操作 409
```

该测试用临时 `runs` 目录，不污染仓库；已纳入 `package.ps1` 前的回归清单。

## 已知边界

- 不执行 s1–s4；没有 `workflow-result.json` 不能出报告（这是有意的：舞台/Agent 与交付口分离）。
- 不做多用户/权限/并发编辑；单机单用户。
- 不做文件在线预览（CSV 大文件不下发浏览器）。
- 签字是「业务负责人确认」的本地留痕，不是法律电子签名。

## 迁移到主工作台（v0.12.0）

| 项 | legacy（v0.11.2） | 主工作台（v0.12.0） |
|---|---|---|
| 服务入口 | `tools/task_server.py` | `server/app.py`（`core/` 可信门 + M1.5 API） |
| 端口 | 8765 | **8787**（`--port` 可配；只绑 `127.0.0.1`） |
| 页面 | `/workbench`（`ui/workbench.html`） | `/`（`ui/index.html` + `ui/api-adapter.js`） |
| 上传 CSV/Excel | 文件上载表单（multipart） | **接单抽屉**（`POST /api/tasks` 或 `/api/tasks/<id>/data`；单文件 ≤8MB、总 ≤24MB） |
| 审批门 | 无 | `POST /api/tasks/<id>/gate`：`gate.json` history append-only + 审计事件（`run.log` kind=status/phase=gate） |
| 签字/收口 | `/sign` | `/sign` + `/finalize`（无 gate 事实源 / 被 reject / 伪造 gate.json 一律 409） |
| 多任务 | 有 | 有（任务栏 + `?task=` 深链） |
| 事件流 | `GET /api/tasks/<id>/log` | `GET /events?since=n`（`&stream=1` SSE，断线按 `since` 续传） |
| runner | 同步 `run_task` | `fixture` / `external` / `dsh`（返回 501，M2 实现） |

迁移步骤：停 8765 服务 → `cd <bot-team 仓库根>` 执行 `python -B server/app.py` → 浏览器打开 `http://127.0.0.1:8787/` → 用接单抽屉重传 T/T30；`runs/<id>/` 历史任务仍会被主工作台扫描到，签字按新审批门流程重走。
