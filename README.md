# 聚鲸 Bot 团队（DSH 版）· 可迁移结构包

> 状态：v0.11.0，2026-10-18 本机（DSH 0.2.x / Windows）搭起并测试（v0.7：真实数据入口/真实报告门/评分卡/签字；v0.8：四段闭环 bundle；v0.9：UI 数据绑定 + 回看/月报；v0.10：无归因/值级合同/本机回看/签字收口；v0.11：一键收口）。
> 用途：把「制造业经营归因四角色团队」做成**结构可跑、可测、可迁移**的包；明天带到工作电脑继续推进。

## 它是什么

- **一个 DSH skill**（`SKILL.md`，name=`jujing-bot-team`）：装入 skill 根即可被模型发现；
- **一份团队结构**：`team/roster.yaml`（名册·唯一事实源）＋`team/roles/*.md`（4 份章程）＋`team/dag.yaml`（五步 DAG）＋`team/gates.md`（门与纪律）；
- **一套行业资产**：`assets/` 8 个判据 YAML＋`demo/` 5 份演示数据（来自既有制造业专家包，未改内容）；
- **两种运行模式**：确定性 `orchestration/team-workflow.mjs`（DSH `workflow` 工具）｜交互式 Agent Teams（`spawn_teammate`×3＋共享任务板）；
- **可机检**：`tests/validate_structure.py`＋`tests/validate_report.py`；
- **UI 概念原型**：`ui/index.html`（单文件离线，AI 员工事务所·舞台实况，假数据，用于 G0「第一眼」评审）；设计定稿见 `docs/OPTIMAL_TEAM_AND_UI_2026-10-18.md`；
- **v0.2.1 增量**：`ui/sprites.js` 原创像素小人（Kairosoft 风格参考，不含开罗素材）＋ `tools/gen_ui_data.py`（YAML→`ui/team-data.js`）＋ `orchestration/dsh-session-tape.mjs`（DSH 日志→舞台 tape，样例 `runs/dsh-tape-sample.json`）；
- **v0.3 增量**：角色 8 帧动画（idle/walk0/walk1/blink/talk/happy/think/surprised；说话口型、换拍行走、随机眨眼、按幕表情）＋真实 DSH 会话 tape 回放（`ui/tape-data.js`，`?mode=tape&t=N`；样例 120 beats）；
- **v0.3.1 增量**：UI「新建任务」接单向导（生成 task JSON）＋`templates/task-intake.example.json`＋`docs/REAL_TASK_PLAYBOOK_2026-10-19_zh.md`＋`docs/PRODUCT_BENCHMARK_2026-10-18_zh.md`；交付卡加成本行（AI 成本/人工节省）；工作流支持 `args.data_dir`；
- **v0.4 增量**：`team/loop/` 四段闭环卡（证据/分析/决策/闭环 schema + 状态机 + 示例）；`docs/FOUR_LOOP_UPGRADE_2026-10-18_zh.md`；GitHub 仓库方案 `docs/GITHUB_REPO_PLAN_2026-10-18_zh.md`；
- **v0.5 增量**：与方法论 v1.3 对齐——`assets/scorecard_six_dim.yaml` 升 100 分制/硬护栏/阈值/CSOLR；新增 `assets/perception_rules.yaml`（L0 检测+三级降级+失败回滚）、`assets/decision_contract_refill.yaml`（决策契约）；四卡补齐决策日志字段；`docs/METHODOLOGY_INTEGRATION_2026-10-18_zh.md`；
- **v0.6 增量**：关系层 `team/memory/`（员工记忆 schema+检索规则+脱敏样例）、目标层 `team/goals/`（目标卡+进度）、成本层 `team/cost/`（AI 成本/人工节省/净收益/ROI）、接入层 `team/integrations/wecom.yaml`＋`tools/wecom_notify.py`（企微群机器人出站，20 条/分钟，dry-run 已验证）；UI 加目标进度、员工记忆、企微推送按钮；
- **v0.7 增量**：真实数据入口 `tools/ingest_task_data.py`（CSV/Excel→T/T30）、`tools/check_data_contract.py`（11 因素/10%/30% 降级）；真实报告门 `assemble_report.py --real`＋`validate_report.py --require-real`；评分卡计算 `tools/score_scorecard.py`；签字持久化 `tools/sign_delivery.py`（拒绝 demo 签字）。
- **迁移件**：`MIGRATION.md`＋`package.ps1`（zip＋manifest）。

