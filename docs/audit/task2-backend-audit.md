# task-2 后端深度审计（orchestration + tools + tests）— backend-inspector

环境：Windows pwsh，Python=D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe（含 pyyaml，无 pandas）。
只读审计；所有探针用 `python -B` 运行、产物写 %TEMP%；仓库未改动。探针：%TEMP%\audit_probe.py、%TEMP%\audit_probe2.py。

## 结论
演示链（demo 资产 + example 四卡）能跑通；但“真实任务”的可信门（护栏/签名/四卡/证据）大量停留在文档层，且没有 HTTP/队列/进度/取消等支撑可视化多任务的后端层。按现状把 UI 接上 = 产出“假真实报告”。

## P0（真实可触发、破坏产品可信）
1. **交付闸门任意绕过** `tools/finalize_delivery.py:36-50`
   - :38 只比对 report sha256；:46 无条件 `dec["status"]="approved"`；:47 `approval.by=sig.get("signer")`（签名文件内容自证）。
   - 不校验：decision 原状态（proposed/rejected）、`decision.task_id` 与 report.task_id 一致、security_gate 是否 confirmed、no_finding、report.is_demo、signer 身份。
   - 实测（探针8）：loopB decision 原 `status=rejected/task_id=T-B` + report T-A、pending gate 的签名 → approved，approval.by="attacker"；伪造 `{"report_sha256": <hash>}` 无 signer 也 approved，by=null。
   - 后果：`gates.md:10-11` 的“securityGate 被绕过=交付失败”不成立；不可逆动作可被 approved。
2. **real 报告可含 demo 占位与不存在的证据仍全绿** `orchestration/assemble_report.py:74-77,88` + `tests/validate_report.py:54-67`
   - assemble real 只查 scorecard 六个键“存在”（不查值/证据/分档）；data_refs 只按逗号切，不验文件存在。
   - validate_report real 只要求数值 0-5，data_refs 仅排除子串 "demo/"（:67）。
   - 实测（探针2）：workflow-result 六维 `{"score":3,"evidence":"demo 占位：…"}` + `--data-refs not-exist/T.json,not-exist/T30.json` → assemble exit0、report `is_demo=false`、`validate_report --require-real` PASS。
3. **硬护栏/100 分制未接入流水线**
   - `tools/run_task.py:92` 只调 validate_report；全仓 grep：`score_scorecard` 只在 docs/tests 出现，无任何生产调用（`:78` 合同输出也被丢弃，未写入报告/decision）。
   - 实测：run_task_smoke 的 scorecard（data 4/h_a 0/exclusivity 3）→ score_scorecard 输出 total 63.35、guardrails=human_ai_conflict+too_many_hypotheses+data_gap、verdict force_manual_review；但 run_task/validate_report 依旧 approved。
   - 实测：`gates.md:15`“空值率>10% 转人工”未实现——单元格缺失 13.64% 时 `check_data_contract.py` null_rate_factor=0.0、level=normal、exit0（:65-66 只算因素级；:97-104 不含 cell_rate）。
4. **满分被判“不输出”** `tools/score_scorecard.py:87,97-107`
   - :87 `round(dims/5*16.67,2)`：六维全 5 → 6×16.67 = **100.02**；:100-102 阈值上界 max 缺省 100 → 无区间命中 → for/else 保留 `verdict="no_output"`、`label=""`（实测）。
   - 影响：冷启动校准下唯一“满分”反而 no_output。

