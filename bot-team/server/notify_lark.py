#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G1 飞书出站接线：任务签字收口（signed/finalized）→ 4 合 1 网关推 mo_p2p 回执。

调用方：server/app.py 的 post_finalize（主路径）与 reconcile_sign_state（兜底路径）。
性质：best-effort——任何失败只写日志，绝不影响交付主流程；防重靠任务目录
标记文件（.lark-signed.json），finalize 与 reconcile 双路径对同一任务只推一次；
发送失败不落标记，下次读路径触发 reconcile 时自动重试。

网关定位：env LARK_SEND 优先，默认 <bot-2>/tools/lark_send.py（bot-team 的上两级）。
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MARK_FILE = ".lark-signed.json"
PURPOSE = "mo_p2p"


def _log(message):
    try:
        print("[lark-notify] " + str(message), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _emit(cfg, name, props):
    """埋点不得中断业务：导入/校验/写盘失败一律吞掉。"""
    try:
        root = str(cfg.root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from core import telemetry
        telemetry.emit(name, props, root=root)
    except Exception:  # noqa: BLE001
        pass


def channel_agent_id(cfg):
    """推送归属：channels 表里 feishu 通道绑定的 agent（D1 起为工作助手团队）。

    读不到就返回空串——不编造 agent id，聚合侧会把它当缺失而不是当某个真实角色。
    """
    db = Path(cfg.root).parents[1] / ".octop" / "octop.db"
    if not db.is_file():
        return ""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            row = con.execute(
                "SELECT agent_id FROM channels WHERE kind='feishu' AND enabled=1 "
                "ORDER BY id LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return ""
    return str(row[0] or "") if row else ""


def _message_id(output):
    """从网关输出里抠 message_id（tenant 直发返回 data.message_id，裸 JSON）。"""
    m = re.search(r'"message_id"\s*:\s*"([^"]+)"', output or "")
    return m.group(1) if m else ""


def _readj(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def find_gateway(cfg=None):
    """lark_send.py 定位：env LARK_SEND 覆盖，默认 bot-2/tools/lark_send.py。"""
    env_path = os.environ.get("LARK_SEND")
    candidates = [Path(env_path)] if env_path else []
    if cfg is not None:
        # cfg.root = bot-team → parents[1] = bot-2
        candidates.append(Path(cfg.root).parents[1] / "tools" / "lark_send.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _chart_image_key(gateway, tdir):
    """渲染回执配图并上传，返回 image_key；任何一步失败都返回 None（卡片退回纯文字）。"""
    import importlib.util

    chart_py = tdir.parents[1] / "tools" / "receipt_chart.py"
    try:
        spec = importlib.util.spec_from_file_location("receipt_chart", str(chart_py))
        receipt_chart = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(receipt_chart)
        png = receipt_chart.render(tdir)
    except Exception as exc:  # noqa: BLE001
        _log("配图渲染失败：%s" % exc)
        return None
    if not png:
        return None
    cmd = [sys.executable, "-B", str(gateway), "--purpose", PURPOSE,
           "--upload-image", str(png), "--auth", "tenant"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                             encoding="utf-8", errors="replace")
        payload = json.loads((out.stdout or "").strip() or "{}")
    except Exception as exc:  # noqa: BLE001
        _log("配图上传失败：%s" % exc)
        return None
    if payload.get("code") == 0 and payload.get("image_key"):
        return payload["image_key"]
    _log("配图上传被拒：%s" % (out.stdout or "")[:200])
    return None


def build_card(task_id, decision, signer, closure, image_key=None):
    """交付签字回执卡片（飞书卡片 2.0 + 三个 a/b/c 回调按钮）。

    为什么用卡片：文字回执只能放链接，且回信要打字；卡片能把结论、议题、回看
    日期并排显示，并把「采纳/转人工/转分析」变成一键回调（value 里自带 task_id，
    归属零歧义）。按钮未订阅回调时仍可退回打字回 a/b/c。
    """
    d_id = str(decision.get("id") or "-")
    d_state = str(decision.get("status") or "-")
    question = str(decision.get("question") or "").strip()[:120] or "（未记录）"
    due = str(closure.get("due") or "").strip() or "未设定"
    verdict = str(closure.get("verdict") or "").strip()
    if verdict not in ("hit", "miss", "partial"):
        verdict = ""       # unverified/空 都不是结论，不能显示成「已回填」
    due_line = f"**T+7 回看**  {due}" + (f"（已回填：{verdict}）" if verdict else "")

    def btn(text, letter, kind="default"):
        return {"tag": "button", "text": {"tag": "plain_text", "content": text},
                "type": kind,
                "behaviors": [{"type": "callback",
                               "value": {"letter": letter, "task_id": task_id}}]}

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text", "content": "交付签字回执"},
            "subtitle": {"tag": "plain_text", "content": task_id},
            "template": "blue",
        },
        "body": {"elements": [
            {"tag": "markdown",
             "content": f"**决策**  {d_id} · {d_state}\n**签署**  {signer or '-'}"},
            {"tag": "markdown", "content": f"**议题**  {question}"},
            {"tag": "markdown", "content": due_line},
        ] + ([{"tag": "img", "img_key": image_key, "mode": "fit_horizontal",
               "preview": True,
               "alt": {"tag": "plain_text", "content": "关键指标序列与归因置信度"}}]
             if image_key else []) + [
            {"tag": "hr"},
            {"tag": "markdown", "content": "这份建议是否采纳？点下面按钮，或直接回 **a** / **b** / **c**。"},
            {"tag": "column_set", "flex_mode": "bisect", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [btn("采纳归档", "a", "primary")]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [btn("转人工", "b")]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [btn("转补充分析", "c")]},
            ]},
        ]},
    }


def notify_signed(cfg, tdir):
    """签字收口后推飞书回执；幂等（标记文件），失败静默。"""
    tdir = Path(tdir)
    # 通道未查到绑定时记 unbound：不编造 agent id，也让聚合侧看得见缺口
    agent_id = channel_agent_id(cfg) or "unbound"
    emitted = False
    try:
        mark = tdir / MARK_FILE
        if mark.exists():
            return
        gateway = find_gateway(cfg)
        if gateway is None:
            _log(f"{tdir.name} 网关不存在，跳过（设 LARK_SEND 指定 lark_send.py 路径）")
            _emit(cfg, "push_sent", {"task_id": tdir.name, "agent_id": agent_id,
                                     "ok": False, "error_kind": "no_gateway"})
            return
        status = _readj(tdir / "status.json")
        decision = (_readj(tdir / "loop" / "decision.json")
                    or _readj(tdir / "decision.json"))
        closure = (_readj(tdir / "loop" / "closure.json")
                   or _readj(tdir / "closure.json"))
        signer = ((decision.get("approval") or {}).get("by")
                  or status.get("signer") or cfg.owner)
        card = build_card(tdir.name, decision, signer, closure,
                          image_key=_chart_image_key(gateway, tdir))
        # --auth tenant：调度/服务上下文没有 Trae 注入的 user token，走应用身份
        # （与 reply_consume 的回执口径一致；user 身份在此上下文必失败）
        cmd = [sys.executable, "-B", str(gateway), "--purpose", PURPOSE,
               "--card", json.dumps(card, ensure_ascii=False), "--send",
               "--auth", "tenant", "--idempotency-key", f"signed-{tdir.name}"]
        started = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        latency_ms = int((time.perf_counter() - started) * 1000)
        output = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        payload = {"task_id": tdir.name, "agent_id": agent_id, "ok": ok,
                   "latency_ms": latency_ms}
        message_id = _message_id(output) if ok else ""
        if message_id:
            payload["message_id"] = message_id
        if not ok:
            payload["error_kind"] = "gateway_rc"
        _emit(cfg, "push_sent", payload)
        emitted = True
        if ok:
            mark.write_text(json.dumps({"sent_at": datetime.now().isoformat(timespec="seconds"),
                                        "gateway": str(gateway),
                                        "message_id": message_id or None,
                                        "agent_id": agent_id},
                                       ensure_ascii=False),
                            encoding="utf-8")
            _log(f"{tdir.name} 回执已推送")
        else:
            _log(f"{tdir.name} 推送失败 rc={proc.returncode}: {output[:200]}")
    except Exception as exc:  # noqa: BLE001
        _log(f"{tdir.name} 异常（不影响主流程）：{exc}")
        if not emitted:
            _emit(cfg, "push_sent", {"task_id": tdir.name, "agent_id": agent_id,
                                     "ok": False, "error_kind": "exception"})