## 真实数据流程（v0.7）

```powershell
# 1. CSV/Excel → T/T30 JSON（不补数）
python tools/ingest_task_data.py --input task.csv --line B --t-start 2026-10-05 --t-end 2026-10-11 --base-start 2026-09-05 --base-end 2026-10-04 --outdir runs/task-B
# 2. 数据合同（--strict 时 manual 退出非零）
python tools/check_data_contract.py --t runs/task-B/material_refill_T.json --t30 runs/task-B/material_refill_T30.json --strict
# 3. 六维评分（数值 0-5，--strict 拒绝叙述）
python tools/score_scorecard.py --scores "{\"data_completeness\":4,\"timeliness\":3,\"significance\":5,\"exclusivity\":3,\"history\":4,\"human_ai_consistency\":0}" --strict
# 4. 收口为真实报告（禁 demo 占位）
python orchestration/assemble_report.py workflow-result.json runs/task-B/report.json --real --task-id T-20261019-B --context "B线补料率7日 5.1%→7.2%" --data-refs "runs/task-B/material_refill_T.json,runs/task-B/material_refill_T30.json" --expected "T+7 补料率 ≤5.40%" --records-dir runs/task-B/loop --analysis-json team/loop/example-analysis.json --goal-json team/goals/example-goal.json --cost-json team/cost/example-cost.json
# 5. 真实报告门 + 签字
python tests/validate_report.py runs/task-B/report.json --require-real
python tools/sign_delivery.py runs/task-B/report.json --signer "业务负责人"
# 6. 四段闭环 bundle 校验（核心三件+可选分析/目标/成本）
python tools/validate_loop.py --task-dir runs/task-B/loop --require-complete
# 7. 成本记录/汇总（成本透明）
python tools/cost_record.py record --task T-20261019-B --ai-cost 0.02 --tokens 79000 --hours 2.5 --action 500 --avoided 2000 --source estimate --outdir runs/task-B
python tools/cost_record.py summary --file runs/task-B/cost.jsonl
# 8. 记忆检索（按员工+实体，最多 5 条）
python tools/memory_retrieve.py --file team/memory/example-memory.json --employee analyst --entity "塑胶厂-B线" --limit 5
# 9. 回看扫描（本机清单；GitHub issue 需 gh）
python tools/review_due.py --root runs --out runs/review-due.md
python tools/review_due.py --root runs --create-issue   # 仅 CI/装了 gh 的机器
# 10. 成本月报
python tools/cost_monthly.py --file runs/task-B/cost.jsonl --month 2026-10 --outdir runs
# 11. 企微交付卡（从 report.json 自动填充；默认 dry-run）
python tools/wecom_notify.py --kind delivery --report runs/task-B/report.json --cost "AI ¥0.02 / 人工节省 2.5h" --dry-run
# 12. 负向门（确认 demo/叙述/缺数据真的被拒）
python tests/negative_gates.py
# 13. 无真实数据也能回归（合成 11 列 CSV，跑通全链）
python tests/real_mode_smoke.py
# 14. 企微传输层（本地 HTTP server，成功/失败路径；不连真实企微）
python tests/wecom_transport_smoke.py
# 15. 无归因路径 + 值级合同 + 签字收口
python tests/no_finding_smoke.py
python tests/contract_values_smoke.py
python tools/finalize_delivery.py --report runs/task-B/report.json --loop-dir runs/task-B/loop --signature runs/task-B/report.signature.json
# 16. 一键收口（ingest→合同→real 报告→四卡→签字→approved）
python tools/run_task.py --csv task.csv --line B --t-start 2026-10-05 --t-end 2026-10-11 --base-start 2026-09-05 --base-end 2026-10-04 --field-map templates/field-map.example.yaml --outdir runs/task-B --workflow-result runs/task-B/workflow-result.json --analysis-json team/loop/example-analysis.json --goal-json team/goals/example-goal.json --cost-json team/cost/example-cost.json --expected "T+7 补料率 ≤5.40%" --signer "业务负责人"
# 17. 本机 T+7 回看计划任务（每周一 09:00）
powershell -ExecutionPolicy Bypass -File tools/install_review_task.ps1
```