## P1
- **无 provenance/不可审计**：`run_task.py:82-85` 直接 `--real`；`assemble_report.py:122` 只记 `"source":"team-workflow.mjs"`（不记结果文件路径/sha256）；`orchestration/team-workflow.mjs:8` data_dir 缺省即 demo。旧任务/demo 的 workflow-result 可生成 `is_demo=false` 报告（无 inputs 绑定）。
- **四卡可整包伪造**：`tools/write_loop_bundle.py:57-60` 自填 score=1.0/grade=B/data_quality=A/freshness=30；:127-131 原样 copy example analysis/goal/cost（不校验）；:44-45/:108 同日全 `-001`（D-/A-/C-/E-）→ 同日多任务撞号。实测 decision.analysis_id=A-20261019-001 与被拷 analysis.id=A-20261018-01 悬空、decision.entity_id=T-PROBE2、goal.entity=塑胶厂-B线；`validate_loop.py:77-87` 不校验 association，`--require-complete` 仍 PASS。
- **幂等/并发**：`tools/cost_record.py:61` `cost_id=C-{date}-{task}`，实测同任务跑两次产生两条相同 id，summary net=2999.96 双计（单次 1499.98）；无文件锁；sign/finalize/assemble/write_loop_bundle 全部非原子写（`finalize:50`、`sign:76`、`assemble:124`）；同 outdir 重跑/并发覆盖 report/loop/sign（`run_task.py:63-64` mkdir exist_ok）。
- **合同漏洞**：`check_data_contract.py:65-66` 只因素级空值率（1 个因素全缺=0.0909≤0.10→normal；单元格>10% 不转人工）；:89-104 date_gaps 算了但不参与 level；:121 `t30_provided=bool(args.t30)`，实测 T30 路径不存在仍 normal；稀疏 2 条/7 天窗 date_gaps=5 仍 normal；:124 --strict 只在 manual 才 exit2。
- **数据入口**：`ingest_task_data.py:29` pd.read_csv 默认 UTF-8（GBK CSV 必崩）；:60 `pd.to_datetime` 无 errors=coerce（非法日期整批 traceback）；:62 产线精确串比较（空格/大小写/全角即空）；:63-64 误报“窗口内无数据”；:73 字符串窗口（start>end 静默空）；:67-87 meta 继承 demo schema_version。实测受阻于 pandas 缺失（见下），code-level 确认。
- **依赖缺失阻断**：本 venv 与 PATH python 均 `ModuleNotFoundError: pandas`（实测），仓库无 requirements.txt/pyproject.toml → `run_task.py:68` ingest 第一步即崩，真实任务入口在本仓指定环境不可用。
- **凭据/出站**：`wecom_notify.py:30-33` mask 保留前 24+后 4 字符（36 位 key 暴露 28 位），:78/:81 打印进日志/终端（docstring 声称“不写入日志”）；:72 把 key 拼 URL ?key=，:104 `--endpoint` 任意 → 本机 SSRF/外带；无 webhook 自动 dry-run（:110-112）→ CI 误判成功；文档要求 20 条/分钟限速/队列，代码无。
- **后端层整体缺失**：全仓仅 `tests/wecom_transport_smoke.py:39` 有 mock HTTPServer；无 HTTP API、无任务注册表/状态机、无事件流、无队列/异步/进度/取消；`ui/index.html:837-856` 只在浏览器生成 JSON Blob 下载，:252/835 新建任务无后端接收；`gen_ui_data.py:64` generated_at 写死 2026-10-18（今日 2026-10-19），:67-75 context 恒 example；`gen_tape_ui.py:15` 固定 runs/dsh-tape-sample.json；`gates.md:17`“连续3次失败暂停”无计数器实现。
- **调度安装**：`install_review_task.ps1:10` 用 PATH python（非 venv）；:14 schtasks 无工作目录、无退出码检查 → 相对 `runs` 在 System32 解析、中文/空格路径嵌套引号易断，失败仍打印 registered。

