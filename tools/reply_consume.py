#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G2 回信消费（批次 B 乙'·调度拉 + 应用身份）：拉 Mo 的 a/b/c 回信 → 机械化留痕/转交。

设计纪律（c14 拍板）：机械化/语义分层。
  脚本只做确定性动作：查信 → 判别 → 防重 → 记录 → 委托网关回执；
  a/b/c 的语义执行（归档决策、补充分析）归会话轮，本脚本不生成业务结论。

凭证路线（c16 勘误，2026-10-24）：
  不再用 lark-cli 的 user OAuth token（由 Trae 插件 env 注入，调度上下文取不到）。
  改用与 Octop 原生桥同一个应用的 tenant token：app_id/app_secret 持久化在
  bot-2/.octop/octop.db channels 表，运行时只读、不落盘、不打印。

判别口径（c6 预案 + c7 通道勘误 + c16 实测修正）：
  chat=oc_559ba779…（桥应用 ↔ Mo 的 p2p）；sender_type=user 且 sender.id=MO_OPEN_ID；
  msg_type=text（网关出站一律 post，故 post 一律排除，避免自污染）；
  body.content 是 {"text":"..."} 需解析后取首字符；多字母＝歧义 → 不猜，转「等人工」。

防重：trace 内 message_id 全集（append-only）+ cursor 双保险。
留痕：bot-2/.junshi/reply-consume.jsonl（append-only）＋ runs/<task>/reply-ack.json。
未知回信（非 a/b/c 的 Mo 文本）不消费、不回复，避免抢既有桥的话头。

用法：
  python tools/reply_consume.py --dry                 # 只判别不动作
  python tools/reply_consume.py                       # 真消费（增量）
  python tools/reply_consume.py --since "2026-10-24T10:25:54+08:00"   # 回溯一段
  python tools/reply_consume.py --self-test           # 判别器断言（机检门）
计划任务：tools/install_reply_consume_task.ps1（每 10 分钟）
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHAT_ID = "oc_559ba779af9fa9e32c56f4aacc71bb96"
# 注意：open_id 是「应用维度」的。此 ID 是桥应用 cli_aa536a0b3db89d29 视角下的 Mo，
# 与 lark-cli 视角的 ou_ef49d5e9… 不是同一个串（c16 实测）。
MO_OPEN_ID = "ou_daffa42fee5a30596dd3e5393b527e5e"
TZ = timezone(timedelta(hours=8))
BASE = "https://open.feishu.cn"

TOOLS_DIR = Path(__file__).resolve().parent          # bot-team/tools
BOT_TEAM = TOOLS_DIR.parent                          # bot-team
BOT2 = BOT_TEAM.parents[1]                           # bot-2
OCTOP_DB = BOT2 / ".octop" / "octop.db"
STATE = BOT_TEAM / ".reply-consume-state.json"
TRACE = BOT2 / ".junshi" / "reply-consume.jsonl"
RUNLOG = BOT2 / ".junshi" / "reply-consume-runs.jsonl"   # 每次运行快照（调度静默失败排查）

sys.path.insert(0, str(BOT_TEAM))
sys.path.insert(0, str(TOOLS_DIR))
from core import telemetry  # noqa: E402
from im_signers import resolve as resolve_im_signer  # noqa: E402

LETTER_RE = re.compile(r"^\s*([abcABC])\s*(?:[.。、:：,，!！]|\s|$)")
ANY_LETTER_RE = re.compile(r"(?<![A-Za-z0-9])[abcABC](?![A-Za-z0-9])")

ACTION = {"a": "archive_confirmed", "b": "transfer_manual", "c": "transfer_analysis"}


