# 自优化协议（AI+数据闭环）

> 本 skill 的核心差异化能力：不仅是一套静态方法论，而是一个**能被自身使用数据持续改进的活系统**。对齐 SEAK 四齿飞轮＋darwin 优化循环＋消费探针三源。

## 总览：四齿咬合

```
信号齿(使用中AI记录) → 积累齿(数据+WAL晋升) → 落地齿(棘轮版本) → 控制齿(人类确认)
        ▲                                                        │
        └────────────── 消费探针回流(证据/缺席告警) ←──────────────┘
```

**组件都做了但箭头没接＝大多数自优化系统的死法。** 每一轮使用结束时，四个齿必须各转一格（见「每轮协议」）。

## 四能力域自优化映射

| 能力域 | 数据来源 | 沉淀位置 | 晋升路径 |
|---|---|---|---|
| **团队能力** | G2.5 对抗记录、角色实效观察、协作摩擦 | `data/wal.jsonl`（type=lesson/pattern, clause=角色/门） | 判例库→SKILL.md 角色表/门禁修订 |
| **代码能力** | 修复事故、机检漏项、测试复利模式 | wal（clause=测试/机检） | 新机检项→stages/04 动作清单 |
| **架构设计** | 架构决策、流/常判定、退役事件 | wal（clause=架构/退役） | 模板化→roles-and-gates.md 治理七件套 |
| **产品设计** | 用户否决理由、G0 打回、北极星偏移 | wal（clause=G0/北极星/减法） | 乔布斯判例→stages/02+07 判据修订 |

每个域的晋升都必须经棘轮（见下），不是改了就算。

## 两区关系：快记（信号区）→ 慢提（经验区）

| 区 | 载体 | 形态 | 节奏 | 判据 |
|---|---|---|---|---|
| 信号区 | `data/wal.jsonl` | **自由文本行**（`--text` 一段话，超软上限补 `--ref` 外链详版） | 当场记、零摩擦 | 见 `params.json#wal` |
| 经验区 | `knowledge/entries/` | **结构化元数据**（`params.json#knowledge.required_meta` 七字段：标签／形态／锚点／状态／现落点／溯源／隐私） | 晋升时提炼（`promote-check` 候选 → 人工确认） | `kb-lint` ③ 逐字段核 |

**为何两区形态不同（锁定批·J3：已落地但棘轮未判严格提升、未升号——判据见 `references/evolution.md` §五）**：信号区要「无感化」——记录时加字段＝加摩擦，摩擦一高就没人记，飞轮断在第一齿；经验区要「可抽检可追溯」——没有锚点与元数据就抽不到、追不了。故**结构化不是漏做，而是移到晋升环节**：`promote-check --digest` 自动拼材料供人提炼，条目落 `entries/` 时按七字段成形（`kb-lint` ③ 机检）。
**判定口径**：在信号区要求结构化＝错位的加法（只增摩擦）；在经验区缺元数据＝真缺口（`kb-lint` 红）。**同一份内容不必两处同形**——`wal` 行是「发生过什么」的原始证据，`entries/` 条目是「以后怎么做」的判据。

## 三个执行器（scripts/，标准库零依赖）

### 1. wal.py — 信号与积累

```bash
# 任何纠偏/教训/新模式，当场入库（append-only，永不改写历史行）
python scripts/wal.py add --source user --type correction --text "G0 对齐时用户打回：要的效果说反了" --clause "SKILL.md#G0"
#   --source: user|ai   --type: correction|lesson|pattern   --clause: 关联条款锚点

python scripts/wal.py list [--clause "SKILL.md#G0"] [--open]     # 查看记录
python scripts/wal.py promote-check [--min <阈值>]               # 同类(type+clause)次数达阈值未晋升 → 输出晋升候选
#   --min 缺省取 assets/params.json#wal.promote_min（本文不复制数值，避免两处漂移）
python scripts/wal.py mark-promoted --ids 12,13,14 --to "v1.2.0" # 人工确认晋升后标记（追加状态行）
```

### 2. rubric_score.py — 落地齿（棘轮）

