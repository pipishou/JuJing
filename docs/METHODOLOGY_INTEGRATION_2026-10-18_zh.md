# 方法论 × bot 团队 × 四段闭环：整合方案（2026-10-18）

> 输入：用户《制造业企业级AI经营框架 v1.3》（851 行）＋本包 v0.4 四段卡＋竞品基准。
> 原则：**不重复造轮子**——方法论已有的（五层引擎/六维评分卡/决策日志/决策契约/降级/回滚）直接复用；外部已有的（检索 API、GitHub、标准框架）直接接；只自研"制造业因果资产 + 舞台/交付卡"。

## 0. 结论

1. **bot 团队不是另一个产品**，它是方法论 L0–L2（感知/归因/建议）的**可执行班组**；四段闭环是方法论"决策日志轴心"的**可机检外壳**。
2. 方法论补上了 bot 团队最缺的三块：**六维评分卡（100 分制+硬护栏）**、**决策契约（决策单元/行动/成本/护栏/负责人）**、**失败模式与回滚（连续 3 次方向错误暂停 AI 建议）**。
3. 四段闭环补上了方法论落地最缺的三块：**证据卡（外部来源可追溯）**、**分析卡（权重/敏感性/反面/预演失败）**、**闭环卡（actual_result/命中率/棘轮）**。
4. 组件选择：检索用 DeepSeek API；决策/回看用 GitHub Issues + Actions；分析用 Pugh 矩阵 + pre-mortem；闭环用 AAR/PDCA；校验用 JSON Schema；**不自建**搜索、数据库、调度器、IM、ML 平台。

## 1. 方法论 → bot 团队映射

| 方法论（v1.3） | bot 团队落点 | 现状 | 差额动作 |
|---|---|---|---|
| L0 感知（μ±2σ） | 咨询分析师/取数员 + `assets/perception_rules.yaml` | 有快照，无异常检测规则 | ✅ 新增 perception_rules |
| L1 归因（因素排除法 6 步） | AI 归因专家 + `assets/exclusion_six_steps.yaml` + `prompt_crp.yaml` | 已有 | 保持 |
| L1 六维评分卡（100 分制） | `assets/scorecard_six_dim.yaml` v1.4 | 原为 5 分/维、无护栏/阈值 | ✅ 增加 16.67 换算、硬护栏、80/60/40 阈值、CSOLR 校准计划 |
| L2 建议（行动+成本收益） | 业务专家 + `assets/roi_anchors.yaml` + `irreversible_actions.yaml` | 已有 | 用决策契约约束行动集合 |
| L3 执行（工单/CRM，延后） | 工作助理 + 交付卡签字（人执行） | 原型签字 | 不接工单，保留"只呈请不执行" |
| L4 学习（因果/增量价值，延后） | 记录员 + 闭环卡 + CSOLR | 只有回看文案 | ✅ 闭环 schema 加 increment_value/calibration |
| 决策日志（轴心） | `team/loop/decision-record.schema.yaml` + 交付卡 | v0.4 已有四组字段 | ✅ 对齐方法论字段（entity_id/context/recommendation/actual_action/…） |
| 决策契约（场景级） | `assets/decision_contract_refill.yaml` + crews | 无 | ✅ 新增，班组启动时装载 |
| 数据质量 A/B/C + 三级降级 | `assets/perception_rules.yaml` + 数据合同 | 有概念，无阈值文件 | ✅ 10%/30% 降级阈值写入 |
| 失败模式/回滚 | `perception_rules.failure_modes` + 闭环卡 failure_mode | 无 | ✅ 先写规则，真实任务验证 |
| 组织角色/归责 | UI 中"老板签字 + 复核人"；用户是最终拍板 | 有签字原型 | 不把 AI 写成决策主体 |
| 三场景 | crews：补料率/停机/周报（周报≠产量） | 有 4 班组 | P1 把"产量提升"补成第三场景 |

## 2. 四段闭环 → 方法论字段

| 四段卡 | 方法论对应 | 必填字段（新增） |
|---|---|---|
| 证据卡 | 决策日志"当时上下文"的输入证据 | `data_quality_grade(A/B/C)`；外部来源用 CRAAP 分级 |
| 分析卡 | 六步排除 + 六维评分 + 决策契约 | `attribution_method`、`degradation_level`、`scorecard`、`decision_contract` |
| 决策记录 | 决策日志四组分类 | `entity_id/metric/context/recommendation/actual_action/override_reason/attribution_method/outcome_window/action_cost/increment_value/calibration_version` |
| 闭环记录 | 评估信息 + 失败模式 + CSOLR | `increment_value/attribution_method/scorecard_snapshot/failure_mode/calibration_version` |

