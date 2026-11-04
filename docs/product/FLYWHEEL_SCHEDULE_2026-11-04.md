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
