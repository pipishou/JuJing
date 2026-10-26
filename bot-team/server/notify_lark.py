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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MARK_FILE = ".lark-signed.json"


def _log(message):
    try:
        print("[lark-notify] " + str(message), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


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


def _base_url():
    host = os.environ.get("BOT_HOST") or "127.0.0.1"
    port = os.environ.get("BOT_PORT") or "8787"  # 与 app.py DEFAULT_PORT 对齐
    return f"http://{host}:{port}"


def notify_signed(cfg, tdir):
    """签字收口后推飞书回执；幂等（标记文件），失败静默。"""
    tdir = Path(tdir)
    try:
        mark = tdir / MARK_FILE
        if mark.exists():
            return
        gateway = find_gateway(cfg)
        if gateway is None:
            _log(f"{tdir.name} 网关不存在，跳过（设 LARK_SEND 指定 lark_send.py 路径）")
            return
        status = _readj(tdir / "status.json")
        decision = (_readj(tdir / "loop" / "decision.json")
                    or _readj(tdir / "decision.json"))
        closure = (_readj(tdir / "loop" / "closure.json")
                   or _readj(tdir / "closure.json"))
        signer = ((decision.get("approval") or {}).get("by")
                  or status.get("signer") or cfg.owner)
        lines = [
            "**交付签字回执**",
            f"任务：{tdir.name}",
            f"决策：{decision.get('id') or '-'} · {decision.get('status') or '-'} · 签署 {signer}",
        ]
        question = str(decision.get("question") or "").strip()
        if question:
            lines.append(f"议题：{question[:80]}")
        due = str(closure.get("due") or "").strip()
        if due:
            lines.append(f"T+7 回看：{due}（到期未回填将催办）")
        lines.append(f"交付卡：{_base_url()}/api/tasks/{tdir.name}/delivery-card")
        cmd = [sys.executable, "-B", str(gateway), "--purpose", "mo_p2p",
               "--markdown", "\n".join(lines), "--send",
               "--idempotency-key", f"signed-{tdir.name}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        if proc.returncode == 0:
            mark.write_text(json.dumps({"sent_at": datetime.now().isoformat(timespec="seconds"),
                                        "gateway": str(gateway)}, ensure_ascii=False),
                            encoding="utf-8")
            _log(f"{tdir.name} 回执已推送")
        else:
            _log(f"{tdir.name} 推送失败 rc={proc.returncode}: "
                 f"{((proc.stdout or '') + (proc.stderr or ''))[:200]}")
    except Exception as exc:  # noqa: BLE001
        _log(f"{tdir.name} 异常（不影响主流程）：{exc}")
