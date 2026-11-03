#!/usr/bin/env python3
"""跨机「知悉层」导出器。

问题：两台电脑不能双向同步 Tobs 的 Bot 身份（botharness.db）与运行时账本，
但**同一支 bot 团队至少要能读到同一份项目上下文**。

做法：主机器把「做了什么」压成只读快照写进 bot-memory 仓的 `_shared/`：
  - `artifacts-index.jsonl` 每个任务产物的清单（task_id/name/type/size/sha256）
  - `timeline.jsonl`         关键事件（出卡/签字/回填/产物保存），一行一件
  - `PROJECT_STATE.md`       人可读的当前进度快照（Bot 也读这个）
  - `README.md`              知悉层说明（只写一次）

纪律：
  - `_shared/` **只由主机器写**，副机只 pull 不写 → 无冲突。
  - 不导出正文（产物正文有各自仓库/目录），只导出「索引 + 摘要 + sha256」。
  - 不含凭据；token/密钥形态的字段一律丢弃。

用法：
  python tools/sync_shared.py --shared-dir ../bot-memory/_shared
  python tools/sync_shared.py --dry-run --json   # 只看不写
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _default_shared() -> Path:
    """bot-memory 的位置因部署而异：环境变量 > 相邻 > 上一级 > 同级父目录。"""
    env = os.environ.get("BOT_MEMORY_DIR")
    if env:
        return Path(env) / "_shared"
    for base in (ROOT.parent, ROOT.parent.parent, ROOT):
        cand = base / "bot-memory"
        if cand.is_dir():
            return cand / "_shared"
    return ROOT.parent / "bot-memory" / "_shared"


DEFAULT_SHARED = _default_shared()
EVENT_PREFIX = "bot_team_"
# 只收「有意义、能回答『今天做了什么』」的事件
TIMELINE_EVENTS = (
    "bot_team_card_issued",
    "bot_team_card_signed",
    "bot_team_outcome_recorded",
    "bot_team_artifact_saved",
    "bot_team_review_recorded",
    "bot_team_memory_change",
)
SECRET_HINTS = ("secret", "token", "password", "api_key", "apikey", "credential")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return "unknown"


def _clean(obj, depth: int = 0):
    """递归丢掉凭据样字段，深度限 4（防大对象）。"""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in str(k).lower() for h in SECRET_HINTS):
                continue
            out[str(k)] = _clean(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_clean(v, depth + 1) for v in obj][:50]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        if isinstance(obj, str) and len(obj) > 500:
            return obj[:500] + "…"
        return obj
    return str(obj)[:200]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect_artifacts(runs_dir: Path) -> list:
    """扫 runs/<task>/artifacts/manifest.json，只取索引字段。"""
    rows = []
    if not runs_dir.is_dir():
        return rows
    for manifest in sorted(runs_dir.glob("*/artifacts/manifest.json")):
        doc = _read_json(manifest)
        if not isinstance(doc, dict):
            continue
        task_id = doc.get("task_id") or manifest.parent.parent.name
        for item in doc.get("artifacts") or []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "task_id": task_id,
                "name": item.get("name"),
                "type": item.get("type"),
                "size": item.get("size"),
                "sha256": item.get("sha256"),
                "is_demo": bool(item.get("is_demo")),
                "created_at": doc.get("generated_at") or item.get("created_at"),
            })
    return rows


def collect_timeline(events_path: Path, limit: int = 500) -> list:
    """从 runtime/events.jsonl 抽关键事件；坏行跳过，不抛。"""
    rows = []
    if not events_path.is_file():
        return rows
    try:
        lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("event") or "")
        if not name.startswith(EVENT_PREFIX) or name not in TIMELINE_EVENTS:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        rows.append({
            "ts": ev.get("ts"),
            "event": name.replace(EVENT_PREFIX, "", 1),
            "seq": ev.get("seq"),
            "refs": ev.get("refs") if isinstance(ev.get("refs"), (dict, list)) else None,
            "payload": _clean(payload),
        })
    return rows[-limit:]


def build(runs_dir: Path, events_path: Path, limit: int = 500) -> dict:
    arts = collect_artifacts(runs_dir)
    line = collect_timeline(events_path, limit=limit)
    issued = [r for r in line if r["event"] == "card_issued"]
    signed = [r for r in line if r["event"] == "card_signed"]
    outcomes = [r for r in line if r["event"] == "outcome_recorded"]
    return {
        "schema": "shared.awareness.v1",
        "generated_at": _now(),
        "host": _host(),
        "counts": {
            "artifacts": len(arts),
            "timeline": len(line),
            "cards_issued": len(issued),
            "cards_signed": len(signed),
            "outcomes": len(outcomes),
        },
        "artifacts": arts,
        "timeline": line,
        "tasks_seen": len({r.get("refs", {}).get("task_id") for r in line if isinstance(r.get("refs"), dict)}),
    }


STATE_TPL = """# 团队项目快照（知悉层）

> 由主机器 `tools/sync_shared.py` 自动生成。**只读**：副机只 pull 不改。
> 生成时间：{generated_at}　来源主机：{host}

