# 聚鲸 Bot 团队（DSH 版）· 可迁移结构包

> 状态：v0.6.0，2026-10-18 本机（DSH 0.2.x / Windows）搭起并测试（v0.2：6 员工/4 班组/货架 6 态/可签字交付卡/UI；v0.2.1：原创像素角色＋YAML→UI 数据＋DSH 会话 tape 适配器；v0.3：角色 8 帧动画＋真实会话 tape 回放；v0.3.1：接单向导/真实任务手册/竞品基准；v0.4：四段闭环卡；v0.5：与方法论 v1.3 对齐；v0.6：关系/记忆、Goals、成本透明、企微接入）。
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
- **v0.6 增量**：关系层 `team/memory/`（员工记忆 schema+检索规则+脱敏样例）、目标层 `team/goals/`（目标卡+进度）、成本层 `team/cost/`（AI 成本/人工节省/净收益/ROI）、接入层 `team/integrations/wecom.yaml`＋`tools/wecom_notify.py`（企微群机器人出站，20 条/分钟，dry-run 已验证）；UI 加目标进度、员工记忆、企微推送按钮。
- **迁移件**：`MIGRATION.md`＋`package.ps1`（zip＋manifest）。

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


