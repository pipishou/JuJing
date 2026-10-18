---
name: jujing-bot-team
description: 聚鲸 bot 团队结构包（制造业经营归因四角色：工作助理 host＋咨询分析师＋AI 归因专家＋制造业业务专家）；五步 DAG 协作、8 个判据资产、5 份演示数据、可机检校验与决策日志。Use when 用户提到 bot 团队、制造业归因、补料率异常、组建团队、跑归因演示、迁移 bot 团队时使用。不写业务代码，不执行不可逆动作。
---

# 聚鲸 Bot 团队（DSH 版）· 结构包 v0.2

> 本 skill 把一个**可迁移的 bot 团队结构**装进 DeepSeek Harness：6 员工名册（含货架 6 态）、4 套班组模板、五步 DAG、行业资产、交付卡（可签字）、双运行模式、可机检校验。
> 定位：**结构可跑、可测、可迁移**；不承诺归因正确率（那要靠真实数据与 M1 试点验证）。

## 0. 快速判断是否该用

- 用：要组建/运行「制造业经营异常归因」的 bot 团队、要跑 demo、要迁移团队结构到另一台机器。
- 不用：单点问答（如"保养多少钱"）——host 直接答，不必编排；纯代码任务。

## 1. 团队名册（唯一事实源 `team/roster.yaml`）

| 角色 | 层 | 章程 | DSH 映射 |
|---|---|---|---|
| 工作助理 host | L3 | `team/roles/host.md` | Lead（加载本 skill 的会话主 agent） |
| 咨询分析师 | L0 | `team/roles/consulting-analyst.md` | `spawn_teammate` 成员 analyst |
| AI 归因专家 | L1 | `team/roles/ai-attribution-expert.md` | `spawn_teammate` 成员 attribution |
| 制造业业务专家 | L2 | `team/roles/manufacturing-ops-expert.md` | `spawn_teammate` 成员 business |

## 2. 两种运行模式

### 模式 A：确定性 workflow（推荐用于测试/演示/迁移验收）

把 `orchestration/team-workflow.mjs` 交给 DSH 的 `workflow` 工具，`args` 传：

```json
{ "base": "<本 skill 根目录绝对路径>" }
```

脚本会按 `team/dag.yaml` 跑 s1‖s2 → s3 → s4，返回 JSON；host（Lead）再按 `team/gates.md` 收口成 `runs/demo-report.json`，跑 `tests/validate_report.py`。**无需常驻成员、可重复、可离线复核。**

### 模式 B：Agent Teams 交互（推荐用于真实协作/演示 UI）

1. 读 `team/roster.yaml` 与 `team/dag.yaml`；
2. 按三份角色章程 `spawn_teammate` 建 analyst / attribution / business；
3. 用 `team_task_create` 建 s1–s5（按 `depends_on` 设 blocker），派给对应成员；
4. 预派发确认（G0）后再派发；某步失败回退重排或换角，不无脑重试；
5. host 收口报告与决策日志，跑两个校验脚本。

> Agent Teams 是 DSH 实验特性：单进程共享 cwd、名册扁平（≤8 人）、无跨进程保证；不适合当生产多租户。

## 3. 五步 DAG（`team/dag.yaml`）

`s1 快照（分析师）‖ s2 因素表（业务专家）→ s3 排除法六步（归因专家）→ s4 方向校验＋ROI（业务专家）→ s5 报告＋决策日志（host）`

## 4. 产出契约（s5 必填）

`finding`（≤2，带 evidence）｜`cannot_attribute`（**强制非空**）｜`scorecard`（六维）｜`recommendation`（action + roi 三锚点 + irreversibility）｜`decision_log_entry`（四分类 + clause/trace）。

## 5. 门与纪律

- 预派发确认（G0）＋ securityGate（不可逆动作二次确认）——见 `team/gates.md`；
- 降级：空值率>10% 转人工；候选≥2 补反事实；连续 3 次失败暂停；
- 封闭世界：只在 `assets/factors_table.yaml` 11 类因素内归因，表外拒答；
- 数据缺失写 missing，不补数不插值不猜。

## 6. 校验（交付前必须绿）

```bash
python tests/validate_structure.py           # 结构/名册/DAG/资产/演示数据完整性
python tests/validate_report.py runs/demo-report.json   # 报告 schema＋断言
```

任一非零 ⇒ 不得声称交付完成。

