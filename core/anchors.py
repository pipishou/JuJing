#!/usr/bin/env python3
"""三个判断锚点（F2）：出卡 / 签字 / 回填 各发一条事件。

设计纪律：
- **绝不阻断主链路**：锚点失败只写 stderr，不影响签字/回填/出卡本身。
- **只记事实**：卡型、档位、总分、是否演示、签字指纹、回填窗口——不记正文。
- 事件口径与 `core/eventlog.py` 一致（`bot_team_` 前缀、seq 单调、append-only）。
- 判断只认这三个锚点：群里被 @ 到、读过卡、点过"已读"都不算决策。

对应飞轮：N1 采纳率 = signed/issued；N3 定案耗时 = signed_at - issued_at；
值得率 = judged_ok/(judged_ok+judged_not_ok)；回填率 = outcome/issued。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402


def _safe(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 —— 锚点是旁证，不许打断主流程
        print(f"[anchors] 事件写入失败（已忽略）：{exc}", file=sys.stderr)
        return False


def _scorecard(report: dict) -> tuple[float | None, str, str]:
    sc = (report or {}).get("scorecard") or {}
    total = sc.get("total")
    try:
        total = float(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return total, str(sc.get("verdict") or ""), str(sc.get("label") or "")


def card_issued(task_id: str, report: dict, *, source: str = "run_task",
                events_path: str | None = None) -> bool:
    """A1：卡已产出（进入待签）。"""
    total, verdict, label = _scorecard(report)
    report = report or {}
    return _safe(
        eventlog.emit,
        "card_issued",
        actor=str(report.get("pack") or "bot-team"),
        refs=[task_id],
        payload={
            "task_id": task_id,
            "card_kind": report.get("card_kind") or "attribution",
            "verdict": verdict,
            "verdict_label": label,
            "total": total,
            "is_demo": bool(report.get("is_demo")),
            "evidence_tier": report.get("evidence_tier") or "",
            "source": source,
            "origin": "anchors",
        },
        path=events_path,
    )


def card_signed(task_id: str, *, signer: str, report_sha256: str = "",
                card_kind: str = "", elapsed_ms: int | None = None,
                events_path: str | None = None) -> bool:
    """A3：人签了字（这是唯一的采纳信号）。"""
    payload = {
        "task_id": task_id,
        "signer": signer,
        "report_sha256": report_sha256,
        "card_kind": card_kind or "attribution",
        "origin": "anchors",
    }
    if elapsed_ms is not None:
        payload["elapsed_ms_since_issue"] = int(elapsed_ms)
    return _safe(
        eventlog.emit,
        "card_signed",
        actor=signer or "unknown",
        refs=[task_id],
        payload=payload,
        path=events_path,
    )


def outcome_recorded(task_id: str, *, observed_outcome: str, window_days: int | None = None,
                     source: str = "api", events_path: str | None = None) -> bool:
    """A4：T+7 实测回填（判断对错最终由现实裁定）。"""
    payload = {
        "task_id": task_id,
        "observed_outcome": (observed_outcome or "")[:200],
        "origin": "anchors",
        "source": source,
    }
    if window_days is not None:
        payload["window_days"] = int(window_days)
    return _safe(
        eventlog.emit,
        "outcome_recorded",
        actor="老板",
        refs=[task_id],
        payload=payload,
        path=events_path,
    )
