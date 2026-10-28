# -*- coding: utf-8 -*-
"""飞书卡片按钮回调消费者（长连接 + card.action.trigger）。

职责边界：
- 只处理 a/b/c 三个回调按钮，只认 MO_OPEN_ID 的点击，其余一律忽略。
- 3 秒内必须响应：写账本 + 回 toast/改卡片都在同一次处理里完成（都是本地/轻请求）。
- 不改交付主流程：账本写失败就只回 toast，不让用户看到报错。

启动（二选一）：
- 作为 octop 的钩子：OCTOP_CARD_HOOK=<本文件路径>::on_card_action，由网关 WS 客户端调用。
- 独立常驻：python tools/card_consumer.py（仅在没有 octop 接管回调时使用；两个 WS 客户端会分流事件）。
"""
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BOT_TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(BOT_TEAM.parents[1] / "tools"))   # lark_send 在 bot-2/tools

import lark_oapi as lark  # noqa: E402
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    P2CardActionTrigger, P2CardActionTriggerResponse)

from core import telemetry  # noqa: E402
from lark_send import load_app_creds  # noqa: E402
from reply_consume import MO_OPEN_ID  # noqa: E402

LETTERS = {"a": "采纳归档", "b": "转人工", "c": "转补充分析"}
LOG_PATH = BOT_TEAM / ".telemetry" / "card-consumer.log"
CLICK_LOG = BOT_TEAM / ".telemetry" / "card-clicks.jsonl"


def _log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    # 被 octop 以钩子方式加载时，文件日志可能因进程环境失败；走宿主 logger 兜底可见
    try:
        logging.getLogger("card_consumer").info(line)
    except Exception:  # noqa: BLE001
        pass


def _record_click(payload):
    """事件级证据：回调一进来就落一行，先于任何分支判断（用于区分「没收到」与「收到了但分支返回」）。"""
    try:
        CLICK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CLICK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
    except Exception as exc:  # noqa: BLE001
        try:
            logging.getLogger("card_consumer").warning("click-log 写入失败：%r", exc)
        except Exception:  # noqa: BLE001
            pass


def _readj(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _choice_path(task_id):
    return BOT_TEAM / "runs" / task_id / ".card-choice.json"


def _last_push_ts(task_id):
    """最近一次成功推送的时刻（epoch 秒）；账本里是 ISO 串，必须解析，解析失败返回 None。"""
    ts = None
    for row in telemetry.read(root=str(BOT_TEAM)):
        if row.get("name") == "push_sent" and row.get("task_id") == task_id \
                and row.get("ok"):
            try:
                ts = datetime.fromisoformat(str(row.get("ts") or "")).timestamp()
            except ValueError:
                continue
    return ts


def _already_recorded(task_id, letter):
    """同一任务同一字母是否已有回信记录（文字回信或按钮），返回 attr 或 None。"""
    for row in telemetry.read(root=str(BOT_TEAM)):
        if row.get("name") == "reply" and row.get("task_id") == task_id \
                and row.get("letter") == letter:
            return row.get("attr") or "unknown"
    return None


def _result_card(task_id, letter, at):
    tdir = BOT_TEAM / "runs" / task_id
    decision = _readj(tdir / "loop" / "decision.json") or _readj(tdir / "decision.json")
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text", "content": "交付签字回执"},
            "subtitle": {"tag": "plain_text", "content": task_id},
            "template": "green" if letter == "a" else "orange",
        },
        "body": {"elements": [
            {"tag": "markdown",
             "content": f"**决策**  {decision.get('id') or '-'} · {decision.get('status') or '-'}"},
            {"tag": "markdown",
             "content": f"**回信**  {letter} · {LETTERS[letter]}（点击于 {at}）"},
            {"tag": "hr"},
            {"tag": "markdown", "content": "已记录，无需再回复。"},
        ]},
    }


