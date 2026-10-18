# 迁移说明（本机 → 工作电脑）

> 目标：明天到工作电脑后，**10 分钟内**把 bot 团队结构跑起来并验证绿。
> 本包只含团队结构/资产/演示/测试，不含 Octop 运行时与 254MB 旧迁移包。

## 0. 带什么

把这个目录整体带走即可：`D:\项目\bot\bot-team-dsh\`（约几 MB）。
可选：`D:\项目\skill\jujing-junshi.zip`、`jujing-agent-workflow.zip`（聚鲸双 skill，个人工作流，按需装）。

## 1. 工作电脑环境

1. Windows + Python 3.12+（需要 `pyyaml`：`python -m pip install pyyaml`）。
2. DeepSeek Harness（桌面/CLI 任一）；本包已按 DSH skill 规范写 `SKILL.md`。
3. 可选：Octop（只有要跑旧版 runtime 时才需要，本包不依赖）。

## 2. 安装 skill（两条路，二选一）

**路 A（推荐）**：复制到用户 skill 根：

```powershell
$dst = "$env:USERPROFILE\.dsh\skills\jujing-bot-team"
Copy-Item 'D:\path\to\bot-team-dsh' $dst -Recurse -Force
# DSH 自动发现（热加载），新会话即可用 /jujing-bot-team
```

**路 B（不安装）**：直接把 `orchestration/team-workflow.mjs` 的 `base` 指向包路径跑 `workflow` 工具；适合只验收不常驻。

## 3. 验收清单（必须全绿才算迁移成功）

> 本机（2026-10-18）已跑绿一次，回执见 `runs/TEST-RECEIPT-2026-10-18.md`；迁移后按同一清单复跑。

```powershell
cd <包根>
python tests\validate_structure.py          # 期望 exit 0
# DSH 会话内：用 workflow 工具跑 orchestration/team-workflow.mjs（args: {"base":"<包根>"}）
python tests\validate_report.py runs\demo-report.json   # 期望 exit 0
```

| # | 检查 | 判据 |
|---|---|---|
| 1 | 结构完整 | `validate_structure.py` exit 0 |
| 2 | demo runner 可跑 | workflow 返回 s1–s4 四段 JSON，无空段 |
| 3 | 报告合规 | `validate_report.py` exit 0（finding/ cannot_attribute/ scorecard/ roi/ 决策日志） |
| 4 | skill 可见 | 新会话 `/` 菜单或 `<available_skills>` 出现 `jujing-bot-team` |
| 5 | 迁移留痕 | 新机把 `runs/` 与 `docs/ledger.md` 带上，继续追加 |

## 4. 到工作电脑后的第一批动作（按顺序）

1. 跑 §3 验收；
2. 按 `docs/BOT_PILOT_SPEC_2026-10-18_zh.md` §2 做**数据合同审计**（真实塑胶厂 T/T-30 + 因素字段；demo 数据只用于结构验收）；
3. 数据审计绿后，把 `demo/` 路径换成真实数据路径，按 G0 预派发确认跑一次真归因；
4. 结果回填 `docs/ledger.md`（append-only）。

## 5. 不要带/不要做的

- 不要带：`.conda/`、`Octop-source*/`、`bot-migration-2026-10-16.zip`、`node_modules`、缓存目录、任何密钥。
- 不要做：把 demo 结果当真实归因对外；未经用户许可执行 git；把 `.env.local` / key 写进包。

## 6. 已知限制（如实）

- DSH Agent Teams 为实验特性（≤8 人、共享 cwd、无跨进程 exactly-once）；模式 B 只用于演示/轻协作。
- 本包校验的是**结构与 schema**，不是归因正确率。
- 归因依赖 LLM 判断，存在漂移；M1 试点须以真实数据与人工复核为准。
