# 待发版池（<产品名>）—— PENDING_RELEASES.md

> 实例池：**产品仓库常驻文件**，跨批次滚动（实例化自 skill 的 `assets/pending-releases.template.md`；判据唯一事实源＝skill `assets/params.json#release_pool`）。
> 定位：采纳了但没发版的条目在此显式攒批——「发现问题」与「发版解耦」，定期有计划地打包发版，不是发现一个发一个。
> 纪律：条目只增不改写历史（状态流转＝改状态列＋日期列，同 append-only 精神）；机检＝skill 侧 `python scripts/probe_usage.py pool-check --pool <本文件路径>`。

## 触发判据（引用键名，数值以 params.json#release_pool 为准）

1. 池内 `adopted` 态 ≥ `adopt_trigger_n` 条 → 打包走扩展四问＋棘轮发版；
2. 命中 `blocker_definition`（数据丢失/安全/机检失效）→ 不等攒批，单条立即发版；
3. 距上一批发版 ≥ `stale_days` 天且池内存在 `adopted` → 打包发版。

## 条目表（列结构固定，pool-check 按此机检；状态值域＝params.json#release_pool.statuses）

| ID | 来源 | 问题点 | 拟更新内容 | 状态 | 日期 | 批次回执 |
|---|---|---|---|---|---|---|
| PR-001 | D6/DECISIONS#卡 | （一句话） | （拟改文件/条款/代码，可外链） | proposed | 2026-09-28 | — |
| PR-002 | 第 5 次独立复核 S3 | 棘轮不按 rubric 口径过滤（`pre-r1` 与 `2026-10-29-r1` 直接比大小） | `scripts/rubric_score.py` 增口径过滤＋历史行回填脚本 | proposed | 2026-11-10 | — |

## 状态语义

- `proposed`：发现待评估（G2.5 二次评估前）
- `adopted`：评估通过待发版（攒批对象）
- `observing`：挂观察（复发→adopted；到期不复发→expired；窗口＝observe_window_rounds 轮）
- `rejected`：评估否决（留痕不删，理由写在「批次回执」列）
- `shipped`：已随某批次发版（批次回执列填版本号＋日期）
- `expired`：观察超窗/语境失效（留痕不删）

## 发版回执协议

批次发版（扩展四问＋棘轮过门）完成后：本池所有 `shipped` 条目回执列填 `<版本号> <日期>`；同时 skill 侧 `references/evolution.md` 登记批次（双回流：池行＋登记两件在位）。

| P2-1 | 演进登记表迁 `data/`＋引用改指＋索引生成（`references/evolution.md` 体量超线根因） | 待批 | 2026-11-10 三次复核 suggest；需改 19 处 `evolution.md#N` 引用 |

## PR-003 · 判据三件同批准入规则（proposed 2026-11-10）

**提案**：任何新锚点进 `assets/params.json#clauses` 必须同批交齐三件——①载体（阶段件/判据/模板里的可执行句）②机检项（`probe_usage.py` 的 size 或 selfcheck 断言）③**独立构造的负样本**（把载体或断言改坏必须转红）。缺任一件不得入池。

**动机**：第 3–5 次独立复核的 blocking 全部落在「判据、载体、机检三件不同深」与「断言自证」；B 维（机检可信度）三轮分别为 10/30、24/30、19/30。

**成本**：每条新判据 +1 负样本构造（约 15–30 行测试代码）。

**状态**：待批。批前新判据仍按现流程，但在 `references/evolution.md` 登记行须显式写明「负样本：有/无」。

## PR-004 · 复核节奏与版本位解耦（proposed 2026-11-10）

**提案**：①独立复核不再每批触发，改为每 3 个 patch 位（或每次 minor 位）一次；②复核必须固定同一评审员、同一 rubric 口径，分数只在同口径内比较；③版本位规则维持现状（LOCKED 下 patch＋1），但**复核通过不是升 minor 的必要条件**——避免为凑分连续打 patch。

**动机**：同一版本不同评审员差 1.5–2 分（v2.3.5：6.44 vs 8.21；v2.3.4：7.39 vs 7.76），逐批复核＝噪声驱动开发。

**状态**：待批。
