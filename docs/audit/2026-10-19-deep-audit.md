# bot-team 深度体检报告（2026-10-19）

> 范围：四路只读审计（UI / 后端 / 团队模型 / DSH 数据通路）+ demo 实跑 + 一次全仓代码体检。
> 所有结论带 `file:line` 证据，探针脚本与原始报告见文末附录；审计期间仓库零改动。

---

## 0. 一句话结论

**演示链能跑，真实任务不能信，界面是壳，产品形态未定。**
四路审计指向同一个断点：这套包是「能自证的演示资产」，不是「能接单的助手」。**现在把 UI 接到后端 = 产出「假真实报告」**——所以不能先接前端，必须先把可信门修好。

---

## 1. 事实基线

| 项 | 实测 |
|---|---|
| 仓库 | `D:\work\产品类\个人产品\bot-2\JuJing`，main @ `135b36c` |
| 演示链 | `team-workflow.mjs`（4 agents）→ `runs/demo-result-2026-10-19.json` → `assemble_report.py` → `runs/demo-report-2026-10-19.json` → `validate_report.py` **PASS** |
| 机检 | `validate_structure.py` **PASS（250 项）**、7 个 smoke 全绿 |
| 审计覆盖 | ui/ 全部、orchestration/ 3 个、tools/ 19 个、tests/ 9 个、team/ 全部 yaml + roles + assets、docs/ 全部 |
| DSH 宿主 | 0.2.1-rc.2，node 24.19，python 3.12.14；CLI `dsh.cmd`（headless / sdk / web / plugin） |
| 前端规模 | `ui/index.html` 879 行 / 72,630B 单文件，无构建、无框架、零前端测试 |
| 上游资产 | `D:\项目\REA`（行家来了逆向产物）**不存在**，无法对照 |

---

## 2. 唯一真正闭环的部分：demo 链

- workflow 硬编码 s1–s4 提示词与 schema，不读 `team/dag.yaml`（双源，改 YAML 不改行为）。
- assemble 产出的报告契约是完整可用的：finding / cannot_attribute / scorecard / recommendation / decision_log_entry / role_coverage。
- tape 回放是**道具**：`runs/dsh-tape-sample.json` 已脱敏，120 beats 中 118 条 tool 无 args、0 条 user、仅 2 条「内容已脱敏」的 speak；`dsh-session-tape.mjs:89` 只取前 240 beats。

---

## 3. 后端：真实任务的「可信门」形同虚设（P0 ×4）

### P0-1 交付闸门可任意绕过 `tools/finalize_delivery.py:36-50`
- `:38` 只比对报告 sha256；`:46` **无条件** `dec["status"]="approved"`；`:47` `approval.by=sig.get("signer")`（签名文件自证身份）。
- 不校验：decision 原状态（proposed/rejected）、`decision.task_id` 与 report.task_id 一致、security_gate 是否 confirmed、no_finding、`report.is_demo`、signer 白名单。
- 实测：被 rejected 的 decision + 跨任务 report + pending gate → **approved**，`approval.by="attacker"`；伪造 `{"report_sha256": <hash>}` 无 signer 也 approved。

### P0-2 real 报告可含 demo 占位与不存在的证据仍全绿 `orchestration/assemble_report.py:74-77,88` + `tests/validate_report.py:54-67`
- assemble 只查 scorecard 六个键「存在」，data_refs 只按逗号切、不验存在。
- 实测：六维 `{"score":3,"evidence":"demo 占位"}` + `--data-refs not-exist/...` → exit0、`is_demo=false`、`--require-real` **PASS**。

### P0-3 硬护栏 / 100 分制没进流水线
- `tools/run_task.py:92` 只调 validate_report；全仓 `score_scorecard` **零生产调用**，合同输出被丢弃。
- 实测：smoke 卡打分 63.35、护栏 `human_ai_conflict + too_many_hypotheses + data_gap`、verdict `force_manual_review` → 仍 approved。
- `gates.md:15`「空值率 >10% 转人工」未实现：单元格空值 13.64% 时 `check_data_contract.py:65-66` 算因素级 `null_rate=0.0`、`level=normal`。

### P0-4 满分隔界 bug `tools/score_scorecard.py:87,97-107`
- `round(dims/5*16.67,2)` → 六维全 5 = **100.02** > 上界 100，无区间命中 → verdict `no_output`。冷启动校准下「满分」反被拒。