## 一、现在在做什么

{now_doing}

## 二、数字（可复算）

| 指标 | 值 |
| --- | --- |
| 产物件数 | {artifacts} |
| 时间线事件 | {timeline} |
| 出卡 | {issued} |
| 签字 | {signed} |
| 回填 | {outcomes} |

## 三、最近发生了什么（最后 10 条）

{recent}

## 四、可以调的本机接口（副机同款代码都有）

- `GET /api/workitems` 事项账本（唯一事实源 `runtime/workitems/current.json`）
- `GET /api/reminders` 今日硬提醒（逾期/今日到期）
- `POST /api/context/ask` 本机上下文问答（带出处）
- `POST /api/dialog` 团长三回合定案
- `POST /api/tasks/<id>/sign` / `/finalize` / `/review` / `/outcome`

## 五、边界

- 本文件不含产物正文，只有索引与 sha256；正文在各自主机的 `runs/<task>/artifacts/`。
- 账本 `current.json` **不在**知悉层：双机同时改会互相覆盖，只能一台当「账本主」。
- Bot 身份（`botharness.db`）不在知悉层：新机按 `docs/product/MIGRATION_TO_NEW_PC_2026-11-03.md` 重建。
"""

README = """# _shared —— 跨机知悉层

同一支 bot 团队在多台电脑上跑时，**不能同步全部状态，但必须能读到同一份上下文**。
这个目录就是那份上下文：由主机器单向生成，副机只读。

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `PROJECT_STATE.md` | 当前进度快照（人读 + Bot 读） | 主机器 |
| `artifacts-index.jsonl` | 每个任务产物的索引（task_id/name/size/sha256） | 主机器 |
| `timeline.jsonl` | 出卡/签字/回填/产物保存等关键事件 | 主机器 |

规则：
1. 只有主机器跑 `python tools/sync_shared.py` 然后 `git push`；
2. 副机 `git pull` 后即可读，**不要在这里手改**（会被下一次生成覆盖）；
3. 不含凭据、不含产物正文、不含账本（账本只能一台主）。
"""


def render_state(doc: dict) -> str:
    now = []
    for ev in doc["timeline"][-3:]:
        p = ev.get("payload") or {}
        task = (ev.get("refs") or {}).get("task_id") if isinstance(ev.get("refs"), dict) else None
        bits = [ev["event"]]
        if task:
            bits.append(str(task))
        if p.get("verdict_label"):
            bits.append(str(p["verdict_label"]))
        if p.get("name"):
            bits.append(str(p["name"]))
        now.append("- " + " · ".join(bits))
    recent = []
    for ev in doc["timeline"][-10:]:
        recent.append(f"- `{ev['ts']}` {ev['event']}" + (
            f" task={ev['refs'].get('task_id')}" if isinstance(ev.get("refs"), dict) and ev["refs"].get("task_id") else ""))
    c = doc["counts"]
    return STATE_TPL.format(
        generated_at=doc["generated_at"], host=doc["host"],
        now_doing="\n".join(now) if now else "（暂无事件）",
        artifacts=c["artifacts"], timeline=c["timeline"],
        issued=c["cards_issued"], signed=c["cards_signed"], outcomes=c["outcomes"],
        recent="\n".join(recent) if recent else "（暂无）",
    )


def write(shared_dir: Path, doc: dict) -> dict:
    shared_dir.mkdir(parents=True, exist_ok=True)
    written = []
    ai = shared_dir / "artifacts-index.jsonl"
    ai.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in doc["artifacts"]), encoding="utf-8")
    written.append(str(ai))
    tl = shared_dir / "timeline.jsonl"
    tl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in doc["timeline"]), encoding="utf-8")
    written.append(str(tl))
    st = shared_dir / "PROJECT_STATE.md"
    st.write_text(render_state(doc), encoding="utf-8")
    written.append(str(st))
    rd = shared_dir / "README.md"
    if not rd.exists():
        rd.write_text(README, encoding="utf-8")
        written.append(str(rd))
    return {"shared_dir": str(shared_dir), "written": written}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跨机知悉层导出器")
    ap.add_argument("--shared-dir", default=str(DEFAULT_SHARED))
    ap.add_argument("--runs", default=str(ROOT / "runs"))
    ap.add_argument("--events", default=str(ROOT / "runtime" / "events.jsonl"))
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    doc = build(Path(a.runs), Path(a.events), limit=a.limit)
    if a.dry_run:
        out = {"ok": True, "dry_run": True, "counts": doc["counts"]}
    else:
        out = {"ok": True, **write(Path(a.shared_dir), doc), "counts": doc["counts"]}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        c = doc["counts"]
        print(f"[OK] 产物 {c['artifacts']} 件 / 时间线 {c['timeline']} 条 / 出卡 {c['cards_issued']} / 签字 {c['cards_signed']} / 回填 {c['outcomes']}")
        if not a.dry_run:
            print(f"[OK] 写入 {a.shared_dir}（{len(out['written'])} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