```bash
# 打分（维度与量程取 assets/params.json#rubric，本文不复制），追加到 data/scores.jsonl
python scripts/rubric_score.py score --version 1.1.0 --s "结构完整:9" "边界覆盖:9" "指令具体性:8" "实证密度:9" "可执行性:9" "防自欺:9" "可演化性:8" "负边界:8" --note "补充示例批次后"

# 版本历史
python scripts/rubric_score.py history

# 棘轮判定：配对校准优先——跨裁判实例绝对分不可比（各版本分数出自不同全新上下文实例，尺度漂移致绝对分退化）
python scripts/rubric_score.py ratchet
#   行带 anchor_avg（score 时加 --anchor-ver ＋ --anchor-s 逐维锚分【首选：均分由脚本复算入档，堵单点谎报】或兼容 --anchor-avg 手传：同一裁判实例先评冻结锚得 X、再评当前批得 Y）→ 比配对差值：
#     Y > X 且 Y ≥ params.json#rubric.ratchet_floor（地板＝双低防护：裁判整体偏松时锚批同涨、差值失真）→ ALLOWED
#     否则 → LOCKED
#   行无 anchor_avg → 退回旧行为：与历史最高绝对分比（向后兼容）
#   版本号只进不退；ALLOWED 后以本批改动后核心样本冻结新锚 data/calibration/anchor-<新版本>.md（旧锚归档不删）
```

维度定义：清单唯一事实源＝`assets/params.json#rubric.dims`（**本文不复制清单**，防两处漂移；改维度即改判据，须走棘轮）。行为锚唯一事实源＝`assets/params.json#rubric.anchors`（BARS 三档 9/7/5：每维「几分＝什么可验证证据」，独立裁判按锚打分以收窄跨实例方差——锚文本仍文档引用；#36 起锚分入档有代码消费：score --anchor-s 校验逐维分＋锚文件存在性）。打分依据真实使用批次，不凭印象。

### 3. probe_usage.py — 消费探针（防条款空转）

```bash
# 条数与窗口取 assets/params.json#probe（本文不复制数值，避免两处漂移）
python scripts/probe_usage.py sample          # 轮转优先抽检（条数＝配置 sample_n）
python scripts/probe_usage.py record --clause "G2.5" --evidence "卡 comment 含双裁判独立结论" --source witness
python scripts/probe_usage.py record --clause "成本四行" --none          # 无消费证据
python scripts/probe_usage.py retire-check                              # 连续「配置 window」轮无消费 → 候选退役
python scripts/probe_usage.py heat                                      # 条款热度回流榜（命中加分＋空转扣分；权重带符号，空转权重为负；观测非门）
python scripts/probe_usage.py wording --since 2026-10-06                 # 措辞绝对化观测（只报不判、恒 0 退出；scope=changed 按文件 mtime 圈定本次改动件）
python scripts/probe_usage.py selfcheck                                 # 机检断言自检（把文档绝对化断言搬进可跑复算；隔离库跑完即删）
```

退役五判据：被通用能力覆盖／补偿旧模型缺陷（立即删）／长期零命中／误报超标／业务不存在。退役也走版本号＋棘轮，保留案例可溯性。

**热度回流（锁定批·D2·正向加分）**：`heat` 把探针痕迹按**条款**聚合——加分＝独立源真消费命中，扣分＝**未记消费证据**（`consumed=false`，即抽到了却用不上＝空转；不止 `--none` 一种路径）；权重与榜单条数上限取 `params.json#governance`（本文不复制数值；**权重带符号**——空转权重为负且绝对值大于命中权重＝罚重于奖）。它补的是飞轮缺掉的一半：`retire-check` 只看「有没有痕迹」（零消费→退役候选），`heat` 才看得见「用得多不多、有没有白用」。
**为什么是观测不是门**：热度受抽检轮转、条款粒度、窗口长短影响，把它做成门＝拿噪声开罚单（也违公理 1「是判断才留给人」）。故 `heat` **常态恒 0 退出**、不进任何机检码（非 0 路径＝**配置错误一类**：`params.json` 缺失/解析失败、必填节或子键缺失、`clauses` 池为空 → fail-closed 报错，不是判定），产物只作晋升/退役评审的参考输入。同理**零痕迹≠零价值**（可能只是没被抽中）：公示而不判罪，扣分条款全量公示不藏坏消息。