### P1（摘要）
- 无 provenance：`run_task.py:82-85` 直接 `--real`；报告只记 `"source":"team-workflow.mjs"`，不记结果文件 sha256；旧 demo 结果可生成 `is_demo=false` 报告。
- 四卡可整包伪造：`write_loop_bundle.py:57-60` 自填 score=1.0/grade=B；`:127-131` 原样 copy example；同日全 `-001` 编号；`validate_loop.py:77-87` 不校 association 仍 PASS。
- 幂等/并发：`cost_record.py:61` 重跑双计（net 2999.96）；sign/finalize/assemble 全非原子写；同 outdir 覆盖。
- 合同漏洞：`check_data_contract.py:89-104` date_gaps 算了不参与 level；`:121` T30 只 `bool(路径)`。
- 入口：`ingest_task_data.py:29` 默认 UTF-8（GBK CSV 必崩）、`:60` 日期不 coerce、`:62` 产线精确匹配、`:63-64` 误报；venv 与 PATH 都缺 pandas 且无 requirements → `run_task.py:68` 首步崩。
- 凭据：`wecom_notify.py:30-33` mask 保留前 24+后 4（36 位 key 暴露 28 位）并入日志，`:104` `--endpoint` 任意（本机 SSRF）。
- **后端层整体缺失**：无 HTTP API、无任务注册表/状态机、无事件流、无队列/异步/进度/取消；UI 侧 `index.html:837-856` 只下载 JSON Blob。

---

## 4. 可视化层：15–20% 完成度的演示壳（P0 ×4）

- **不消费真实交付物**：报告契约已就绪（`assemble_report.py:105-125`），但交付卡（`index.html:380-403`）、舞台台词（`627-633`）、KPI（`260-263`）、本场数据（`352-361`）全硬编码；全页无 `fetch/localStorage/WebSocket`。
- **签字/接单/推送是幻影**：`signPaper`（`805-825`）对硬编码 payload 取 SHA-256 前 16 位；真工具 `sign_delivery.py:21-27` 对 report.json 整体取 sha256、`:60-76` 写 signature.json，`ui/README.md:15` 的「对齐」不实；`generateTask`（`842-861`）只下载、task_id 恒 `-01`、未落 `runs/`。
- **产品硬门缺失**：`team/gates.md:8-11` 的 G0 + securityGate 两级人工门，G0 仅 toast（`448`），securityGate 无界面（`variantD_irreversible` 数据未用），打回（`826-832`）只改章违 `crews.yaml:83`，T+7 回看未接 `review_due.py`。
- **数据错位**：TEAM_DATA 用长 slug（`consulting-analyst`），`sprites.js:14-27` 只有短 key（`analyst/attr/biz/data`）→ 头像全灰（`index.html:538,553`）；`hire()` 按硬编码 id 计数（`567-575`）。
- P1/P2：单文件 879 行、innerHTML 拼接、21 个 inline onclick、无 aria/键盘、字号 8.5–10.5px、仅 1 条 `<1100px` 响应式；两套叙事不一致（BEATS vs renderTape）；零前端测试。
- **判决**：视觉/叙事资产（等距舞台、像素小人、卡片版式、数据文件分治）值得 100% 保留；数据层 + 交互层重写 60–70% JS。

---

## 5. 团队模型：可用但接不通（P0 ×5）

1. **员工 id 宇宙错位**：`gen_ui_data.py:42` 长 id vs `sprites.js:15-22` 短调色板 vs `index.html:502-511/625-634` 固定 4 席 → 9 张员工卡只有 4 个能上台、6 个灰人。
2. **记忆面板恒空**：`gen_ui_data.py:72-74` 按短 id 聚合，`index.html:559` 用长 slug 读 → 详情页永远「暂无跨任务记忆」。
3. **并发撞号**：`assemble_report.py:90` 恒 `DL-{date}-001`；`write_loop_bundle.py:44-45,108` 恒 `D-/A-/C--001`；`run_task.py:65` `T-{date}-{line}`；无发号器/锁 → 同日两单必重号，交付卡-决策日志-四卡对账断链。
4. **craft 被标可雇**：`gen_ui_data.py:46` 违 `shelf.yaml:18`「craft 不出现在招聘页」。
5. **员工档案缺字段**：roles 仅 3/7 有 frontmatter；`shelf.yaml:50` 要 sample.md/png 全仓不存在；`delivery-card.schema.yaml:9-13` 无 employee/crew；cost 示例无 employee_id。
- 多班组是假数据：`gen_ui_data.py:56-62` 生成 4 套，`index.html:366` 写死「5 步·4 角色」、`:235` badge 写死 4，无列表/切换。
- 判据多为文档：仅 `factors_table`（`check_data_contract.py:44`）与 `scorecard_six_dim`（`score_scorecard.py:74`）被机器消费；ROI ¥800/¥500/¥1200 写死 `team-workflow.mjs:128`；制造业内容写死 `assemble_report.py:93,96,110`、`run_task.py:66`、`memory_retrieve.py:9`。
- 团队判决：四表（roster/employees/crews/shelf）**方向对、可作可视化产品的事实源**，但必须统一 id、按任务命名空间发号、班组区动态化。

---

## 6. 落地形态 A/B/C

