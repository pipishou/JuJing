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

## 状态语义

- `proposed`：发现待评估（G2.5 二次评估前）
- `adopted`：评估通过待发版（攒批对象）
- `observing`：挂观察（复发→adopted；到期不复发→expired；窗口＝observe_window_rounds 轮）
- `rejected`：评估否决（留痕不删，理由写在「批次回执」列）
- `shipped`：已随某批次发版（批次回执列填版本号＋日期）
- `expired`：观察超窗/语境失效（留痕不删）

## 发版回执协议

批次发版（扩展四问＋棘轮过门）完成后：本池所有 `shipped` 条目回执列填 `<版本号> <日期>`；同时 skill 侧 `references/evolution.md` 登记批次（双回流：池行＋登记两件在位）。
