# 人格源 · 内容操盘手（manufacturing-content-operator-perspective）

> 本目录是**人格层的唯一事实源**。agents/content-operator.md 里的 PERSONA 块由
> `JuJing/bot-team/tools/rebuild_persona.py` 从 `distill/SKILL.md` 生成，**禁止手改 agent 文件里的 PERSONA 块**。

## 源

| 项 | 值 |
|---|---|
| 人格类型 | **主题蒸馏**（不挂真人名，规避人格权/名誉风险） |
| 蒸馏产物 | `distill/SKILL.md`（6 心智模型 / 8 决策启发式 / 表达 DNA / 平台事实底稿 / 反例黑名单） |
| 蒸馏工具 | 女娲 nuwa-skill（`~/.workbuddy/skills/huashu-nuwa`，上游 `alchaincyf/nuwa-skill` @ `7c7edfeb` 2026-10-24） |
| 蒸馏截止 | 2026-10-28 |
| 挂载方式 | ROLES 表 `@pack:persona/manufacturing-content-operator/distill` |
| 为什么不挂真人 | 第五人需要一个能长期存在的角色而不是某位博主的复刻；挂真人会带来名誉与授权风险（方案 §2.3） |

## 底子（三类来源拼装，不冒充单一人物）

1. **平台机制**：视频号/抖音官方手册与官方口径（高置信）。
2. **结构研究**：大样本标题 RCT、留存曲线结构手册（高/中置信，英文语境为主）。
3. **行业案例**：制造业 B2B 出镜与内容复盘案例（中置信，单案例为主）。

## 增量采集

新素材不直接改 `distill/SKILL.md`，**先落 inbox，标注可信度**，累积到 5 条或平台规则变化时重蒸：

```
inbox/YYYY-MM-DD-<主题>.md
---
可信度: 高|中|低
来源: <链接或说明>
采集日: YYYY-MM-DD
---
<要点，必须带数字与时间>
```

重蒸后把 inbox 文件移到 `ingested/` 并在本文件「重蒸记录」追加一行。

## 重蒸记录

| 日期 | 触发 | 变更 |
|---|---|---|
| 2026-10-28 | 首次蒸馏（第五人立项） | 6 模型 / 8 启发式 / 15 条平台事实底稿 / 指标树 |