| | A 纯静态页 | B 本地 Web 服务 | C DSH Web 插件 |
|---|---|---|---|
| 数据源 | 读会话日志/生成 .js | 服务读写 runs/ + tools/*.py | 宿主内 session/event |
| 实时性 | 回放式 | 实时（轮询/SSE） | 原生实时 |
| 派发任务 | 不能 | `dsh.cmd headless` | 宿主 API |
| 工作量 | 2–3 人日 | 5–8 人日（2–3 周/1 人） | 3–5 人日（3–5 周含探明 API） |
| 风险 | file:// CORS、零写入 | 双窗口/进程/本地安全；SDK 不能旁听 GUI 会话 | 绑 0.2.x 槽位契约、API 波动 |
| 天花板 | 评审演示 | 独立工作台 | 最高，最像「用户不见引擎」 |

**建议：B → C**（先 2–3 周拿真实闭环 + 数据契约，再迁插件；插件期复用 B 的组件与契约）。若定位就是「DSH 换壳」，可先做 2 周插件 API 探针再直上 C。

---

## 7. 对你三点指示的回应

1. **「不想做成 skill 的形式」** → 同意，且现有包确实停在 skill 形态（skill 目录 + 脚本 + 演示）。产品化需要把它拆成四层：
   ```
   app/web     前端（迁入视觉资产，重写数据适配层）
   app/server  本地服务（/api/tasks /api/report /api/sign /api/review）
   core        编排 + 报告组装 + 可信门（读 dag.yaml、接 score_scorecard）
   team        名册/班组/货架/角色卡/判据（唯一事实源）
   runs/<task_id>/  task.json / result.json / report.json / signature.json / 四卡
   ```
2. **「bot 团队 + 可视化，不是聊天窗口」** → 现有「AI 员工事务所」四区（员工货架 / 班组编排 / 舞台实况 / 交付墙）方向成立、视觉资产可留；但**舞台必须由真实事件流驱动**（会话日志 tail 或宿主 session/event），否则就是动画片。
3. **「深入检查代码」** → 本报告；四路结论一致：**先修可信门，再接前端**。

---

## 8. 建议路径

### M1（1 周，目标：真实一单可信闭环）
1. 修 P0 可信门：finalize 状态机 + task_id 一致性 + signer 白名单 + gate confirmed；run_task 接 score_scorecard；validate_report 拒占位/验 data_refs 存在与 sha256；修 100.02 与单元格空值阈值。
2. 编号与 provenance：`runs/<task_id>` 命名空间发号（原子写 + 锁），报告记录 T/T30/workflow-result 的 sha256。
3. 前端 data-adapter：交付卡/舞台/KPI/本场数据改为读 `runs/latest-report.json`，替换 5 处硬编码。
4. 最小服务：`/api/tasks`、`/api/report`、`/api/sign`、`/api/review`；跑通「接单 → 跑 → 交付 → 签字 → 回看」。

**M1 验收（用户视角）**：
- 给一份真实 task-intake，一条命令产出 `runs/<task_id>/report.json`，task_id 唯一、重跑不覆盖；
- 报告里 data_refs 全部存在且带 sha256，占位 score 必被拒；
- pending securityGate / rejected decision / 伪 signer → 交付必须失败；
- 页面显示的是这份真报告；**UI 签字哈希与 `sign_delivery.py` 对同一 report.json 的 sha256 完全一致**。

### M2（1–2 周）
多任务列表 + 真实事件流（tail `session.v4.jsonl.zstd` 或宿主订阅）+ 审批身份 + 断点/取消 + 交付墙历史回放。

### M3（2–4 周）
C 形态落地：DSH Web 插件整页/面板 + 反壳（禁模型名/token/插件管理、浅色锁定）；或 B 独立部署。

---

## 9. 待你拍板（G0）

1. **形态**：B→C（推荐）／直上 C（先 2 周插件探针）／只做 A 演示？
2. **M1 范围**：是否按上面 4 条开工（约 5–7 人日）？还是先只修「可信门」不碰前端？
3. **行业边界**：制造业专用判据（补料率/型腔/换模）保留为「第一套班组模板」，还是抽成通用判据 + 行业包？

---

## 附录 · 证据索引

- 后端完整报告（59 行，含复现命令）：`audit/task2-backend-audit.md`
- 探针：`%TEMP%\audit_probe.py`（8 探针）、`%TEMP%\audit_probe2.py`（7 探针），均 `python -B`，产物写 `%TEMP%`
- demo 产物：`JuJing/bot-team/runs/demo-result-2026-10-19.json`、`demo-report-2026-10-19.json`
- 共享任务板：task-1（UI，rev4 completed）、task-2（后端，rev3 completed）、task-3（模型，rev3 completed）、task-4（DSH 通路，rev3 completed）
- 关键复现结果：all5 total=100.02 verdict=no_output；smoke 63.35 force_manual_review；finalize rejected→approved by attacker；fake sig→approved by null；cost 双计 net 2999.96；memory ids=[M2,M1]；tape beats=0 exit0
