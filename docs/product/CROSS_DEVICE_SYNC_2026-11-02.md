# 跨电脑上下文管理与信息同步（方案）

> 日期：2026-11-02 ｜ 状态：方案（未实施）｜ 关联：D-051

## 一、先分清三类资产

| 类别 | 内容 | 能不能同步 | 说明 |
|---|---|---|---|
| A 派生缓存 | `runtime/context/index.jsonl`（70MB）、`runtime/workitems` 的统计投影 | **不同步** | 每台机器本地重建，10 分钟内可重建（487 文件 / 6.6s 抽取 + 建索引） |
| B 事实源 | `runtime/workitems/current.json`（事项账本）、`runtime/events.jsonl`（事件流）、`runs/<task>/`（report/signature/outcome/loop） | **必须同步** | 这才是"跨电脑的上下文"——不在一台机器上就丢 |
| C 代码与人格 | `packs/`、`team/`、`agents/`、`docs/`、`core/tools/server/ui` | **已在 git** | 走 `origin/bot-team` 分支 |

关键判断：**跨电脑同步的不是"上下文文件"，是"判断记录"**。索引可以重建，签过的卡不能重建。

## 二、三个方案

### 甲（推荐其一）多机 git 同步
把 B 类放进一个**独立私有仓** `bot-team-memory`（与代码仓分开，避免每次签卡都触发 CI）：

```
bot-team-memory/
├── workitems/current.json      # 事项账本（revision 乐观锁）
├── events/events.jsonl         # 全部 bot_team_* 事件（append-only）
└── runs/<task_id>/{report.json, signature.json, outcome.json, loop/}
```

- 事件流是 append-only JSONL：**天然无冲突**，合并取并集 + 按 `event_id` 去重。
- 账本是单 JSON：带 `revision`，合并取高 revision；低的那台把差异写 `workitems/conflicts/<ts>.json` 留证，不静默丢。
- Tobs 的 Bot 记忆本来就是 git 仓库 → 同一套机制，两仓可同机同步。

### 乙（推荐其二）单主 + 客户端
一台机器当**主机**（就是每天开 ≥8h 那台），其他机器只当客户端（浏览器开 `127.0.0.1:8787` 或走飞书）。
- 优点：零冲突，状态只有一个真源。
- 代价：主机不在线，别的机器就只能看不能写。
- 落地：客户端把新产生的文件/笔记丢进主机的 `inbox/`（飞书发文件给 Bot 也算），由主机索引。

### 丙 云单点
阿里云轻量 2C4G 跑同一套服务，任何机器都只是浏览器。等价于乙 + 云主机。
- 判据：**只在"每天开机 ≥8h"不成立时才上云**，否则先跑甲/乙。

## 三、推荐组合

**先甲（多机 git，0 元）→ 满足不了再丙（云单点）。**
乙作为甲失败时的兜底（多主冲突不可控时退回单主）。

理由：甲不需要新硬件、不需要备案、不需要常开；丙的钱花在"主机常开"上，而这件事你现在这台电脑就在做。

## 四、落地三件（甲方案，约 0.5 人日）

1. `tools/sync_memory.py`
   - `--init` 建 `bot-team-memory` 仓并写 `.gitignore`（只放 B 类）
   - `--push`：先把 B 类从 `runtime/`、`runs/` 复制进记忆仓 → `git pull --rebase` → 冲突按上面规则合并 → `commit` → `push`
   - `--pull`：反向写回（写回前比对 revision，高的赢）
   - `--status`：两侧 revision / 事件条数 / 最近签字时间
2. 每台机器两条计划任务：**开机 pull 一次**；**每小时 push 一次**（事件流追加，失败不阻塞）
3. `runtime/` 的 `.gitignore` 改为白名单：只放行 `workitems/current.json`、`events/events.jsonl`，索引与外设状态继续忽略

## 五、必须说清的边界

- **索引不同步**：换机器后第一件事是重建索引（`python -B tools/context_index.py`），不是等同步。
- **并发写同一任务会冲突**：两台机器同时改同一个 `WI-xxxx` 或同时签同一张卡，靠 revision 只能"事后发现"，不能"事前阻止"。要么单主，要么加锁（已实现 `core/filelock.py` 只在本机生效）。
- **飞书里的东西不等于在这里**：飞书文档/聊天记录目前只有拉取的部分进索引；没拉下来的，跨机器也看不到。
- **凭据不同步**：`.env.local`、`server/im_signers.json`、`octop.db` 里的 IM 凭据不进 git。新机器要单独配。

## 六、验收（跨电脑同步做到没有）

| 编号 | 判据 | 验法 |
|---|---|---|
| S1 | A 机签的卡，B 机打开能看到同一份 `report.json` 与 `signature_sha256` | 两机 `sha256sum` 一致 |
| S2 | A 机改的事项状态，B 机 pull 后 revision 递增且内容一致 | `--status` 两侧同 revision |
| S3 | 两机同时改同一事项，冲突被记录而非静默覆盖 | `workitems/conflicts/` 有文件 |
| S4 | 换机器后 10 分钟内能重建索引并命中同一批问题 | `context_index` 重跑 + 抽查 4 问 |
