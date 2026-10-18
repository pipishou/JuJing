# 四段闭环升级方案（检索 → 分析 → 决策 → 闭环）

> 输入：本会话的竞品基准（Muse/GrokBot/AI 员工平台）、REA 逆向资产（行家来了/DSH/小南斗）、项目 ledger/sessions/WAL、v0.3.1 交付物。
> 问题：**这四个方面怎么完善，才让"想清楚"变成可复用、可机检、可复盘的能力，而不是每次靠人脑？**
> 结论：先补"卡 + 门 + 回填"三件（B 方案），再补 UI 四台（C 方案）；不先加员工/班组。

## 0. 六件套（军师固定输出）

### 【选项地图】

| 选项 | 一句话 | 证据 | 成本 | 风险 | 可逆性 |
|---|---|---|---|---|---|
| A 方法论补丁 | 只改 SKILL.md/模板，加四段判据 | 本会话诊断；REA 方法报告 E1–E6 | 0.5 天 | 口号化，无载体 | 双向 |
| **B 卡+门+机检** | 四张 schema + 状态机 + validator + GitHub CI | 本包已落 `team/loop/`；validator 可扩 | 2–3 天 | 字段设计过度，真实任务不填 | 双向 |
| C 四台 UI | 检索台/分析台/决策台/回柜台 | v0.3.1 UI 已有舞台/交付卡；Muse 的 Goals/Activity 可借鉴 | 1–2 周 | 先做 UI 后补数据，变空壳 | 双向 |
| D 全平台 | 多用户/接入/计费/IM | 竞品（瞬知/agnt8x/GrokBot）证明是平台活 | 数月 | 偏离首客户，烧钱 | 部分单向 |

### 【权衡】
- 不加载体（A）＝ 又一次"只写判据＝口号"，违背公理 1；
- 先做 UI（C）＝ 没有真实证据/决策记录，四台就是四个空页面；
- 全平台（D）＝ 首客户还没签字，过早平台化。
- **B 是唯一"今天能落地、明天真跑能验证"的层**。

### 【推荐】B → C，置信 MED-HIGH
本周落 B：四张卡 + 状态机 + validator + GitHub CI；明天真实任务按四段走一遍；拿到真实回填后再决定 C 的四台怎么排。

### 【承重追问】
**JuJing 是公开仓库（已确认）——bot-team 的产品代码/真实任务数据要不要进这个公开库？** 这决定 GitHub 布局与脱敏边界；在用户回答前，默认只进"可公开的代码/文档/demo"，不进真实数据与个人记忆。

### 【假设账本】
1. 四段闭环先在单机跑通，不依赖外部定时服务；
2. 真实任务数据可由用户脱敏后放进私有目录；
3. GitHub 仓库用于版本管理，不用于自动部署；
4. 竞品评分是内部基准，不构成市场结论。

### 【成本行】
本轮检索 2 次 ≈ **¥0.010**（决策框架/agent 闭环各 1 次）；本地读取/文档 ¥0；未打包。

---

## 1. 现状诊断（1–5 分）

| 段 | 现状 | 分 | 证据/断点 |
|---|---|---|---|
| 检索 | ds_search 可用；有 score/URL；但结果只进报告，未成"证据卡" | 3 | 本会话 7 次检索；benchmark 报告有 URL/score，但无 quote/grade/freshness 字段 |
| 分析 | 有选项地图/评分/假设；但无判据权重、敏感性、pre-mortem | 3 | `PRODUCT_BENCHMARK` 有八维评分，但权重未显式、无"推翻条件" |
| 决策 | ledger 有 D0–D4；但无结构化决策记录（审批/推翻条件/review_due 不齐） | 2.5 | `.junshi/ledger.md` 行内信息全，但机检无法逐字段验 |
| 闭环 | sessions `review_due` 已被相位 0 消费；但无 actual_result 回填、无 hit/miss、无棘轮 | 2 | 多条 `review_due=2026-10-25` 尚未到期；无 closure 记录 |

**总判断：检索/分析"能用"，决策/闭环"靠自觉"。** 这正是"demo 眼前一亮、产品还不成立"在方法层的同构。

## 2. 目标设计（四段各一张卡）

已在 `team/loop/` 落地：

| 卡 | 文件 | 作用 | 机检点 |
|---|---|---|---|
| 证据卡 | `evidence-card.schema.yaml` | 把检索结果变成可引用证据（quote/grade/freshness） | URL/quote≤200/score≥1.0/grade |
| 分析卡 | `analysis-card.schema.yaml` | 选项≥2、判据权重、敏感性、反面证据、pre-mortem | 权重和≈1、disconfirming≥1、premortem≥3 |
| 决策记录 | `decision-record.schema.yaml` | 谁拍的板、放弃什么、推翻条件、review_due | approval 三字段、reversal≥1、review_due |
| 闭环记录 | `closure-record.schema.yaml` | 实际结果、hit/miss、教训、棘轮动作 | verdict 必填、miss 必须写 action |