def _on_card(data: P2CardActionTrigger):
    resp = P2CardActionTriggerResponse()
    ev = getattr(data, "event", None)
    operator = getattr(getattr(ev, "operator", None), "open_id", None)
    value = getattr(getattr(ev, "action", None), "value", None) or {}
    message_id = getattr(getattr(ev, "context", None), "open_message_id", None)
    task_id = str(value.get("task_id") or "")
    letter = str(value.get("letter") or "").lower()
    _record_click({"ts": datetime.now().isoformat(timespec="seconds"),
                   "operator": operator or "", "task_id": task_id,
                   "letter": letter, "message_id": message_id or "",
                   "value": value})

    if operator != MO_OPEN_ID:
        resp.toast = {"type": "error", "content": "无权操作这张回执"}
        return resp
    if letter not in LETTERS or not task_id:
        resp.toast = {"type": "error", "content": "按钮参数异常"}
        _log("bad value=%s task=%s" % (value, task_id))
        return resp

    at = datetime.now().strftime("%Y-%m-%d %H:%M")
    prev = _readj(_choice_path(task_id))
    if prev.get("letter") == letter:
        resp.toast = {"type": "info", "content": f"已记录过：{letter} · {LETTERS[letter]}"}
        return resp
    # 文字回信已入账的同任务同字母：不重复记账，避免采纳率被同一决策算两次
    if _already_recorded(task_id, letter):
        resp.toast = {"type": "info", "content": f"已记录过：{letter} · {LETTERS[letter]}"}
        _log("dup-skip task=%s letter=%s by=%s" % (task_id, letter, operator))
        return resp

    try:
        _choice_path(task_id).parent.mkdir(parents=True, exist_ok=True)
        _choice_path(task_id).write_text(
            json.dumps({"letter": letter, "at": at, "message_id": message_id},
                       ensure_ascii=False), encoding="utf-8")
        props = {"task_id": task_id, "letter": letter, "attr": "card_button",
                 "thread_id": message_id or ""}
        push_ts = _last_push_ts(task_id)
        if isinstance(push_ts, (int, float)):
            props["latency_s"] = max(0, int(time.time() - float(push_ts)))
        telemetry.emit("reply", props, root=str(BOT_TEAM))
        ok = True
    except Exception as exc:  # noqa: BLE001
        _log("记账失败 %s: %s" % (task_id, exc))
        try:
            logging.getLogger("card_consumer").warning("记账失败 task=%s: %r", task_id, exc)
        except Exception:  # noqa: BLE001
            pass
        ok = False

    # 记账没成就不许改卡：卡片说「已记录」而 toast 说「失败」是自相矛盾，会骗用户
    if not ok:
        resp.toast = {"type": "error", "content": "记录失败，请直接回文字 a/b/c"}
        return resp
    resp.toast = {"type": "success", "content": f"已记为 {letter} · {LETTERS[letter]}"}
    resp.card = {"type": "raw", "data": _result_card(task_id, letter, at)}
    _log("click task=%s letter=%s by=%s msg=%s ok=%s"
         % (task_id, letter, operator, message_id, ok))
    return resp


# octop 网关钩子入口（OCTOP_CARD_HOOK=...::on_card_action）
on_card_action = _on_card


def _boot_marker():
    """被宿主进程加载时先写一条启动行：用来把「宿主进程写不进 bot-team」和「还没人点」分开。"""
    try:
        CLICK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CLICK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                 "event": "hook_loaded", "pid": os.getpid()},
                                ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        try:
            logging.getLogger("card_consumer").warning("boot-marker 写入失败：%r", exc)
        except Exception:  # noqa: BLE001
            pass


_boot_marker()      # 宿主加载即落一行启动标记


def main():
    app_id, app_secret, src = load_app_creds()
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_card_action_trigger(_on_card) \
        .build()
    client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    _log("连接中 app=%s creds=%s" % (app_id, src))
    client.start()          # 阻塞；断线自动重连
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