def _log(msg):
    try:
        print("[reply-consume] " + str(msg), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ---------- 凭证（只读、不落盘、不打印） ----------

def load_creds():
    """返回 (app_id, app_secret, source)。env 优先，其次 octop.db channels 表。"""
    env_id = os.environ.get("FEISHU_APP_ID")
    env_secret = os.environ.get("FEISHU_APP_SECRET")
    if env_id and env_secret:
        return env_id, env_secret, "env"
    if not OCTOP_DB.is_file():
        raise RuntimeError("找不到 Octop 配置库：%s" % OCTOP_DB)
    con = sqlite3.connect("file:%s?mode=ro" % OCTOP_DB, uri=True)
    try:
        rows = con.execute(
            "SELECT config_json FROM channels WHERE kind='feishu' AND enabled=1").fetchall()
    finally:
        con.close()
    for (cfg,) in rows:
        try:
            j = json.loads(cfg or "{}")
        except Exception:  # noqa: BLE001
            continue
        if j.get("app_id") and j.get("app_secret"):
            return j["app_id"], j["app_secret"], "octop.db"
    raise RuntimeError("octop.db channels 表无可用 feishu 应用凭证（app_id/app_secret）")


def http_json(method, url, token=None, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tenant_token(app_id, app_secret):
    res = http_json("POST", BASE + "/open-apis/auth/v3/tenant_access_token/internal",
                    body={"app_id": app_id, "app_secret": app_secret})
    if res.get("code") != 0:
        raise RuntimeError("换 tenant token 失败 code=%s msg=%s"
                           % (res.get("code"), res.get("msg")))
    return res["tenant_access_token"]


# ---------- 判别 ----------

def text_of(msg):
    """body.content → 纯文本。text 消息是 {"text":"..."}；解析失败则原样返回。"""
    body = (msg.get("body") or {}).get("content") or ""
    try:
        j = json.loads(body)
    except Exception:  # noqa: BLE001
        return body
    if isinstance(j, dict) and isinstance(j.get("text"), str):
        return j["text"]
    return body


def classify(text):
    """返回 (letter|ambiguous|None, 原串)。None＝不是回信。"""
    t = (text or "").strip()
    m = LETTER_RE.match(t)
    if not m:
        return None, t
    letters = {x.lower() for x in ANY_LETTER_RE.findall(t)}
    if len(letters) > 1:
        return "ambiguous", t
    return m.group(1).lower(), t


# ---------- 拉信 ----------

def pull(start_sec, end_sec, token, max_pages=5, timeout=60):
    items, page_token, pages = [], None, 0
    last_has_more = False
    while pages < max_pages:
        q = {"container_id_type": "chat", "container_id": CHAT_ID,
             "start_time": str(start_sec), "end_time": str(end_sec),
             "sort_type": "ByCreateTimeAsc", "page_size": "50"}
        if page_token:
            q["page_token"] = page_token
        url = BASE + "/open-apis/im/v1/messages?" + urllib.parse.urlencode(q)
        res = http_json("GET", url, token, timeout=timeout)
        if res.get("code") != 0:
            raise RuntimeError("拉信失败 code=%s msg=%s" % (res.get("code"), res.get("msg")))
        d = res.get("data") or {}
        items += d.get("items") or []
        last_has_more = bool(d.get("has_more"))
        page_token = d.get("page_token")
        pages += 1
        if not last_has_more or not page_token:
            break
    return items, last_has_more


# ---------- 状态 / 留痕 ----------

def consumed_ids():
    ids = set()
    if not TRACE.exists():
        return ids
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("message_id"):
            ids.add(row["message_id"])
    return ids


def append_trace(row):
    TRACE.parent.mkdir(parents=True, exist_ok=True)
    with TRACE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_state(cursor_ms):
    STATE.write_text(json.dumps({"cursor_ms": str(cursor_ms),
                                 "updated": datetime.now(TZ).isoformat(timespec="seconds")},
                                ensure_ascii=False), encoding="utf-8")


def to_sec(raw_since):
    """--since 支持 ISO 或纯数字（秒）。解析失败返回 None。"""
    s = str(raw_since or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}", s):
        v = int(s)
        return v // 1000 if v > 9999999999 else v
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+", s):
        head, tzs = s.split("+", 1)
        if len(head) == 16:
            head += ":00"
        s = head + "+" + tzs
    try:
        return int(datetime.fromisoformat(s.replace(" ", "T", 1)).timestamp())
    except ValueError:
        return None


def pick_task(root):
    """最近收口的任务（closure.json 最新）——回信归属的兜底，不是首选依据。"""
    best = None
    for clo in root.glob("*/closure.json"):
        try:
            mt = clo.stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, clo.parent)
    return best[1] if best else None


def pushed_tasks():
    """遥测账本里的成功推送 [(推送时刻, task_id)]，升序。

    回信归属的唯一硬依据：Mo 只对「最近一封交付」回信，所以按推送时间切，
    而不是按 closure.json 的 mtime 猜（多任务同日会串号，采纳率直接失真）。
    """
    out = []
    try:
        rows = telemetry.read()
    except Exception:  # noqa: BLE001
        return out
    for row in rows:
        if row.get("name") != "push_sent" or row.get("ok") is not True:
            continue
        task_id = row.get("task_id")
        try:
            ts = datetime.fromisoformat(str(row.get("ts") or ""))
        except ValueError:
            continue
        if task_id and ts.tzinfo:
            out.append((ts, str(task_id)))
    return sorted(out)


def attribute_task(root, reply_dt, pushes, max_age_days=14):
    """把一封回信归到任务上，返回 (task_dir|None, 依据)。

    依据取值：push_index（回信前最近一次成功推送）｜fallback（无推送记录时按最近收口）。
    """
    best = None
    for ts, task_id in pushes:
        if ts <= reply_dt and (reply_dt - ts).total_seconds() <= max_age_days * 86400:
            best = task_id
    if best:
        cand = Path(root) / best
        if cand.is_dir():
            return cand, "push_index"
    return pick_task(Path(root)), "fallback"


def delivery_dt(task_dir):
    """该任务的推送时间（.lark-signed.json 的 sent_at）；取不到返回 None。"""
    if task_dir is None:
        return None
    try:
        j = json.loads((task_dir / ".lark-signed.json").read_text(encoding="utf-8-sig"))
        return datetime.fromisoformat(str(j.get("sent_at") or "")).replace(tzinfo=TZ)
    except Exception:  # noqa: BLE001
        return None


def runlog(row):
    try:
        RUNLOG.parent.mkdir(parents=True, exist_ok=True)
        with RUNLOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def find_gateway():
    env_path = os.environ.get("LARK_SEND")
    cands = [Path(env_path)] if env_path else []
    cands.append(BOT2 / "tools" / "lark_send.py")
    for cand in cands:
        if cand.is_file():
            return cand
    return None


def ack(gateway, markdown, key, dry):
    """回执仍走 4 合 1 网关（白名单＋审计），但用 tenant 身份（调度上下文无 user token）。"""
    if dry or gateway is None:
        _log("dry/无网关，跳过回执：" + markdown.splitlines()[0])
        return False
    cmd = [sys.executable, "-B", str(gateway), "--purpose", "mo_p2p",
           "--markdown", markdown, "--send", "--auth", "tenant",
           "--idempotency-key", key]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    if p.returncode != 0:
        _log("回执失败 rc=%d %s" % (p.returncode, ((p.stdout or "") + (p.stderr or ""))[:200]))
        return False
    return True


# ---------- 动作 ----------

def handle(letter, raw, task_dir, args):
    """机械动作：落任务目录标记；回执文案由本函数生成。语义执行留给会话轮。"""
    action = ACTION.get(letter, "needs_human")
    payload = raw[:400]
    if args.dry:
        task_name = task_dir.name if task_dir else "（未匹配任务）"
        return "[dry] %s → %s ｜ %s" % (action, task_name, payload[:80])
    if task_dir is not None and letter in ACTION:
        fname = "reply-ack.json" if letter == "a" else "pending-reply.json"
        target = task_dir / fname
        rec = {"letter": letter, "action": action, "text": payload,
               "at": datetime.now(TZ).isoformat(timespec="seconds")}
        old = []
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(old, list):
                old = [old]
        except Exception:  # noqa: BLE001
            old = []
        old.append(rec)
        target.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")

    task_name = task_dir.name if task_dir else "（未匹配任务）"
    head = {"a": "**回信已收 · a 确认归档**",
            "b": "**回信已收 · b 转人工**",
            "c": "**回信已收 · c 补充分析**"}.get(letter, "**回信歧义 · 待人工确认**")
    lines = [head, "任务：%s" % task_name, "原文：%s" % payload[:160]]
    if letter == "a":
        lines.append("机械化留痕已落：reply-ack.json + 决策账本 trace（语义归档归会话轮）")
    elif letter in ("b", "c"):
        lines.append("已落 pending-reply.json 转交会话轮处理：%s" %
                     ("生成疑问清单并回推" if letter == "b" else "按注明方向跑增量分析"))
    else:
        lines.append("同一回信出现多个选项字母，脚本不猜。请明确一个字母重发。")
    lines.append("（机械回执，无需再确认）")
    return "\n".join(lines)


# ---------- 机检 ----------

def self_test():
    cases = [
        ("a. 确认归档，T+7 按上述判据回看", "a"),
        ("B 有疑问，转人工沟通", "b"),
        ("c。要求补充分析：补 10-20 后数据", "c"),
        ("c", "c"),
        ("继续 L2-L6", None),
        ("SHA256", None),
        ("军师拍板 G2", None),
        ("执行 G2", None),
        ("B线数据异常", None),
        ("a 还是 b 我拿不准", "ambiguous"),
        ("", None),
        ("  A、确认", "a"),
    ]
    bad = [(t, exp, classify(t)[0]) for t, exp in cases if classify(t)[0] != exp]
    for t, exp, got in bad:
        print("FAIL  expect=%r got=%r  input=%r" % (exp, got, t))

    # 包体解析门：text 消息的 body 是 {"text":"..."}，直接取首字符会永远拿到 '{'
    body_cases = [
        ('{"text":"a. 确认归档"}', "a"),
        ('{"text":"B 有疑问"}', "b"),
        ("not-json", None),
    ]
    for body, exp in body_cases:
        got = classify(text_of({"body": {"content": body}}))[0]
        if got != exp:
            bad.append((body, exp, got))
            print("FAIL  body=%r expect=%r got=%r" % (body, exp, got))

    total = len(cases) + len(body_cases)
    print(json.dumps({"ok": not bad, "cases": total, "failed": len(bad)},
                     ensure_ascii=False))
    return 0 if not bad else 1


# ---------- 主流程 ----------

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="G2 a/b/c 回信消费（调度拉·应用身份）")
    ap.add_argument("--since", default=None, help="起始时间：ISO 或 unix 秒；缺省用游标")
    ap.add_argument("--root", default=str(BOT_TEAM / "runs"))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    now = datetime.now(TZ)

    # 凭证预检（fail-closed）：拿不到应用凭证就明确报错，不再静默空转。
    try:
        app_id, app_secret, cred_src = load_creds()
    except Exception as exc:  # noqa: BLE001
        err = "凭证不可用：%s" % str(exc)[:240]
        runlog({"at": now.isoformat(timespec="seconds"), "rc": 3, "error": err,
                "python": sys.executable, "pid": os.getpid()})
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 3

    start_sec = to_sec(args.since)
    if start_sec is None:
        cur = read_state().get("cursor_ms")
        if cur:
            start_sec = max(0, int(cur) // 1000 - 60)      # 回退 60s 防边界漏信
        else:
            start_sec = int((now - timedelta(hours=1)).timestamp())   # 首跑回看 1 小时
    end_sec = int((now + timedelta(days=1)).timestamp())

    try:
        token = tenant_token(app_id, app_secret)
        msgs, has_more = pull(start_sec, end_sec, token)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:300]
        runlog({"at": now.isoformat(timespec="seconds"), "rc": 2, "since": start_sec,
                "error": err, "cred_source": cred_src, "python": sys.executable})
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 2

    seen = consumed_ids()
    pushes = pushed_tasks()
    gateway = None if args.dry else find_gateway()

    rows, max_ms = [], None
    skipped = {"deleted": 0, "not_mo": 0, "not_text": 0, "dup": 0, "not_reply": 0}
    for m in msgs:
        ct = str(m.get("create_time") or "")
        if ct.isdigit() and (max_ms is None or int(ct) > max_ms):
            max_ms = int(ct)
        if m.get("deleted"):
            skipped["deleted"] += 1
            continue
        sender = m.get("sender") or {}
        # 身份口径：白名单文件（server/im_signers.json）优先，未配置时退回 legacy open_id。
        # 这里只认「有权记账的人」，不是「能看见卡片的人」——两件事别混。
        signer = None
        if sender.get("sender_type") == "user":
            try:
                signer = resolve_im_signer(sender.get("id"), chat_id=m.get("chat_id"))
            except Exception as exc:  # noqa: BLE001 - 白名单坏了必须停，不能放行
                _log("IM 签字白名单不可用，本轮不消费任何回信: %s" % exc)
                return 2
        if not signer:
            skipped["not_mo"] += 1
            continue
        if m.get("msg_type") != "text":
            skipped["not_text"] += 1          # 网关出站一律 post：排除自污染
            continue
        mid = m.get("message_id")
        if not mid or mid in seen:
            skipped["dup"] += 1
            continue
        letter, raw = classify(text_of(m))
        if letter is None:
            skipped["not_reply"] += 1
            continue                      # 非回信：不消费不回复
        # 归属：回信时刻 → 此前最近一次成功推送的任务（push_index）；无推送记录才兜底
        reply_dt = (datetime.fromtimestamp(int(ct) / 1000, TZ)
                    if ct.isdigit() else now)
        task_dir, attr = attribute_task(Path(args.root), reply_dt, pushes)
        row = {"date": now.isoformat(timespec="seconds"), "message_id": mid,
               "create_time": ct, "letter": letter,
               "action": ACTION.get(letter, "needs_human"), "text": raw[:400],
               "task": task_dir.name if task_dir else None, "attr": attr,
               "dry": args.dry}
        if not args.dry:
            append_trace(row)
        markdown = handle(letter, raw, task_dir, args)
        sent = ack(gateway, markdown, "reply-" + mid, args.dry) if not args.dry else False
        row["acked"] = sent
        # 埋点不得中断业务：无 task_id（未匹配到任务）时不猜，宁可不记
        if not args.dry and letter in ACTION and task_dir is not None:
            try:
                props = {"task_id": task_dir.name, "letter": letter, "attr": attr}
                pushed = delivery_dt(task_dir)
                if pushed and ct.isdigit():
                    sec = (datetime.fromtimestamp(int(ct) / 1000, TZ) - pushed).total_seconds()
                    if sec >= 0:
                        props["latency_s"] = round(sec, 1)
                telemetry.emit("reply", props)
            except Exception:  # noqa: BLE001
                pass
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    if not args.dry and max_ms:
        write_state(max_ms)
    last = rows[-1] if rows else {}
    runlog({"at": now.isoformat(timespec="seconds"), "rc": 0, "since": start_sec,
            "pulled": len(msgs), "consumed": len(rows), "has_more": has_more,
            "skipped": skipped, "cred_source": cred_src, "pushes": len(pushes),
            "task": last.get("task"), "attr": last.get("attr"), "dry": args.dry,
            "python": sys.executable})
    print(json.dumps({"ok": True, "pulled": len(msgs), "consumed": len(rows),
                      "has_more": has_more, "since": datetime.fromtimestamp(
                          start_sec, TZ).isoformat(timespec="seconds"),
                      "skipped": skipped, "cred_source": cred_src,
                      "pushes": len(pushes),
                      "task": last.get("task"), "attr": last.get("attr"),
                      "dry": args.dry}, ensure_ascii=False))
    if has_more:
        _log("has_more=true：本轮只消费了首页，游标已推进，下轮续拉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