状态机：`draft → researched(G1) → analyzed(G2) → decided(G3) → delivered → closed(G4)`；`rejected/superseded` 也是一等状态。

## 3. 四段怎么"完善"（逐段动作）

### 检索：从"搜到"到"可用证据"
1. 每条承重结论必须挂证据卡 ID；正文不复制大段原文，只放 quote。
2. 证据分级 A/B/C；C 级不能单独承重（对应公理 0 的三查）。
3. 时效：市场 30 天、技术 90 天、财务/合规 7 天；过期证据自动降级。
4. 检索预算：每条 query 先过"值不值 5 厘钱"；同 query 缓存。
5. 机检：`evidence.jsonl` 缺 URL/quote/score 即 FAIL。

### 分析：从"拍脑袋对比"到"可复算"
1. 选项 ≥2，必含"不做"；每项写成本/风险/可逆性。
2. 判据权重显式（和≈1），加权分只排序不替代判断。
3. 敏感性：改权重最大的 1–2 个判据，结论翻转就写条件结论。
4. 反面证据 + pre-mortem 3 条；"空白判断"必须带置信度。
5. 机检：无权重/无反面对比即 FAIL。

### 决策：从"账本一行"到"可追溯记录"
1. 一条记录一个决定；否决也是结果（写 rejected）。
2. 必带：证据/分析 ID、chosen、confidence、reversibility、approval(by/at/mode)。
3. 必带：推翻条件 + review_due；单向门必须 explicit 审批。
4. append-only；错账用 `supersedes` 新行。
5. 机检：缺 approval 或 reversal 即 FAIL。

### 闭环：从"写了回看"到"真的回填"
1. 到期扫描：`review_due` 到期先呈现"实际结果如何"；不回填不继续新决策。
2. 回填 actual_outcome / metric / verdict（hit/miss/partial）。
3. 棘轮：hit → 固化默认值；miss → 升检查项；连续 3 miss → 退役。
4. 指标：命中率、证据覆盖率、决策周期、推翻率、单决策成本。
5. 机检：miss 缺 action 即 FAIL。

## 4. GitHub 仓库接法（不打包，不执行 git）

已确认 `https://github.com/pipishou/JuJing` 是**公开仓库**（MIT，现有两个 skill 目录）。建议：

```text
JuJing/
├─ jujing-agent-workflow/     # 现有，不动
├─ jujing-junshi/            # 现有，不动
├─ bot-team/                 # 新增：产品包（本机 bot-team-dsh，去掉 dist/runs/task/个人数据）
├─ docs/                     # benchmark / playbook / four-loop 方案
├─ .github/workflows/validate.yml  # 结构校验 + 密钥扫描
├─ .gitignore
└─ README.md                 # 产品一句话 + 安装/验收
```

**公开库禁入**：`.credentials*`、`.env*`、`.junshi/`、`skills/*/data/`、`runs/task-*/`（真实工厂数据）、`dist/`、REA 逆向资产、Octop 源树、个人会话日志。
**可进**：代码、schema、demo 数据（脱敏）、文档、测试、CI。
详细见 `GITHUB_REPO_PLAN_2026-10-18_zh.md`。

## 5. 30 天路线（建议）

| 周 | 动作 | 出口判据 |
|---|---|---|
| W1（本周） | 四卡 + 状态机 + validator + CI；明天真实任务按四段走 | validator 绿；一次真实任务产出 4 张卡 |
| W2 | 接单向导→自动生成四卡模板；交付卡挂四卡链接 | 新任务 10 分钟内起卡 |
| W3 | 四台 UI（检索/分析/决策/回柜）最小版 | 用户可在 UI 里看完一条决策链 |
| W4 | 命中率/证据覆盖率报表；棘轮第一次调整 | 至少 1 条 hit + 1 条 miss 回填 |

## 6. 不要做

- 不要先做四台 UI 再补数据（空壳）；
- 不要把"拍板"自动化（军师不替用户决策）；
- 不要用功能数/员工数当进度（虚荣指标）；
- 不要为了"看起来完整"补假证据/假回填（账本会烂）。

---

**一句话**：四段闭环的完善，不是加四个页面，而是加**四张卡 + 四道门 + 一次真实回填**；先把 B 落地，明天用真实任务验证，再决定 C 的四台怎么排。