## P2
- `score_scorecard.py:53` stdout reconfigure utf-8-sig → 输出带 BOM，实测 `json.loads` 失败（管线消费方坏）；:46 的 0-5 范围对 NaN 比较为 False → NaN 通过、输出非法 JSON（实测 ok=true、total NaN）。
- `dsh-session-tape.mjs`：非 zstd 文件 → frames 空 → beats=0 且 exit0（实测）；magic 0x28b52ffd 搜索可能错切帧；解码失败仅 console.error。`review_due.py:53-56` 非法 due 静默跳过（实测 due=0/exit0），无告警/无 --json；`memory_retrieve.py:42` 缺 entity 字段的记忆绕过实体过滤（实测 --entity 甲线 返回 M1+M2），:46-52 非法 expires 永不失效。
- `assemble_report.py:56,124` + `write_loop_bundle.py:89`：`--date 2026/10/19` 先写出 is_demo=false 报告再 `ValueError: Invalid isoformat string` 崩溃（无回滚，残留 report/无 loop）；`get_flag` :30-35 手写解析（值以 -- 开头/未知 flag 静默）。
- `validate_report` 不读 `scorecard.total/verdict`：100 分制结果从不进入报告；`delivery-card.schema.yaml`（report_id DL-…/signature 状态机/followup 7 天）无任何代码生成或校验。
- 测试缺口：`negative_gates.py` 只测 demo 叙述串；无 dict 占位/GBK/T30/最终闸门负例；`real_mode_smoke.py:83-90` 用 example analysis/goal/cost 即“真实模式”全绿；`validate_structure.py` 只查文件与字符串字面量，行为 bug 全绿。
- team-workflow.mjs schema：finding/cannot_attribute/recommendation 无 minItems/maxItems（finding≤2、cannot_attribute 非空只在 prompt 文案 :120）；scorecard 非 required；s3/s4 违约要到 s5 validate 才暴露（整跑浪费）。

## 探针证据（可复现）
- %TEMP%\audit_probe.py：16 探针（score all5、real 占位、example 四卡、cell 空值、T30 缺失、GBK、非法日期、产线大小写、memory 泄漏、finalize 跨任务/伪签、tape 非 zstd、review 非法 due、cost 月前缀、NaN、run_task 缺 wr、UI generated_at、verify 忽略 signer）。
- %TEMP%\audit_probe2.py：8 探针（all5 判定、smoke 卡护栏、单元格空值门、稀疏覆盖、pandas、wecom mask、非 ISO 日期、cost 重跑双计、factors_table）。
- 关键输出：all5 total=100.02 verdict=no_output bom=true；smoke 63.35 force_manual_review；cell 13.64% normal exit0；finalize rejected→approved by attacker；fake sig→approved by null；cost 两条同 id net 2999.96；memory ids=[M2,M1]；tape beats=0 exit0；month 2026-1 records=2。

## 若只修 5 处（建议顺序）
1. `tools/finalize_delivery.py:36-50`：加状态机（proposed/pending_signature→approved）+ task_id/decision_id 一致性 + security_gate confirmed + signer 白名单，不可逆动作强制人工确认。
2. `tools/run_task.py:92` 前后接入 `score_scorecard`，并把 `tests/validate_report.py:54-67` 升级：拒占位/叙述 scorecard（dict 也查 evidence 非“demo 占位”）、data_refs 存在性+sha256、要求 scorecard.total/verdict 与打分器一致。
3. `tools/score_scorecard.py:87,97-107`：改 16.67→精确权重或 `>=total_score` 兜底，修 100.02 越界与 NaN/BOM。
4. 可审计+幂等：report 记录 workflow-result/T/T30 的 sha256 与路径；D-/A-/C-/E-/DL- id 加序号或短哈希；所有落盘改 temp+os.replace，outdir 加锁。
5. `check_data_contract.py` + `ingest_task_data.py`：cell_rate>10%→manual、date_gaps/最小记录数、T30 内容校验；CSV 编码探测（GBK/GB18030）、`errors="coerce"` 丢行计数、产线 strip/大小写归一。

## 附：未改仓库证明
全部写入均落在 %TEMP%（audit-probe-*/audit2-*/sc-*、audit_probe*.py、本文件）；仓库内无 __pycache__（python -B/PYTHONDONTWRITEBYTECODE=1）。
