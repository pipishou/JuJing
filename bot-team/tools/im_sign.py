#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IM 签字：白名单身份 → /sign → /finalize，并留一份 IM 侧审计旁证。

为什么不用「回信 a/b/c」当签字
------------------------------
a/b/c 是**采纳记录**（你的选择进 ledger），签字是**对报告的字节负责**
（sha256 写进 signature.json）。两者混在一起，产出率就会被「点了按钮」刷高。
所以这条路单独走，且必须过 `im_signers` 白名单。

三个刻意选择
------------
1. **不改 report.json 一个字节**：签字前后 sha256 必须一致，否则「签的到底是哪一版」说不清。
2. **审计旁证单独落盘** `runs/<id>/im-sign.json`：IM userid、会话、消息 id、时间、
   签的是哪个 sha，都可回放；旁证不进签名文件，避免污染验签口径。
3. **幂等**：同一 task 已 approved 时直接返回 already，不重复签（重复签会让
   「谁签的」变成最后一次覆盖）。

CLI：
    python -B tools/im_sign.py --task-id T-20261028-001 --sender ou_xxx [--chat-id oc_xxx]
    python -B tools/im_sign.py --task-id ... --sender ... --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BOT_TEAM = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import im_signers  # noqa: E402

TZ = timezone(timedelta(hours=8))
DEFAULT_SERVER = os.environ.get("BOT_SERVER") or "http://127.0.0.1:8787"
APPROVED_STATES = ("approved", "finalized", "signed")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _http(method, url, body=None, timeout=30):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "HTTP %s: %s" % (exc.code, raw[:200])}


def runs_root(server_root=None):
    """与服务端同口径：BOT_RUNS_ROOT 优先，否则 bot-team/runs。"""
    env = os.environ.get("BOT_RUNS_ROOT")
    if env:
        return Path(env)
    if server_root:
        return Path(server_root) / "runs"
    return BOT_TEAM / "runs"


def write_audit(task_id, event, root=None):
    """把 IM 侧旁证追加到 runs/<id>/im-sign.json（原子写，保留 history）。"""
    base = Path(root) if root else runs_root()
    path = base / task_id / "im-sign.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"task_id": task_id, "events": []}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            doc.setdefault("events", [])
        except Exception:  # noqa: BLE001 - 旁证坏了不阻断签字，但要留痕
            doc = {"task_id": task_id, "events": [], "note": "原有旁证不可解析，已重建"}
    doc["events"].append(event)
    doc["events"] = doc["events"][-20:]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def sign_task(task_id, sender_id, *, chat_id="", message_id="", server=None,
              dry_run=False, runs_dir=None, signers_path=None):
    """IM 签字主流程。返回结构化结果，永不抛（调用方拿 ok 判断）。"""
    server = (server or DEFAULT_SERVER).rstrip("/")
    who = im_signers.resolve(sender_id, chat_id=chat_id, path=signers_path)
    if not who:
        return {"ok": False, "stage": "identity", "error": "未授权签字人",
                "sender": im_signers.mask(sender_id)}
    base = {"task_id": task_id, "signer": who["signer"], "im_id": im_signers.mask(who["id"]),
            "source": who["source"], "chat_id": chat_id, "message_id": message_id}

    detail = _http("GET", "%s/api/tasks/%s" % (server, task_id))
    # 注意：GET /api/tasks/<id> 成功时不带 ok 字段（只有 error/404 才带），
    # 所以判成败要看 status 字典在不在，不能看 ok——这是踩过的坑。
    if not isinstance(detail.get("status"), dict):
        return dict(base, ok=False, stage="detail",
                    error=detail.get("error") or "任务不存在")
    state = ((detail.get("status") or {}).get("state") or "").lower()
    if state in APPROVED_STATES:
        return dict(base, ok=True, stage="already", state=state, message="已经签过，不重复签")

    delivery = _http("GET", "%s/api/tasks/%s/delivery" % (server, task_id))
    if not delivery.get("ok"):
        return dict(base, ok=False, stage="delivery",
                    error=delivery.get("error") or "交付卡不可读")
    sha = str(delivery.get("signature_sha256") or "")
    if dry_run:
        return dict(base, ok=True, stage="dry-run", will_sign_with=who["signer"], sha256=sha)

    signed = _http("POST", "%s/api/tasks/%s/sign" % (server, task_id), {"signer": who["signer"]})
    if not signed.get("ok"):
        event = dict(base, ts=_now(), action="sign", ok=False, error=signed.get("error"))
        write_audit(task_id, event, runs_dir)
        return dict(base, ok=False, stage="sign", error=signed.get("error") or "签字失败")

    final = _http("POST", "%s/api/tasks/%s/finalize" % (server, task_id), {"by": who["signer"]})
    event = dict(base, ts=_now(), action="finalize", ok=bool(final.get("ok")),
                 sha256=signed.get("sha256") or sha,
                 decision=final.get("decision"), status=final.get("status"),
                 error=None if final.get("ok") else final.get("error"))
    path = write_audit(task_id, event, runs_dir)
    if not final.get("ok"):
        return dict(base, ok=False, stage="finalize", error=final.get("error") or "终态失败",
                    audit=str(path))
    return dict(base, ok=True, stage="approved", decision=final.get("decision"),
                status=final.get("status"), sha256=event["sha256"], audit=str(path))


def main(argv=None):
    ap = argparse.ArgumentParser(description="IM 签字（白名单身份 → sign → finalize）")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--sender", required=True, help="IM 侧身份（飞书 open_id / 企微 userid）")
    ap.add_argument("--chat-id", default="")
    ap.add_argument("--message-id", default="")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--runs-dir", default="", help="审计旁证落盘目录（默认按 BOT_RUNS_ROOT）")
    ap.add_argument("--signers-file", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        result = sign_task(args.task_id, args.sender, chat_id=args.chat_id,
                           message_id=args.message_id, server=args.server,
                           dry_run=args.dry_run,
                           runs_dir=args.runs_dir or None,
                           signers_path=args.signers_file or None)
    except im_signers.SignerConfigError as exc:
        print(json.dumps({"ok": False, "stage": "config", "error": str(exc)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