**`heat` 自身的健康判据（防「观测件」变成死功能）**：连续 `params.json#probe.retire_window` 轮零调用 → 转退役候选（回 `references/evolution.md` 登记表评估，退役时其 `governance` 三键与本文/`SKILL.md` 措辞一并对齐）；榜单被误当门使用（有人据此自动退役条款）→ 收窄输出措辞并在评审口径重申「观测非门」。**注意热度窗口与 `retire-check` 尾窗不同**（前者 cutoff 后全史、后者尾窗轮数）——两者不可互相佐证。

**措辞绝对化观测（`wording`，判据 `params.json#wording`；锁定批·甲 ②-B）**：与 `heat` 同构的**观测非门**——列清单、不判罪、常态恒 0 退出、不进任何机检码。*为何不是门*：原提案＝把「绝对化措辞未列例外」做成机检门，实测命中 95 行次、其中 77 处未带例外，**主体是合法用法**（「永远红」＝陷阱描述、「一切判据值只存 params.json」＝铁律、「恒 ImagePullBackOff」＝状态描述、「唯一入口」＝正当限定）⇒ 判定面过宽 ⇒ 永远红 ⇒ 被绕过（`pitfalls.md` #19）；且「这句措辞是否**失实**」是**判断**（公理 1：是事实就机检，是判断才留给人）。故机器只把清单端到眼前（`--since` 圈定本次改动件＝文件级 mtime，非行级；缺省＝全树只报），改写为「常态…（例外：…）」或挂 `params-exempt:理由` 由人做。
**断言机检化（`selfcheck`；锁定批·甲 ②-A）**：绝对化断言分两类落点——**可复算**者（退出码／覆盖数／悬空链／拒绝规则）不写在散文里，写成 `selfcheck` 的隔离负测试（逐条打印「断言→期望→实测→OK/FAIL」，任一 FAIL 即红；wal 侧走 `JUJING_DATA_DIR` 临时库、跑完即删，真实台账零污染）；**属判断**者（措辞是否失实、同类性判定）留给人，但须留痕可审（`--cohesion`）。这是公理 1 的**双向纪律**：既不许把判断塞给机器，也不许把事实留给记忆。

### 3.5 effect（效果实测，复用探针脚本，不加新执行器）

```bash
# 冷启动 / 每轮协议第 4 步：抽题 → 双轨跑 → 诚实记录
python scripts/probe_usage.py effect --list                                   # 看题集
python scripts/probe_usage.py effect --id 1 --mode full_test --verdict pass \
  --note "带skill按scale_tiers档选编制，baseline未做裁剪"                       # 真跑子agent
python scripts/probe_usage.py effect --id 2 --mode dry_run --verdict fail \
  --note "漏了用户视角断言，判为绿测掩盖"                                       # 模拟推演
```

- `--mode`：`full_test`（真跑带/不带 baseline 双轨）或 `dry_run`（模拟推演）；跑不了真测就标 `dry_run`，不冒充 full_test。
- `--verdict`：`pass` / `fail`；`--note` 必填，写清「带 skill vs baseline 差在哪」，空话=无效（防自欺）。
- 数据落 `data/effect.jsonl`（append-only）。题集 `assets/test-prompts.json` 是本体（常），随使用只增不减。

## 每轮协议（每次使用本 skill 的项目会话结束时执行）

