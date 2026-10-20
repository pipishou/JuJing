# M1 构建规格 · 真实一单可信闭环（2026-10-19）

> 项目：`D:\work\产品类\个人产品\bot-2`；代码根：`D:\work\产品类\个人产品\bot-2\JuJing\bot-team`（下称 `$BT`）。
> Python：`D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe`（含 pyyaml，**无 pandas**）。
> 铁律：不改 DSH 宿主；不装依赖；不跑 git；不删 demo 原数据；python 一律 `-B`；只在各自 write scope 内写文件。

## 0. 为什么做（背景，一句话）

四路审计结论：演示链能跑，但真实任务的可信门形同虚设——`finalize_delivery` 可被任意绕过、real 报告可含 demo 占位、`score_scorecard` 零生产调用、满分被判 no_output。**先把门修好，再把真报告接到可视化界面**，否则 UI 接上=产出「假真实报告」。

用户已拍板（`.junshi/ledger.md`）：可视化工作台（非 skill）、通用判据+行业包、制造业真实数据、B→C 路线、先跑通个人 demo。

## 1. 验收判据（A1–A6，第三方可照做）

| # | 判据 | 机检方式 |
|---|---|---|
| A1 | 同日连建 2 个任务：task_id 不同、各自 `runs/<id>/`、重跑不覆盖历史 | `test_m1_acceptance.py` 用例 1 |
| A2 | real 报告含 `{"score":3,"evidence":"demo 占位"}` 或 data_refs 指向不存在文件 → `validate_report --require-real` FAIL、`assemble` 非 0 退出 | 用例 2 |
| A3 | pending securityGate / rejected decision / 伪 signer（签名文件含 report_sha256 但无合法 signer）→ `finalize` 非 0；合法流程 → approved 且 `approval.by` == 传入 signer | 用例 3 |
| A4 | 六维全 5 → total=100.0（不是 100.02）、verdict 命中「最优区间」而非 no_output；`run_task` 产出的 report 含 `scorecard.total`/`verdict`，且与 `score_scorecard` 独立调用一致 | 用例 4 |
| A5 | 改 `packs/manufacturing/pack.json` 的 ROI 锚点 → 新报告 ROI 值随之变化（证明行业包真被消费，不是写死） | 用例 5 |
| A6 | UI 交付卡字段 == `report.json`（finding/scorecard/roi/decision_log），签字哈希 == `sign_delivery.py` 对同一 report.json 的 sha256 | 用例 6 + 人工走查 |

## 2. 目标目录与契约

```
$BT/
  core/                     # 通用件（跨行业，零第三方依赖）
    __init__.py
    ids.py
    provenance.py
    gates.py
    pack.py
    report_contract.py
  packs/manufacturing/
    pack.json               # 行业包唯一机器事实源
  team/                     # 团队模型（本轮不动）
  tools/                    # CLI（改为消费 core + pack）
  orchestration/            # 本轮改 team-workflow.mjs 读 pack
  server/app.py             # 本地服务（stdlib http.server）
  ui/                       # 前端（本轮只加数据适配层）
  runs/<task_id>/           # 运行数据（新）
  tests/test_m1_acceptance.py
```

### 2.1 `core/ids.py`
```python
allocate(root: str, kind: str, task_id: str | None = None) -> str
# kind ∈ {task, report, decision, analysis, evidence, cost}
# 格式：T-YYYYMMDD-003 / DL-YYYYMMDD-001 / D-... / A-... / E-... / C-...
# 存储：runs/<task_id>/ids.json（kind -> next int）；task 级用 runs/.task_seq.json
# 并发：os.open(lock, O_CREAT|O_EXCL) 锁文件 + 重试（<=3s）+ 原子 os.replace 写回
```
- 同日多任务不得重复；已存在目录时 seq 跳过。
- 所有旧的 `-001` 字面量改为调用 `allocate()`。

### 2.2 `core/provenance.py`
```python
sha256_file(path) -> str
refs_record(paths: list[str], base: str) -> list[dict]   # [{path, sha256, size}]
verify_refs(refs: list[dict|str], base: str, require_sha=True) -> None  # 缺失/哈希不符 → ProvenanceError
```

### 2.3 `core/gates.py`
```python
PLACEHOLDER_TOKENS = ("demo 占位", "占位", "placeholder", "…")   # 用于 evidence 检测
is_placeholder_evidence(text) -> bool
validate_real_report(report: dict, base: str) -> None            # GateError：占位/缺证据/refs 不存在/total 与 verdict 不一致
check_finalize(decision: dict, report: dict, signature: dict, allowlist: list[str]) -> list[str]
# 返回不通过原因列表（空=可 approved）；校验：decision.status==proposed、
# decision.task_id==report.task_id、security_gate 若 report.security_gate_requested 必须 status==confirmed、
# signature.report_sha256==sha256(report file)、signature.signer ∈ allowlist、report.is_demo 不得为真时走 real 交付
```

