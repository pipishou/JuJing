# M1 验收回执 · 真实一单可信闭环（2026-10-19）

> 结论：**M1 达成**。A1–A6 独立验收全绿；本地服务 + 真报告上屏链路跑通；可信门 fail-closed。
> 规格：`audit/M1-build-spec.md` ｜ 决策账本：`.junshi/ledger.md` ｜ 独立验收：`bot-team/tests/test_m1_acceptance.py`

---

## 1. 验收结果（独立 Maker-Checker，黑盒）

| # | 判据 | 结果 | 证据摘要 |
|---|---|---|---|
| A1 | 同日双任务不撞号、重跑不覆盖 | PASS | `allocate` 产 T-20261019-001/002，预建目录后自动跳 003；server 连建 002/003、历史目录不被覆盖 |
| A2 | 占位分 / 不存在 refs / 哈希错 → 拒收 | PASS | `validate_report --require-real` 3 负例 FAIL；`assemble --real` 2 负例非 0；正控通过 |
| A3 | pending gate / rejected decision / 伪签名 → 拒；合法链 → approved 且 by=signer | PASS | 合法链默认 signer=老板、bundle=proposed、finalize rc0 approved；无白名单 fail closed；白名单外/缺 signer/sha 错/pending/rejected 全非 0 |
| A4 | 六维全 5 → 100.0 且 verdict 命中；run_task 报告含 total/verdict 且与打分器一致 | PASS | full-5 → total=100.0 verdict=output；run_task 报告 total=60.0/verdict=force_manual_review，与 `score_scorecard` CLI 一致 |
| A5 | 改行业包 ROI 锚点 → 报告随之变化 | PASS | pack 改 777/999 后报告 ROI 同步；mjs 内 `¥800/¥500/¥1200` 字面量 0 命中；`BOT_PACK` 覆盖生效 |
| A6 | UI 交付卡 == report.json；签字哈希 == `sign_delivery.py` sha256 | PASS | 四字段（finding/scorecard/recommendation/decision_log_entry）字段级一致；sha256 三方一致（report 文件 == signature == /sign 返回）；UI 静态接线完整（`PixelSprites`/`beats`/`openIntake` 保留） |

服务层实测（8790）：`/api/health` → `POST /api/tasks` → `/run`（202 后台）→ 轮询 `assembled` → `/sign`（返回 sha256）→ `/finalize` 可逆路径 200 approved（approval.by=老板）；**不可逆路径 409 fail-closed 且不修改 decision**；`/log?after=n` 增量事件正常。
路径安全：6 种静态遍历（`..`, `%2e%2e`, `..%2f`, `%5c`…）+ 6 种非法 task_id + 3 种越权 GET → 全 404/400，无源码泄漏。
回归：`validate_structure.py` PASS、`validate_report.py runs/demo-report-2026-10-19.json` PASS、`tests/m1_smoke.py` PASS；demo 数据哈希未变。

## 2. 启动与使用（本机）

```powershell
$BT = 'D:\work\产品类\个人产品\bot-2\JuJing\bot-team'
$py = 'D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe'
# 1) 起本地工作台（127.0.0.1:8787）
cd $BT; & $py -B server/app.py --port 8787 --root $BT
# 2) 浏览器打开 http://127.0.0.1:8787/ → 新建任务 → 运行 → 签字 → 收口
# 3) 停止
Get-NetTCPConnection -LocalPort 8787 -State Listen | % OwningProcess -Unique | Stop-Process -Force
```

不开服务直接双击 `ui/index.html`（file://）时自动回退静态演示，不会崩。
M1 回归门：`& $py -B tests/test_m1_acceptance.py`（沙盒跑，不污染仓库）。

## 3. 本轮代码改动（相对 `135b36c`）

**新增**：`core/{ids,provenance,gates,pack,report_contract,fsutil}.py`、`packs/manufacturing/pack.json`、`server/app.py` + README、`ui/api-adapter.js`、`tests/{m1_smoke.py,test_m1_acceptance.py,fixtures/m1/*}`。
**重写/修复**：`orchestration/assemble_report.py`（refs+sha256、real 拒占位、pack 读 ROI、原子写）、`orchestration/team-workflow.mjs`（读 `args.pack_path`，删硬编码 ROI）、`tools/{run_task,finalize_delivery,sign_delivery,write_loop_bundle,score_scorecard,ingest_task_data,validate_report}.py`、`ui/index.html`（仅数据适配，视觉未动）。
**验收后 Lead 二次修复**：
1. `tools/finalize_delivery.py`：`--sha256` 由「覆写源」改为「一致性校验」，不一致 rc=2（堵住整文件哈希绕过）。
2. `server/app.py:797-808`：finalize 输出解析先整段后末行 → 409 恢复可读原因（不再 `error:"{"`）。

## 4. 已知限制（不阻塞 M1）

- **不可逆建议无法 approved**：securityGate 无人工确认入口 → finalize 409 fail-closed。M1 用可逆任务完成闭环；确认入口列 M1.5 第 1 项。
- A6 的 UI 部分为「静态检查 + HTTP 断言」，无浏览器渲染/点击验证，需人工走查（可对照 `ui/preview*.png`）。
- `run_task.py:314` 存在「自己传 `--signer` 即可自签」的 demo 便利路径（M1.5 收紧）。
- 占位检测为 token 启发式：零宽字符、语义变体（"待补充"）不命中（记录，不扩表）。
- 真实 CSV/Excel 入口依赖 pandas，当前环境无且未获授权新增依赖 → 真实工单先在 M2 升级协议里定接入方式。
- 三个既有测试仍红（`negative_gates.py` / `real_mode_smoke.py` / `run_task_smoke.py` 顶部 import pandas）——基线问题，本轮按红线未动测试。

## 5. 下一步（待用户拍板）

1. **git 提交授权**：本轮改动未提交（红线：git 动作需用户下令）；是否提交、拆几个 commit、是否需要先清 `runs/.task_seq.json` 测试残留。
2. **M1.5 开工**：securityGate 确认入口 → signer 兜底收紧 → 视觉/身份证错位修复 + 真人走查。
3. **真实使用**：用户本人用真实制造业数据跑 1 单（先按 JSON 样本格式给数据），回写 `.junshi/ledger.md` 复盘栏。
