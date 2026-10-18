# jujing-skills · 聚鲸技能集

Agent Skills（SKILL.md 协议）格式的两个可复用技能：一套可自我演化的多角色 agent 开发工作流，加一个「念头 → 产品」的决策军师。

Two Agent-Skills-compatible skills: a self-evolving multi-role agent development workflow, and a decision advisor that turns rough ideas into shippable products.

| Skill | 版本 | 一句话 | 触发词 |
|---|---|---|---|
| [jujing-agent-workflow](jujing-agent-workflow/) | v2.3.3 | 多角色 agent 开发工作流：立项→需求→开发→测试→部署→版本→排障→反馈运营，内核为 AI＋数据飞轮自优化（WAL／棘轮／探针） | 聚鲸、工作流、立项、质量门、三绿、G2.5、飞轮、经验库 |
| [jujing-junshi](jujing-junshi/) | v0.3 | 念头到产品的思维决策层：取证检索→选项地图→权衡推荐→人拍板→决策落账 | 军师、帮我想想、选型、要不要做、方案对比、复盘 |

## 安装 / Install

兼容所有支持 Agent Skills 协议的 runtime（Claude Code、Trae、WorkBuddy/CodeBuddy、Codex、Cursor 等）。

Clone 本仓库，把想要的 skill 文件夹复制到对应 runtime 的 skills 目录：

```bash
git clone https://github.com/<your-org>/jujing-skills.git
cp -r jujing-skills/jujing-junshi ~/.claude/skills/
```

- Claude Code: `~/.claude/skills/`
- Trae: 工作区 `.agents/skills/` 或经插件市场「技能」页签上传
- WorkBuddy / CodeBuddy: 项目级 `.workbuddy/skills/` 或 `.codebuddy/skills/`

也可以直接下载对应 skill 文件夹的 zip，在支持「上传本地技能」的 runtime 里安装。

## 数据飞轮 / How the loop works

两个 skill 的迭代由一条外部反馈回路驱动：

1. **曝光**：市场渠道分发，装机量不设 KPI
2. **信号**：GitHub issue 模板格式化反馈 + Actions 定时采集 traffic 写入 `metrics/`
3. **决策**：issue 落入决策账本（ledger），达到可执行判据才改动 skill
4. **发布**：release notes 回流市场更新

**首转判据：≥1 条可执行外部反馈回灌进 skill 版本。**

## 隐私 / Privacy

两个 skill 均为纯本地文本（SKILL.md + markdown + 本地 python 脚本），**不采集、不上报任何使用数据，无遥测**。

Both skills are pure local text. No telemetry, no data collection.

## License

[MIT](LICENSE) © 2026 聚鲸智能 (Jujing)
