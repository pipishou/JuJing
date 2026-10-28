# 人格源 · 乔布斯（steve-jobs-perspective）

> 本目录是**人格层的唯一事实源**。agents/jobs.md 里的 PERSONA 块由
> `bot-team/tools/rebuild_persona.py` 从这里生成，**禁止手改 agent 文件里的 PERSONA 块**。

## 源

| 项 | 值 |
|---|---|
| 女娲包路径 | `C:\Users\kf-41\Desktop\个人内容\skill\nuwa-skill-main\nuwa-skill-main\examples\steve-jobs-perspective` |
| 现用底稿 | `@pack:persona/steve-jobs/distill/SKILL.md`（包内重蒸稿；女娲再更新不覆盖） |
| 蒸馏产物 | `SKILL.md`（6 心智模型 / 8 决策启发式 / **后见之明校验层** / 表达 DNA / 失败模式 Fallback 树） |
| 蒸馏截止 | 2026-10-28（重蒸：补 2012–2026 后见之明 + 3 本新传记/档案） |
| 本仓库最后校验 | 2026-10-28 |
| 环境变量覆盖 | `NUWA_ROOT`（默认即上表路径）、`ADVISORY_PACK` |

## 增量采集

```
persona/steve-jobs/inbox/YYYY-MM-DD-<slug>.md
```

乔布斯已故，增量价值低于卡帕西——**只有出现新的传记/档案/录音/关键他人回忆**才收。

消化后移到 `ingested/`，并在下表追加。

| 已消化日期 | 条目 | 影响 |
|---|---|---|
| 2026-10-28 | 2026 重蒸批：后见之明（Apple Silicon 2020 / After Steve 2022 / Make Something Wonderful 2023 / Vision Pro 2023-24 / Apple Intelligence + DMA 2024） | 新增「后见之明校验」层；模型仍 6 条，但每条附 15 年后判定；如实标注联网检索不可用（API 402），条目为公开常识级 |

## 更新三步

1. **收**（新料进 `inbox/`） → 2. **蒸**（改 `distill/SKILL.md`，或改 `VOICE.md` 调风格；女娲源只作底稿起点） → 3. **刷**（`rebuild_persona.py --apply` 后 `--check` 退出 0） → 4. **验**（`tests/test_advisory_pack.py`）。

## 边界

- 2011 年 10 月 5 日之后的任何产品/事件：**不得以第一人称给出立场**，只能承认「我 2011 年就不在了，对 X 没有公开表态」。
- 不引用名言充当论据；每条引用必须挂本场景的具体细节，否则删掉。
- 对没亲眼见过的产品：反问要细节，不许假装用过。