## 快速开始（本机）

```powershell
# 1. 结构/资产/演示数据完整性
python D:\项目\bot\bot-team-dsh\tests\validate_structure.py

# 2. 确定性跑一遍 demo（在 DSH 会话里让 Lead 调用 workflow 工具，脚本见 orchestration/team-workflow.mjs）
#    产物写到 runs/demo-report.json，再校验：
python D:\项目\bot\bot-team-dsh\tests\validate_report.py D:\项目\bot\bot-team-dsh\runs\demo-report.json
```

## 目录

```
bot-team-dsh/
├─ SKILL.md                 # DSH skill：触发词/两模式/DAG/门/校验
├─ README.md
├─ MIGRATION.md             # 工作电脑迁移步骤 + 验收清单
├─ package.ps1              # 打 zip + manifest(sha256)
├─ team/
│   ├─ roster.yaml          # 名册唯一事实源
│   ├─ dag.yaml             # 五步 DAG（并行/依赖/产出契约）
│   ├─ gates.md             # 人工门/权限/降级/CRP/决策日志
│   └─ roles/               # host + 3 角色章程
├─ assets/                  # 8 个判据 YAML（factors/scorecard/exclusion/crp/roi/degradation/decision_log/irreversible）
├─ demo/                    # T / T-30 / 变体 B / C / D
├─ orchestration/
│   └─ team-workflow.mjs    # 确定性 runner（喂给 DSH workflow 工具）
├─ tests/                   # 结构与报告机检
├─ runs/                    # 本机测试回执 + demo 报告
├─ ui/                      # UI 概念原型（AI 员工事务所·舞台实况）+ 预览图
└─ docs/                    # 方向手稿 / 试点规格 / 最优版团队+UI 设计 / 项目账本快照
```

## 本机测试结果（2026-10-18）

| 测试 | 结果 |
|---|---|
| 结构/名册/DAG/资产/演示数据/门 完整性（`validate_structure.py`，79 项） | **PASS** |
| skill 热加载发现（`~/.dsh/skills/jujing-bot-team`） | **PASS**（会话目录出现 `jujing-bot-team`） |
| DAG 实跑（DSH `workflow`，4 个角色代理，s1‖s2→s3→s4） | **PASS** |
| 报告契约（`validate_report.py`，41 项） | **PASS**（finding=2 / cannot_attribute=12 / scorecard 六维 / recommendation=4） |

回执与已知问题（U1–U5）：[runs/TEST-RECEIPT-2026-10-18.md](runs/TEST-RECEIPT-2026-10-18.md)。
封装件：`dist/bot-team-dsh-2026-10-18.zip`（59.7 KB，33 文件，sha256 见 `dist/manifest-2026-10-18.txt`）。

## 设计边界（诚实）

- **结构 ≠ 效果**：本包保证"团队结构可跑、产出 schema 可检"；归因是否正确要靠真实数据（见 `docs/BOT_PILOT_SPEC_2026-10-18_zh.md` 的数据合同）。
- **demo 数据是演示数据**，不是工厂真实数据；不得据此对外宣称归因准确率。
- **DSH Agent Teams 是实验特性**（≤8 人、共享 cwd、无跨进程保证）；生产/多租户不在本包承诺内。
- **不写业务代码**：包内只有结构/资产/校验/编排，后续开发按工作流 G1 起卡。


