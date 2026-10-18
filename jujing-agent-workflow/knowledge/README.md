# 记忆调用判据表（路由权威）

> M1 记忆系统轻量版（2026-09-28 拍板）：记忆＝本 skill 内 `knowledge/`＋`preferences/`（M3-lite 个人层）＋`data/`，不建独立 git 仓库；**分享 skill 文件夹＝分享记忆**；三步接入见 `references/routing.md` §七。本表是「什么任务优先读哪层」的唯一权威——路由先于加载，先查本表再读记忆。

## 一、调用判据（任务 → 优先读哪层）

分层理念源自参考文章《一文搞懂个人AI记忆系统构建全流程》（2026-08-27）；L1–L5 使用方式判据以 `params.json#memory.weight_tiers` 为唯一事实源（零硬编码）。个人层激活状态以 `params.json#memory.personal_layers` 为唯一事实源：**preferences 已建（M3-lite，2026-09-28）**；identity／principles／context 为 M3 候选未建（pending，偷建目录＝kb-lint ⑩ 红）。

| 任务 | 优先读（现状） | 层路由 |
|---|---|---|
| 写方案 | knowledge/ 方法可复用类条目（方法论、模板）＋**preferences/ 工作风格与沟通偏好（已建，L3）**；用户当前表达偏好（会话中直接问） | preferences（L3，已生效）＋principles（L2，待建） |
| 做计划 | knowledge/ 经历复盘类条目（历史经验）；INDEX 当前状态列 | context（L4，待建——项目级状态读项目侧档案）＋principles（L2，待建） |
| 工作复盘 | INDEX＋`data/probe.jsonl` 消费记录（真实轨迹，不凭印象） | context（L4，待建）＋knowledge（L5） |
| 评审设计文档 | knowledge/ 评审踩坑类条目＋references/pitfalls.md＋**preferences/ 工作风格偏好（减法/门先行）** | knowledge（L2/L5）＋preferences（L3，已生效） |
| 换工具／分享给合伙人 | `routing.md` §七 三步接入；全量记忆随 skill 文件夹走 | preferences（已建）＋identity/context（待建） |

**context 层裁决口径（M3-lite 定，建层前生效）**：跨项目通用 context（工作环境/常驻工具链/协作时段）才进个人 context 层；**项目级当前状态一律留项目侧档案**（`assets/profile-template.md` 建档件），两层互不收编——防双源漂移。

**L1–L5 使用方式**（判据文本唯一事实源 `params.json#memory.weight_tiers`）：L1 硬约束默认必守、违反须显式说明；L2 判断框架任务相关时优先；L3 偏好影响形式不覆盖事实；L4 上下文注意时效、高影响决策前向用户验证；L5 经验素材只类比不套用。「决策是我来做」不受权重影响。

## 二、写入协议（curator 三步＋人工决策门）

会话中说「记一下」→

1. **先提取**：按本表路由产出草稿（层／标签／锚点建议＋归类理由）。归类须引用 `params.json#memory.ownership_criteria` 四判据：价值判断还是操作步骤／有没有具体日期／外部输入还是自己提炼／形式偏好还是价值判断（M3-lite 新增：协作与表达的形式偏好→preferences 层）。
2. **再确认**：用户确认内容与归档位置，排除理由必写，可改判归属。
3. **后写入**：走 `INDEX.md` 归档协议（一档一条目、锚点注册进抽检池、溯源必填）。元数据 `隐私（privacy）` 缺省取 `params.json#memory.privacy_default`（public），涉个人敏感／商业机密打 `internal`；`状态` 值可为 `archived`（人工判旧，召回默认跳过；值域见 `params.json#memory.privacy_values`／`params.json#memory.status_values_extra`，kb-lint ⑧ 机检）。

**诚实边界**：「AI 读了 internal 条目，对外输出时跳过其内容」是行为协议承诺，无法机检——不夸大。**晋升／退役／准入／冲突四类决策永远留人**（D1 扩展；与文章「故意不把记忆维护做成全自动」和聚鲸内核双重对齐）。

**留痕纪律（M2 多人共享）**：消费留痕（`probe_usage.py record`）与纠偏留痕（`wal.py add`）必带 `--actor <id>` 且须在 `params.json#memory.actors` 准入登记（未登记／缺省即拒——无默认人，防匿名留痕洗白成佐证）。登记＝合伙人接入第三步（`routing.md` §七）。佐证口径按 `params.json#memory.endorsement.mode` 切档：`dual_track`＝独立源解锁但同人标弱佐证；`cross_actor_only`＝仅跨人（同一锚点 ≥2 已登记 actor）解锁；M1 前无 actor 历史行＝弱佐证诚实标注，不洗白不改写。

## 三、轻量版边界（诚实）

- 无 git＝无逐档历史回退：合并脚本 `--apply` 写入前自动 zip 备份 `data/`＋`knowledge/`＋`assets/`（快照落 `../jujing-backups/`）；其余重要变更前手动备份 skill 文件夹。
- 两份拷贝（你／合伙人）各自生长后的合并：**M2 合并协议已落**——`python scripts/merge_knowledge.py --theirs <对方skill根>`（dry-run 默认只报告；`--apply` 才写）。jsonl 行级合并（全等去重＋零丢行断言）＋wal ID 冲突检测＋条目同名异内容冲突；**冲突绝不自动改写**，人工按消费留痕多者/时间新者裁决，理由一行记 `evolution.md`，改后重跑至零冲突（协议详见脚本 docstring）。INDEX/params 两个权威不自动合并，人工并表后必须跑 `kb-lint` 全绿收口。
- 记忆条数／大小只有观测没有门（拍板 4 重申：不设硬上限，`kb-lint ⑦` 是结构纪律非大小门）。
