# 人格源 · 卡帕西（andrej-karpathy-perspective）

> 本目录是**人格层的唯一事实源**。agents/karpathy.md 里的 PERSONA 块由
> `JuJing/bot-team/tools/rebuild_persona.py` 从这里生成，**禁止手改 agent 文件里的 PERSONA 块**。

## 源

| 项 | 值 |
|---|---|
| 女娲上游 | `alchaincyf/nuwa-skill` @ `7c7edfeb`（2026-10-24，安装于 `~/.workbuddy/skills/huashu-nuwa`） |
| 蒸馏产物 | 包内 `distill/SKILL.md`（7 心智模型 / 8 决策启发式 / 表达 DNA / 反例黑名单）——跟包走，不被女娲更新覆盖 |
| 蒸馏截止 | 2026-10-28（增补 nanochat / 可验证性 / agentic engineering / autoresearch / Claws） |
| 本仓库最后校验 | 2026-10-28 |
| 环境变量覆盖 | `NUWA_ROOT`（默认即上表路径） |

## 增量采集（卡帕西活跃，需要持续收）

新内容不直接改进 agent 文件，**先落 inbox，再走女娲重蒸，最后 rebuild**：

```
persona/andrej-karpathy/inbox/YYYY-MM-DD-<slug>.md
```

每条至少写：`来源URL + 日期 + 一句话主张 + 是否与现有模型冲突`。

消化后把文件移到 `persona/andrej-karpathy/ingested/`，并在下表追加一行。

| 已消化日期 | 条目 | 影响 |
|---|---|---|
| （暂无） | — | — |

## 更新三步（唯一合法路径）

1. **收**：新素材落 `inbox/`。
2. **蒸**：用女娲对该人物重新蒸馏，覆盖 `SKILL.md`（或修改 `VOICE.md`）。
3. **刷**：`python tools/rebuild_persona.py --apply` → 只改 PERSONA 块，职责层与输出契约一字不动 → `python tools/rebuild_persona.py --check` 必须退出 0。

## 边界（来自女娲包的已知盲区，不得越界）

- 商业战略 / 市场营销 / 融资——直接说「这不在我深入思考的领域」。
- 政治 / 政策 / 地缘——同上。
- 蒸馏截止日之后的动态——以角色身份说「最近的我还没跟上」，不编。

## 重蒸记录

| 日期 | 触发 | 变更 |
|---|---|---|
| 2026-04-05 | 女娲首蒸 | 6 模型 / 8 启发式 |
| 2026-10-28 | 用户要求「先更新女娲，再蒸馏最新卡帕西」 | 新增模型七「可验证性（Verifiability）」；模型五补 2025-12 基准失效判断；时间线补 2025-10→2026-04 共 11 条；来源补一手 6 条/二手 2 条；反例黑名单 +2 条（vibe coding 当最终立场、伪造「march of nines」引语） |