### 2.4 `core/pack.py` / `packs/manufacturing/pack.json`
```json
{
  "pack_id": "manufacturing", "version": "1.0.0", "industry": "制造业",
  "thresholds": {"refill_baseline_pct": 5.40, "refill_target_pct": 7.2,
                 "cell_null_rate_manual": 0.10, "t30_window_days": 30},
  "roi_anchors": {"overtime_clean": 1200, "mold_change": 800, "maintenance": 500},
  "irreversible_actions": ["conditional_mold_change", "line_stop"],
  "six_dims": ["data_completeness","timeliness","significance","exclusivity","history","human_ai_consistency"],
  "crew_id": "manufacturing-refill"
}
```
```python
load(pack_id="manufacturing", root=None) -> dict   # env BOT_PACK 可覆盖
```
- `orchestration/team-workflow.mjs` 新增 `args.pack_path`（默认 `packs/manufacturing/pack.json`），ROI 锚点与阈值从 pack 读，删除 `:128` 附近的 ¥800/¥500/¥1200 字面量。
- Python 侧同样从 pack 读（`assemble_report.py`、`run_task.py`、`memory_retrieve.py` 中的制造业硬编码）。

### 2.5 `runs/<task_id>/` 布局
```
task.json      # intake 原样 + task_id + created_at
result.json    # team-workflow 返回的 {snapshot,factorTable,attribution,advice}
report.json    # assemble 产物（含 scorecard.total/verdict、data_refs 带 sha256）
signature.json # sign_delivery 产物 {report_sha256, signer, signed_at}
status.json    # {state: created|running|assembled|awaiting_sign|signed|failed, step, error?}
ids.json       # 任务内编号进度
run.log        # 追加式事件行（JSON lines：{ts, phase, role, line}），供 UI 舞台/SSE
loop/*.json    # 四卡
```

### 2.6 `server/app.py`（stdlib，127.0.0.1:8787，`--port`/`--root` 可配）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/ui/*` | 静态服务 `ui/` |
| GET | `/api/health` | `{ok:true,pack:"manufacturing",version:...}` |
| GET | `/api/packs` | 已装行业包列表 |
| GET | `/api/tasks` | `[{task_id,title,state,updated_at}]`（扫描 runs/） |
| POST | `/api/tasks` | body=intake JSON → 分配 task_id、写 task.json/status.json → 返回 task |
| GET | `/api/tasks/<id>` | `{task,report,status,loop}`（缺失字段为 null） |
| POST | `/api/tasks/<id>/run` | 后台线程跑 `run_task.py`，立即返回 202；状态写 status.json |
| GET | `/api/tasks/<id>/log?after=<n>` | `[{n,ts,phase,line}]` 增量；供 UI 轮询（SSE 可后续） |
| POST | `/api/tasks/<id>/sign` | body `{signer}` → 调 `sign_delivery.py` → 返回 `{sha256,signer}` |
| POST | `/api/tasks/<id>/finalize` | 调 `finalize_delivery.py`，不通过返回 409 + 原因 |
- 失败统一 `{ok:false,error}` + 4xx/5xx；所有子进程用 venv python 绝对路径。

### 2.7 UI 数据适配（本轮只做这些，不重写视觉）
- 新增 `ui/api-adapter.js`，`index.html` 引入；`API_BASE = ''`（同源）。
- 启动时 `GET /api/health`：成功 → 真数据模式；失败 → 保持现有静态演示（file:// 打开时不崩）。
- 真数据模式下替换 5 处硬编码：交付卡（`index.html:380-403` 读 report）、KPI（`260-263` 读 `/api/tasks`）、本场数据（`352-361`）、舞台台词（`627-633` 读 `/api/tasks/<id>/log` 的 phase 行）、待回看（`263`）。
- 签字按钮 → `POST /sign`，把返回的 `sha256` 显示在交付卡（必须与 `sign_delivery.py` 一致）。
- 保留 `tests/validate_structure.py` 依赖的字面量（`PixelSprites`、`beats`、`openIntake` 等），不要删。

### 2.8 `tests/test_m1_acceptance.py`
- 纯 stdlib + subprocess；在 `%TEMP%` 复制 `runs/` 沙盒运行，不污染仓库；
- 覆盖 A1–A6；`python -B tests/test_m1_acceptance.py` 退出码 0/1，打印 `RESULT: PASS|FAIL`。

## 3. 批次划分（互不重叠）

| 批次 | 负责人 | write scope | 内容 |
|---|---|---|---|
| B1 后端可信门 | backend-builder | `core/ packs/ orchestration/ tools/ tests/`（**不含** `tests/test_m1_acceptance.py`） | 2.1–2.5 + 修 score/finalize/run_task/assemble/validate |
| B2 服务+前端 | server-ui-builder | `server/ ui/` | 2.6–2.7 |
| B3 独立复核 | verifier | `tests/test_m1_acceptance.py` + `tests/fixtures/m1/` | 2.8 + 独立跑 A1–A6 + 反例 |

## 4. 回归红线

- `python -B tests/validate_structure.py` 必须仍 PASS（它断言文件存在与字符串，别删关键字面量）。
- `python -B tests/validate_report.py runs/demo-report-2026-10-19.json` 必须仍 PASS。
- `tests/*.py` 其余 smoke 不得变红；若某 smoke 依赖旧的「假通过」行为，先在 handoff 里列出并说明，不许静默改成全绿。

## 5. 交付 handoff（每个 builder 结束必须发）

```
【做了什么】一句话
【产物路径】绝对路径清单
【如何验证】可复制命令 + 实际输出摘要
【已知问题】未完成/风险/隐藏前提
【下一步】接收方动作
```
