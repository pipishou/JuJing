# 飞轮节奏：每天 11:00、17:00 各一次全链路（2026-11-04）

> 依据 user m10425：**bot 团队不是实时运转的，不需要高频采集**——改为 11 点、17 点各一次全链路执行。

## 1 节奏

| 维度 | 改前 | 改后 |
|---|---|---|
| 频率 | 每 900 秒（15 分钟）一轮 | **每天 11:00、17:00 各一轮** |
| 触发 | 固定间隔 | 时间槽（`core/timeslots.py`，两处共用一份口径） |
| 全链路 | 采集→候选→周报→WAL→记忆 | 不变（同一 `tick()`） |

## 2 三种跑法

```bash
# ① 服务常驻（推荐）：随 8787 启动，睡到 11:00 / 17:00 各跑一次
#    环境变量 BOT_FLYWHEEL_AT="11:00,17:00"（默认值即此），BOT_FLYWHEEL_DISABLED=1 关闭

# ② 前台常驻（不重启服务也能用）
python -B tools/flywheel_tick.py --at 11:00,17:00 --loop

# ③ 计划任务（每 10 分钟喊一次，只有命中窗口才真跑，跑过即跳过）
python -B tools/flywheel_tick.py --due
```

`--due` 的幂等状态在 `runtime/flywheel_tick.json`（键 `last_slot`，如 `2026-11-04@11:00`），
同一天同一槽不会重复跑；宽限窗口默认 20 分钟（`--grace`）。

## 3 实测（2026-11-04T07:26Z）

```
[OK ] flywheel tick  (2.83s)
  ok collect      2048ms  {"emitted": 122, "skipped": 7, "errors": []}
  ok candidates    151ms  {"built": 2}
  ok weekly        121ms  {"events_in_window": 1321, "issued": 4, "signed": 0, "outcomes": 0, "wal_signals": 1}
  ok wal            66ms  {"candidates": []}
  ok memory        444ms  {"mirrored": 0}
```

- 非窗口时 `--due` 直接返回 `{"ok": true, "skipped": true, "reason": "not_in_window", "slots": ["11:00","17:00"]}`；
- 窗口内第二次调用返回 `{"skipped": true, "reason": "already_ran", "slot": "..."}`；
- `tests/test_timeslots.py` **30 项 PASS**（解析/去重/排序/越界报错/env 回落/跨日跨月/窗口边界/幂等键）。

## 4 为什么这样设计

- **不是定时器驱动业务**：11/17 两轮是「对齐工作节奏的检查点」，不是实时流水线；
- **口径单一来源**：服务的后台线程与命令行 `--loop` 都用 `core/timeslots.py` 算下一次几点，避免两处各写一遍；
- **失败不阻断**：单轮里任一步失败只记 `[flywheel]` 日志与 tick 事件，服务不受影响。

## 5 已经装好的计划任务（无需管理员）

```bat
schtasks /Create /TN "JujingFlywheelDue" ^
  /TR "cmd /c \"D:\work\产品类\个人产品\bot-2\JuJing\bot-team\server\flywheel_due.cmd\"" ^
  /SC MINUTE /MO 10 /F /RL LIMITED
```

- 每 **10 分钟**喊一次 `server/flywheel_due.cmd`；包装器调 `tools/flywheel_tick.py --due`
- **只有落在 11:00 / 17:00 后 20 分钟内才真跑**，其余时间一行 `not_in_window` 就退出
- 同一槽跑过 → `already_ran`（状态在 `runtime/flywheel_tick.json`）
- 日志：`runtime/logs/flywheel-due.log`
- 用户级任务（`/RL LIMITED`），**不需要管理员**；删：`schtasks /Delete /TN JujingFlywheelDue /F`

实测：`schtasks /Run /TN JujingFlywheelDue` → 日志写入 `{"ok": true, "skipped": true, "reason": "not_in_window", "slots": ["11:00","17:00"]}`，退出码 0。

## 6 为什么不挂到 DSH 的定时器

DSH 的 `schedule_create` 是**会话级**提醒：投递对象是当前会话，会话关掉就不再触发，也不能保证宿主空闲时执行。
飞轮要的是「机器开着就跑一次」，所以落在 **Windows 计划任务**上更稳；DSH 常开只是便利，不是可靠时钟。

## 失败告警（2026-11-04 起）

计划任务跑在无控制台的会话里，失败过去只落 `runtime/logs/flywheel-due.log`，靠人翻日志才发现（17:01 那次 GBK 崩溃就是这样）。现在改成**失败主动发飞书**。

| 项 | 值 |
|---|---|
| 触发 | tick 结果 `ok:false`（任一步失败） |
| 通道 | `D:/work/产品类/个人产品/bot-2/tools/lark_send.py`（`--purpose mo_p2p --auth tenant`，与签字通知同一条已验证链路，实测 1.6s） |
| 内容 | 槽位＋第几次/共几次、逐条失败步与错误文本（截 240 字）、通过步数、耗时、处理建议 |
| 去重 | `--due` 同槽最多 3 次尝试，每次失败各发一条（`第 N/3 次`），成功即清计数 |
| 手工跑 | `python -B tools/flywheel_tick.py` 失败也发（不带次数） |
| 关掉 | `BOT_FLYWHEEL_NOTIFY=0` |
| 换脚本 | `BOT_LARK_SEND=<其他发送脚本>`（不存在时静默跳过，不阻断 tick） |
| 失败不阻断 | 告警自身任何异常都被吞掉（`sent:false` + `reason`），绝不让告警把 tick 弄成二次故障 |

自检：`python -B tests/test_flywheel_notify.py`（18 项离线断言，用假脚本验证参数构造、开关、文案与失败兜底）。

实发验证（2026-11-04 17:12）：`{"sent": true, "rc": 0, ... "code": 0, "chat_id": "oc_559ba779af9fa9e32c56f4aacc71bb96"}` —— 已在你飞书收到一条「飞轮失败 演练」测试消息，可忽略。