## 7. 安装与迁移

- 安装：把本目录整体复制到 `<dshHome>/skills/jujing-bot-team/`（默认 `C:\Users\<你>\.dsh\skills\`），DSH 自动发现，无需重启。
- 迁移：见 `MIGRATION.md`（含工作电脑步骤与验收清单）。
- 目录：`team/` 名册·章程·DAG·门 ｜ `assets/` 8 个判据 ｜ `demo/` 5 份演示数据 ｜ `orchestration/` 确定性 runner ｜ `tests/` 机检 ｜ `runs/` 运行回执 ｜ `ui/` UI 概念原型 ｜ `docs/` 方向、试点与最优版设计。

## 8. UI 概念原型（本轮新增）

- `ui/index.html`：单文件、离线可开、假数据；「AI 员工事务所·舞台实况」四区——员工货架/班组编排/舞台实况/交付墙。
- 用途：G0「第一眼」评审与视觉方向定稿，**不是产品实现**；设计定稿见 `docs/OPTIMAL_TEAM_AND_UI_2026-10-18.md`。
- 三源取舍：DSH 引擎（不改）＋行家来了的员工/班组/货架/交付卡/反壳纪律＋Octop 多用户渠道仅作 P1 可选；舞台数据接 DSH 会话事件（JSONL.zstd）适配器（`orchestration/dsh-session-tape.mjs`，已跑样例 379 beats/229 tools）。
- v0.2.1：角色改为原创像素小人（`ui/sprites.js`，Kairosoft 风格参考，不搬素材）；员工/班组数据从 `team/*.yaml` 生成 `ui/team-data.js`。
- v0.3：角色帧动画（walk/blink/talk/happy/think）＋真实会话 tape 回放（`tools/gen_tape_ui.py` 生成 `ui/tape-data.js`；UI 切换「真实 tape」）。
- v0.3.1：接单向导（`ui/index.html` 新建任务）＋真实任务手册（`docs/REAL_TASK_PLAYBOOK_2026-10-19_zh.md`）＋竞品基准（`docs/PRODUCT_BENCHMARK_2026-10-18_zh.md`）；工作流 `args.data_dir` 支持真实数据目录。
- v0.4：四段闭环卡（`team/loop/`：evidence/analysis/decision/closure schema + 状态机 + example）；交付物必须能回答"证据够不够/分析漏没漏/谁拍的板/结果回来没"。
- v0.5：与方法论《AI智能经营模型 v1.3》对齐——`assets/scorecard_six_dim.yaml` 100 分制/硬护栏/阈值/CSOLR；新增 `assets/perception_rules.yaml`（L0 检测+三级降级+失败回滚）、`assets/decision_contract_refill.yaml`；四卡补齐决策日志字段。
- v0.6：关系/记忆（`team/memory/`）、Goals（`team/goals/`）、成本透明（`team/cost/`）、企微出站（`team/integrations/wecom.yaml`＋`tools/wecom_notify.py`）；UI 加目标进度/记忆/企微推送。
- v0.7：真实数据入口（`tools/ingest_task_data.py`/`check_data_contract.py`）、真实报告门（`assemble_report --real`/`validate_report --require-real`）、评分卡计算（`score_scorecard.py`）、签字持久化（`sign_delivery.py`，拒绝 demo 签字）；`templates/field-map.example.yaml` + `tests/real_mode_smoke.py` 用合成 11 列数据跑通全链。
- v0.8：四段闭环 bundle 自动写入（`assemble_report --records-dir` → `tools/write_loop_bundle.py` 写 evidence/decision/closure，analysis/goal/cost 由入参拷贝）+ `tools/validate_loop.py --require-complete` 门。
- v0.8.1：`tools/cost_record.py`（成本记录/汇总）、`tools/memory_retrieve.py`（员工记忆检索，隐私/过期过滤）、`tests/negative_gates.py`（demo/叙述/缺数据必须被拒）。
- v0.9：UI 数据绑定（goals/memory/cost 生成数据）、`tools/review_due.py`（T+7 回看→GitHub issue）、`tools/cost_monthly.py`（成本月报）、`wecom_notify.py --report`（交付卡自动填充）；`.github/workflows/review-due.yml`。
- v0.9.2：`tests/wecom_transport_smoke.py` 用本地 HTTP server 验证企微 POST payload/成功/失败路径（不连真实企微、不需要 webhook）。


