# 飞轮自转：一次 tick 跑完五步（2026-11-04）

> 目的对齐 user m10375：**先把飞轮转起来，做 bot 团队的自优化**——签字先不纠结。

## 1 一条命令

```bash
python -B tools/flywheel_tick.py            # 跑一次，人读
python -B tools/flywheel_tick.py --json     # 机器读
python -B tools/flywheel_tick.py --loop 900 # 常驻，每 900 秒一轮
```

一次 tick = 五个既有工具顺序执行，**本文件不复制业务逻辑**：

| 步 | 工具 | 产出 |
|---|---|---|
| collect | `tools/tobs_collect.py --once` | 白名单事件进 `runtime/events.jsonl` |
| candidates | `tools/task_candidates.py --build` | `runtime/candidates/*.json`（一消息一候选） |
| weekly | `tools/flywheel_weekly.py --days N --write` | `runtime/reports/flywheel-<date>.json` |
| wal | `tools/flywheel_promote.py check` | 达阈值的晋升候选 |
| memory | `tools/memory_commit.py --snapshot` | 嵌套记忆仓 → 父仓 `_mirror/` 提交 |

## 2 服务侧常驻（可选）

`server/app.py` 新增 `start_flywheel(cfg)`：daemon 线程 `bot-flywheel`，默认 **900 秒**一轮。
- `BOT_FLYWHEEL_INTERVAL`（最小 60）
- `BOT_FLYWHEEL_DISABLED=1` 关闭
- 进程内调用 `flywheel_round(cfg)`，日志前缀 `[flywheel]`，异常只记日志不拖垮服务

**注意**：常驻 8787 跑的是服务启动时的代码，改完要重启服务才生效；不重启也能用 `--loop` 自己跑。

## 3 实测证据（本机，2026-11-04T07:17Z）

```
[OK ] flywheel tick  (2.19s)
  ok collect    1441ms  {"emitted": 122, "skipped": 8, "errors": []}
  ok candidates  122ms  {"built": 0}
  ok weekly      108ms  {"events_in_window": 946, "issued": 4, "signed": 0, "outcomes": 0, "wal_signals": 1}
  ok wal          59ms  {"candidates": []}
  ok memory      459ms  {"mirrored": 0}
```

服务侧接线实测：`flywheel_interval=900 / disabled=False`，`flywheel_round(cfg)` 返回 `ok:true`，日志 `[flywheel] tick rc=0 ok …`。

## 4 诚实边界

- `candidates built=0` 是正常：候选按消息去重，已建过的不重复建。
- `signed=0 / outcomes=0`：签字与回填仍是 0（签字问题按 m10375 暂缓）。
- `wal candidates=[]`：同类信号还没达 3 条阈值，飞轮在等积累，不是坏。
- 记忆 `mirrored=0`：这一轮没有新提交。