1. **信号**：本会话的纠偏/教训/新事故 → `wal.py add`。**「敢于不沉淀」判据（锁定批·J1）**：留痕下限见 `params.json#wal.per_round_min`（语义＝**无强制下限**）；**四情形命中任一才须新增行**——① 用户明确纠偏/否决；② 发生事故、返工或假绿被拆穿；③ 某条判据被证伪或修订；④ 出现可复用新模式（`--type pattern`）。四情形全不中 → 记「本轮无新增」空转证据即为合规（不是敷衍）；**凑数行是负收益**：无信息行污染 `params.json#wal.promote_min` 候选与 digest 评审——密度>体量。
2. **积累**：`wal.py promote-check`——有候选则整理成条款草案（晋升材料由 `--digest` 自动拼，含 chain 修正链与消费时序）。**先判同类性再谈晋升**：分组键 `(type, clause)` 的 clause 是**大类锚点**，「同组」只共享上位公理、**不表示同类**；`--digest` 强制打印三问（`params.json#wal.cohesion_questions`：同一操作／同一判据／可否合成一条动作），`mark-promoted` 必带 `--cohesion` 逐条回答（长度下限 `params.json#wal.cohesion_min_len`）并随晋升行留痕——**三条异质教训合并＝无单一动作的口号**（公理 1：缺机检项只记候选）。同根复发**换了锚点**时聚类键看不见，故并行用机器可读 `--chain <父id,...>` 记修正链（悬空/倒指＝kb-lint ⑨ 红，跨锚点链计数公示）。
3. **探针＋热度回流**：`probe_usage.py sample` → 逐条 `record`（有证据/无证据都记，诚实第一）→ `probe_usage.py heat` 看条款热度榜（正向命中加分／空转扣分，权重取 `params.json#governance`）。热度只作晋升与退役评审的**参考输入**——**观测非门**：低热度≠该退役（可能只是没被抽中），高热度≠免审。
4. **效果实测**：从 `assets/test-prompts.json` 抽题（条数上限＝`params.json#probe.effect_sample_max`），用「带 skill vs 不带 baseline」双轨跑一遍 → `probe_usage.py effect` 记录（`full_test` 真跑子 agent ／ `dry_run` 模拟推演，二选一诚实标注）。**探针问「有没有痕迹」，effect 问「痕迹对不对」**——条款有痕迹但效果错，同属公理 5「绿测掩盖死功能」。这是「复用性 🟡」的唯一解药：题集是本体（常），随使用只增不减。
5. **落地**：候选条款攒齐一个改动批次（固化新纪律＝判据＋载体＋机检三件齐，载体槽位/机检项登记模板 `assets/templates.md` §7.1/7.2）→ 跑 `rubric_score.py score`（改版前基线+改版后）→ `ratchet` 判定。
6. **控制**：向用户呈现晋升/退役清单（批量选择题格式），五个人类确认点：WAL 晋升确认／skill 终审／版本晋升确认／负边界变更确认／安全红线变更确认。**并呈现待回看清单**：批次回执「待回看」字段到期（交付日＋`params.json#followup.default_days`）的条目——有消费证据→按 `params.json#followup.statuses` 标 reviewed 并留痕；无证据→如实标注（与探针「无消费证据」同权重，防自欺）。

## 版本与棘轮纪律

- 版本号语义：主版本=结构变化；次版本=新增/修订条款；修订号=勘误。
- **棘轮规则（配对校准优先）**：新版本以真实批次实测证明提升才换号发布——判定优先用**同裁判实例配对差值**（先在改动前冻结锚，再由同一实例评锚 X 与当前批 Y：Y>X 且过 `params.json#rubric.ratchet_floor` 地板＝双低防护）；行无锚字段才退回历史最高绝对分。否则改动回滚或继续迭代。分数只进不退；ALLOWED 批以本批改动后核心样本冻结新锚（`data/calibration/`，旧锚归档不删）。锚须在改动前冻结——改后冻结会被本批改动污染，配对失去基准。
- 改版三问（改任何条款前回答）：哪场事故催生？谁在消费？何时退役？答不出任何一个→不改。
- 流/常检验：要固化的内容是「常」（判据/题集/机制接线）还是「流」（易变措辞/补偿性补丁）？流不固化。

## 防自欺红线

- 探针记录「无消费证据」与「有」同样重要——只记好消息的飞轮是虚荣指标。
- rubric 打分必须引用具体批次证据（`--note` 写明），否则视为无效分。
- 晋升条款必须能被探针抽到（有稳定锚点 clause），否则晋升无效。
- 本协议自身也受探针监督：若连续未执行「每轮协议」达 `params.json#probe.self_stall_warn` 轮→机制空转告警，向用户诚实报告。
