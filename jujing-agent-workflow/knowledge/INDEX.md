# 经验库索引（Knowledge INDEX）

> 分层（P3，设计定稿§六）：本索引＝**候选经验唯一清单**（陷阱 #9 防双写：清单只此一处）；详情在 `entries/` 按需读，禁止整目录读。索引大小观测线见 `assets/params.json#knowledge.index_limit_kb`（软目标，非门）。

## 消费留痕协议

> 召回提示：条目定位靠标签/形态；场景关键词（如「清档」「发版」）在条目正文与溯源行——按标签定位后读全文核对。

1. **会话消费**：会话中真用了某条经验 → `record --clause <锚点> --off-round --evidence <证据> --source witness|machine`（仅独立源；off-round 不入轮转降级判定——降级只看抽检轮，轮转抽检不被绕过）。
2. **探针抽检**：`sample` 抽中锚点 → 会话核「最近产出真有执行痕迹吗」→ `record --clause <锚点> --evidence …`（或 `--none` 如实记零）。
3. **退役**：连续 `assets/params.json#probe.retire_window` 轮零消费 → 状态改 `retire-candidate`（人工确认＋棘轮后才 `retired`）；`kb-lint` 第⑤项核证 retire 态佐证。
4. **晋升**：被 `assets/params.json#kb.promote_min_projects` 个独立项目复现 → 升正文（SKILL/阶段件），本索引行保留、状态改 `promoted`。

> **佐证口径**（kb-lint ⑤ 机检同款）：retire 态佐证与「消费可追溯」只认**独立源真消费**记录——`consumed=true` 且 source∈`assets/params.json#probe.independent_sources`（抽检轮 `record` 或 `--off-round` 会话消费均可）；`--none`（零消费）、`self` 自报、仅被 `sample` 抽中均**不解锁佐证**。
>
> **佐证口径 v2 双轨（M2 接线；拍板 2026-09-28）**：现状＝单轨（上文口径不变）；M2 落 `--actor` 后切双轨——同 actor＝弱佐证（计数但标注）、跨 actor＝强佐证。模式与口径键＝`assets/params.json#memory.endorsement`（切档＝改键值，零代码返工；铁律：全键化零硬编码，不设硬上限）。

## 索引（append-only）

| ID | 条目 | 标签 | 形态 | 锚点 | 状态 | 复现 | 现落点 |
|---|---|---|---|---|---|---|---|
| KB-01 | `knowledge/entries/KB-01-launch-four-questions.md` | 部署/发布 | 通用 | `上线四问` | candidate | 1 | 候选储备（曾落 stages⑤ 版本指纹区，09-27 下沉） |
| KB-02 | `knowledge/entries/KB-02-config-three-questions.md` | 部署/配置 | 通用 | `配置三问` | candidate | 1 | 候选储备（曾落 stages⑤ 配置面区，09-27 下沉） |
| KB-03 | `knowledge/entries/KB-03-wiring-scope.md` | 测试/机检 | 通用 | `判定面收窄` | candidate | 1 | 候选储备（stages④ 详版下沉） |
| KB-04 | `knowledge/entries/KB-04-subtraction-division.md` | 运营/减法 | 通用 | `减法分工` | candidate | 1 | 候选储备（stages③ 详版下沉） |
| KB-05 | `knowledge/entries/KB-05-red-typing.md` | 测试/断言 | 通用 | `红分型` | candidate | 1 | 候选储备（stages④ 详版下沉） |
| KB-06 | —（已升正文，无独立条目） | 验收/判据 | 通用 | `不设硬上限` | promoted | 2 | `SKILL.md` §〇＋`references/evolution.md` #18 |
| KB-07 | `knowledge/entries/KB-07-latent-defect-attribution.md` | 测试/绿测掩盖 | 通用 | `公理5绿测掩盖` | candidate | 1 | 候选储备（WAL #36/#38/#39 判例提炼；修复落点在 DeepTutor 项目侧挂账） |

> KB-06 走的是「同项目两轮复发先行上行」（#8→#18 同根二复发），不以独立项目门论——登记见 `references/evolution.md`。

## 个人层索引（M3-lite，append-only）

> 个人层激活登记表＝`assets/params.json#memory.personal_layers`（唯一事实源）：active 层目录须在盘且条目注册进本段（kb-lint ⑩ 机检）；pending 层（identity／principles／context）**未建**，待真实痛点触发扩展四问后再移入 active——空层就是空层，偷建＝红。

| ID | 条目 | 标签 | 形态 | 锚点 | 状态 | 复现 | 现落点 |
|---|---|---|---|---|---|---|---|
| PREF-01 | `preferences/work-style.md` | 偏好/工作风格 | 通用 | `减法纪律`｜`诚实降级`｜`扩展四问` | candidate | 2 | preferences 层（M3-lite 首建层） |
| PREF-02 | `preferences/communication.md` | 偏好/沟通 | 通用 | `G2.5双视角对抗`｜`决策台账` | candidate | 2 | preferences 层（M3-lite 首建层） |

> 条目真实性：两条均从产品负责人真实拍板/会话口吻提取（溯源行可溯），经开工令确认入档；AI 不代笔预填。消费留痕同「消费留痕协议」（record 带 `--actor`，锚点在池）。
> 个人层晋升门（**未建**，独立评审 2026-09-28 异议留口）：candidate→promoted **不沿用** `#kb.promote_min_projects`（独立项目门对个人层不适——复现常来自同一 owner 的多项目）；待真实痛点触发扩展四问后再定义专属门（候选口径：owner 连续 N 轮真实消费）。门未建前个人层条目只以消费留痕说话，退役照走 `#probe.retire_window`。

## 归档协议（自 knowledge-base.md 承接）

1. **移出正文 ≠ 删除**——原文先落 `knowledge/entries/`（带溯源＋状态＋复现计数），再精简正文。
2. 复现达门升正文：状态改 `promoted`，行保留不删（append-only）。
3. 真过时 → 标 `retired`，案例仍可溯（退役判据五信号：上游漂移/工具替代/判据并入他件/零消费窗口/事故根因已由机检固化）。
4. 与 `data/wal.jsonl`、`references/pitfalls.md` 分工：wal 存纠偏/教训原文，pitfalls 存可复用陷阱表，本库存候选经验中长期储备——三者不重复，各自 append-only。