状态机不变：`draft→researched→analyzed→decided→delivered→closed`；G1 证据门、G2 分析门、G3 决策门、G4 闭环门。

## 3. 组件选择（不重复造轮子）

| 段 | 选什么现成组件 | 为什么不自己造 | 落地方式 |
|---|---|---|---|
| 检索 | **DeepSeek 检索 API**（已有 `ds_search.py`） | 搜索基建/索引/排序不是差异化 | query→证据卡 JSONL |
| 证据评级 | **CRAAP**（Currency/Relevance/Authority/Accuracy/Purpose） | 成熟的信息素养框架 | 映射到 `grade/freshness/stance` |
| 分析 | **Pugh 矩阵/加权决策矩阵** + **pre-mortem（Klein）** | 决策科学已有标准 | 分析卡字段 |
| 决策 | **方法论决策日志 schema** + **MADR/ADR** 表达 | 方法论已定义四组分类，不另造 | 决策记录 + 交付卡 |
| 决策跟踪 | **GitHub Issues + Projects**（公开可审） | 不自建任务板/数据库 | Issue = 决策卡；Projects 字段=状态/置信/复核日 |
| 回看提醒 | **GitHub Actions cron**（或本机计划任务） | 不自建调度器 | 到期自动开 review issue |
| 闭环 | **AAR（After Action Review）四问** + **PDCA** | 组织学习成熟框架 | 闭环卡模板 |
| 治理 | **NIST AI RMF**（Govern/Map/Measure/Manage） | 方法论已引用 | 权限/失败模式/归责映射 |
| 校验 | **JSON Schema + 现有 validator** | 不自建规则引擎 | `validate_structure.py` 扩展 |
| 存储 | 本机 JSONL/Excel（方法论已给降级方案） | 不自建 DB/向量库 | `runs/task-*/` + 私有目录；公开库只放 schema/demo |
| 校准 | **有序 Logistic 回归**（statsmodels，≥50 条才用） | 不自建 ML 平台（CSOLR 零基础设施） | 评分卡版本 JSON |
| UI | 现有**舞台 + 交付卡 + tape** | 不自建聊天框/工作台 | 四卡链接到交付卡 |

**只自研的 3 件**（有护城河）：
1. 制造业因素枚举/排除规则/决策契约/ROI 锚点（领域资产）；
2. 舞台过程回放（DSH 事件→tape）；
3. 可签字交付卡（老板视角的成果形态）。
其余全部用现成组件。

## 4. GitHub 存放（公开仓库，已入库）

已确认 `pipishou/JuJing` 公开并已入库（commit `ef609e8`）。当前结构：

```text
JuJing/
├─ jujing-agent-workflow/     # 现有
├─ jujing-junshi/             # 现有
├─ bot-team/                  # 本包公开安全子集
│  ├─ team/ assets/ demo/ orchestration/ tests/ ui/ templates/ docs/
│  └─ 不放：dist、runs/task-*、.junshi、真实数据、原始会话日志
├─ docs/                      # benchmark / four-loop / methodology-integration / playbook / repo-plan
└─ .github/workflows/validate.yml
```

**公开边界**：方法论原文（个人 IP、含集团信息）**不进公开库**；公开库放"整合后的判据/模板/代码/demo"。
`bot-team/docs/ledger.md`（项目内部账本）也不进公开库；需要时放私有仓库。

## 5. 落地顺序（本周）

1. **资产对齐**：scorecard v1.4、perception_rules、decision_contract_refill（✅ 已完成）。
2. **四卡字段对齐**：evidence/analysis/decision/closure 加方法论字段（✅ 已完成）。
3. **班组装载契约**：c-refill 启动时读 decision_contract（P1 代码）。
4. **明天真实任务**：按 `REAL_TASK_PLAYBOOK` 跑；决策日志字段按方法论填；报告附六维评分卡。
5. **公开仓库**：`bot-team/` 公开安全子集已进 JuJing（commit `ef609e8`）；方法论原文/真实数据/账本仍在私有侧。
6. **校准**：前 30 条冷启动等权；≥50 条再跑有序 Logistic 回归，不提前上 ML。

## 6. 风险与边界

- 方法论 v1.3 的评分卡权重/阈值尚未在真实数据校准：先冷启动等权，别把 80 分当真理。
- "因素枚举表"是封闭世界假设；漏因素会系统性失效——每季度按方法论 §3 触发器重枚举。
- 公开仓库不能放真实工厂数据；四段卡里的证据/决策默认本机私有。
- 不要把 AI 写成决策主体：签字/归责永远在人。
