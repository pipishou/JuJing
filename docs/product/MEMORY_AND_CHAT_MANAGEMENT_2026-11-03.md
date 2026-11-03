# Bot 的记忆与群聊怎么运行、怎么管（2026-11-03 实测）

> 你在群里布置了一个任务，我把前后的数字都取了一遍。**结论先说：群聊在跑，记忆是空的，提醒和派活都没发生。**

## 一、实测：一个任务在系统里留下了什么

| 指标 | 布置前（17:52） | 现在（08:33） | 说明 |
| --- | --- | --- | --- |
| `usage_receipts` | 6 | **208** | 模型被调用 202 次——群消息确实驱动了 Bot |
| `inbox_admissions` | 4 | **86** | 每条群消息给每个 Bot 生成一条「准入记录」 |
| `channel_records` | 7 | **8** | 多了一个频道 |
| `assignments` | 0 | **0** | **没有发生任何派活** |
| `bot_schedules` | 0 | **0** | **没有排任何定时提醒** |
| `messaging_outbox` | 0 | 0 | 没有程序化发消息 |
| `messaging_reception_history` | 0 | 0 | **消息正文不落这张表** |

频道实测：
- `group-69bebbeb…` = **工作助理团队**，成员 5 个 Bot
- `group-mo` = **Mo工作助理团队**
- 另有 6 个 DM 频道（每个 Bot 一个）

准入记录形状（原样摘一行）：`reason=group-ordinary`、`attempt_state=pending`、`source_event_id` 同一条消息 → 5 个 Bot 各一行。**pending 不等于已回复**，它只表示"被排上了"。

## 二、记忆：现在是零沉淀（这是真问题）

`bot-memory/assistant/bot-5f46…/` 里实际只有三个东西：

| 文件 | 大小 | 是什么 |
| --- | --- | --- |
| `SOUL.md` | 7,226 B | 人格（我蒸馏的稿子，昨天 17:38 改过） |
| `MEMORY.md` | **67 B** | 内容就是一行**记忆目录路径**——不是记忆本身 |
| `.botharness/bot.json` | 949 B | 头像与显示名 |

**`log/` 和 `notes/` 目录都不存在** → 说明 Tobs 的「核心记忆」只是个**指针**，真正的记忆要靠 Bot 自己往目录里写文件，而它一条都没写。

> 也就是说：你以为它在"记"，其实它只是在"聊"。聊完什么都没留下。

## 三、管理方案：三层记忆 + 一个同步面

| 层 | 位置 | 谁写 | 同步 |
| --- | --- | --- | --- |
| **L1 人格** | `bot-memory/<角色>/<bot>/SOUL.md` | 我（蒸馏）+ 你审 | git（改动可回滚） |
| **L2 工作记忆** | 同目录 `log/YYYY-MM.md` + `notes/` | **Bot 自己**（每次会话结束写 3 行） | git（append-only，几乎不冲突） |
| **L3 证据** | `bot-team/runs/`、`.junshi/`、决策卡 | 我方服务 | **不 git**（私有、含真实业务） |
| **群聊事件** | sqlite（事实源）→ `bot-memory/_chat/YYYY-MM-DD.*.jsonl` | `tools/tobs_chat_export.py` | git（每天一个文件，可 diff） |

**跨设备规则（你问的重点）**：

1. **只有一个同步面：`bot-memory` 仓**。人格、工作记忆、群聊导出都在里面；拉下来就能接着干。
2. **sqlite 永不进 git**。二进制 + 每机一份 + 合并必冲突。事实源留本机，导出物进仓。
3. **索引不同步**。`tools/context_index.py` 的 40,958 块是本机派生物，换机器重建（63s），不进仓。
4. **冲突靠设计避免，不靠 merge**：日志文件按「月」切、导出按「日」切，两个设备同一时刻写同一天的概率极低；真撞了 `pull --rebase` 手工并。
5. **凭据不同步**：`.env.local`、`im_signers.json`、`*.key` 一律 gitignore（已配）。

**已落地的工具**：

```bash
# 每天 21:00 导出当天群聊事件（可挂计划任务）
python -B tools/tobs_chat_export.py --out D:/work/产品类/个人产品/bot-2/bot-memory/_chat
# 今天实测：channel_records 8 / inbox_admissions 86 / outbox 0 / reception 0
```

## 四、三条硬纪律（不执行，记忆永远是 0）

1. **每个 Bot 每次会话结束写 3 行**：`日期 / 你说了什么 / 我做了什么决定`。写进 `log/YYYY-MM.md`。
2. **每天 21:00 导出群聊事件**，进 `bot-memory/_chat/`，提交推送。
3. **每周把 `log/` 压成 10 行进 `MEMORY.md`**：只压缩、不删原文件（append-only）。

> 第 1 条必须写进 SOUL，否则靠自觉＝不会发生。这不是"以后再说"，是"今天不写，明天它还是零"。

## 五、建议做与待你拍板

| 项 | 我做 | 门 |
| --- | --- | --- |
| 给 5 个 Bot 的 SOUL 追加「记忆纪律 3 行」 | 我改，可随时撤 | 双向 |
| 建每日导出计划任务（21:00） | 我建 | 双向 |
| 修复「派活」 | 你在 Tobs 面板配 `assignment` 模型 | 你 |
| 主动提醒 | 先试 Bot 原生定时（`bot_schedules` 0→1） | 双向 |

## 六、诚实边界

- **消息正文不在 `messaging_reception_history`**（0 行）。我现在能证明"消息驱动了 Bot"（usage 208、admissions 86），**不能证明"我读到了你发的那句话"**——正文可能只在 DSH 会话日志里，未验证。
- `attempt_state=pending` 的含义是"待处理"，不是"已回复"。
- 208 次模型调用里有多少是有效对话、多少是重试/串场，**没查**；只把它当"通道活了"的证据，不当质量证据。
- `bot_schedules=0` 说明：**到今天为止，没有任何一条主动提醒被排上**。
