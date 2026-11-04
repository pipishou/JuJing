#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R2-E 任务候选器判据测试（stdlib，无需 pytest）。

跑法：python -B tests/test_task_candidates.py
输出末行：RESULT: PASS（PASS x / FAIL y）；退出码 0/1。

覆盖：正向成候选、闲聊/短句/无动词负例、同 dialog 聚合、跨 30 分钟不聚合、
跨 dialog 不聚合、幂等、rejected 不复活、confirm/reject 幂等、id 递增、
坏 JSON 不炸、空事件不炸、CLI 端到端。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import task_candidates as tc  # noqa: E402

PASS, FAIL = [], []
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=TZ)
LONG_REQ = (
    "请帮我评估一下这个方案是否可行，需要做一个风险清单出来，"
    "另外查一下上周的数据有没有异常，为什么会出现这种情况。"
)
GREETING = "，".join(
    ["你好", "谢谢", "辛苦了", "早上好", "晚安", "再见", "拜拜", "收到", "好的", "没问题"] * 2
)
NO_KEYWORD = (
    "今天天气不错，阳光很好，适合出去走走看看风景，心情也变好了呢。"
    "下午可能去公园散步，顺便买点水果回来。"
)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"  FAIL {name} {detail}")


def ev(eid, body, ts, dialog="d1", channel=None, event="bot_team_msg_human", seq=None):
    rec = {
        "event": event,
        "event_id": str(eid),
        "ts": ts,
        "seq": int(seq if seq is not None else eid),
        "payload": {"body": body},
    }
    if dialog:
        rec["dialog_id"] = dialog
    if channel:
        rec["refs"] = {"channel": channel}
    return rec


def ts(minute):
    return (NOW.replace(hour=9, minute=0) + timedelta(minutes=minute)).isoformat()


def raises(fn):
    try:
        fn()
    except tc.CandidateError:
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  （非 CandidateError：{type(exc).__name__}: {exc}）")
        return False
    return False


def main():
    t0 = time.time()
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    env_bak = {k: os.environ.get(k) for k in ("BOT_RUNTIME_DIR", "BOT_EVENTS_FILE")}
    os.environ["BOT_RUNTIME_DIR"] = str(base / "default_runtime")
    try:
        # ---------------- 正向 + 字段 ----------------
        a = base / "root_a"
        events = [ev(1, LONG_REQ, ts(0))]
        cands = tc.build(events, root=a, now=NOW)
        check("长文本+关键词成候选", len(cands) == 1, cands)
        c = cands[0] if cands else {}
        check("schema=task.candidate.v1", c.get("schema") == tc.SCHEMA, c.get("schema"))
        check("id 形如 C-YYYYMMDD-001", c.get("id") == "C-20261104-001", c.get("id"))
        check("title ≤30 字", len(c.get("title") or "") <= 30, len(c.get("title") or ""))
        check("conclusion_hint ≤200 字", len(c.get("conclusion_hint") or "") <= 200)
        check("conclusion_hint 是原文前缀", LONG_REQ.startswith(c.get("conclusion_hint") or "?"))
        check("source_event_ids 正确", c.get("source_event_ids") == ["1"])
        check("chars 等于合并文本长度", c.get("chars") == len(LONG_REQ))
        check("status=pending", c.get("status") == "pending")
        check("is_demo=false", c.get("is_demo") is False)
        check("created_at 用传入 now", str(c.get("created_at", "")).startswith("2026-11-04"))
        check("first_ts/last_ts 落原值", c.get("first_ts") == ts(0) and c.get("last_ts") == ts(0))

        # 落盘 + 幂等
        paths = tc.save(cands, root=a)
        check("save 原子写文件", len(paths) == 1 and Path(paths[0]).is_file())
        check("save 后无 .tmp 残留", not list(Path(paths[0]).parent.glob("*.tmp")))
        on_disk = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        check("落盘 JSON 可读且 id 一致", on_disk.get("id") == c.get("id"))
        check("同 source_event_ids 再 build 跳过", tc.build(events, root=a, now=NOW) == [])
        check("list_pending 返回 1 条", len(tc.list_pending(root=a)) == 1)

        # ---------------- 负例：闲聊 / 短句 / 无动词 ----------------
        b = base / "root_b"
        check("GREETINGS 判定纯寒暄", tc.is_greeting(GREETING) is True, GREETING[:20])
        check("带请求的问候不算寒暄", tc.is_greeting("你好，帮我看看这个方案") is False)
        check("纯寒暄不成候选", tc.build([ev(2, GREETING, ts(0))], root=b, now=NOW) == [],
              len(GREETING))
        check("纯寒暄长度 ≥40 仍不成候选", len(GREETING) >= 40, len(GREETING))
        check("短句不成候选", tc.build([ev(3, "帮我查一下", ts(0))], root=b, now=NOW) == [])
        check("长句无关键词不成候选",
              tc.build([ev(4, NO_KEYWORD, ts(0))], root=b, now=NOW) == [], len(NO_KEYWORD))
        check("bot_team_msg_bot 不成候选",
              tc.build([ev(5, LONG_REQ, ts(0), event="bot_team_msg_bot")], root=b, now=NOW) == [])
        check("无正文事件不成候选",
              tc.build([{"event": "bot_team_msg_human", "event_id": "x", "ts": ts(0),
                         "payload": {}}], root=b, now=NOW) == [])

        # ---------------- 聚合：同 dialog / 跨 30 分钟 / 跨 dialog ----------------
        c1 = base / "root_c"
        first = "先帮我看一下这个季度的销售数据，特别是华东区域"
        second = "有没有异常，为什么同比掉了这么多，需要做个复盘"
        merged = [
            ev(11, first, ts(0), dialog="dA"),
            ev(12, second, ts(10), dialog="dA"),
        ]
        check("两半各自 <40 字", len(first) < 40 and len(second) < 40)
        got = tc.build(merged, root=c1, now=NOW)
        check("同 dialog 10 分钟内聚合成 1 条", len(got) == 1, got)
        if got:
            check("聚合后 source_event_ids 含 2 条", got[0]["source_event_ids"] == ["11", "12"])
            check("聚合后 chars ≥40", got[0]["chars"] >= 40)

        # 刚好 30 分钟仍聚合；31 分钟断开
        edge = tc.build([ev(21, first, ts(0), dialog="dA"),
                         ev(22, second, ts(30), dialog="dA")], root=c1, now=NOW)
        check("间隔=30 分钟仍聚合", len(edge) == 1, edge)
        gap = tc.build([ev(31, first, ts(0), dialog="dA"),
                        ev(32, second, ts(31), dialog="dA")], root=c1, now=NOW)
        check("间隔 31 分钟不聚合（各自不足 40）", gap == [], gap)

        cross = tc.build([ev(41, first, ts(0), dialog="d1"),
                          ev(42, second, ts(5), dialog="d2")], root=c1, now=NOW)
        check("跨 dialog 不聚合", cross == [], cross)

        # 无 dialog 时按 refs.channel 聚合
        ch = tc.build([ev(51, first, ts(0), dialog=None, channel="ch-x"),
                       ev(52, second, ts(5), dialog=None, channel="ch-x")], root=c1, now=NOW)
        check("无 dialog 按 refs.channel 聚合", len(ch) == 1 and ch[0]["channel"] == "ch-x", ch)
        ch2 = tc.build([ev(61, first, ts(0), dialog=None, channel="ch-x"),
                        ev(62, second, ts(5), dialog=None, channel="ch-y")], root=c1, now=NOW)
        check("不同 channel 不聚合", ch2 == [], ch2)

        # ---------------- id 递增 / confirm 幂等 / reject ----------------
        d = base / "root_d"
        d1 = tc.build([ev(71, LONG_REQ + "这是第一件事。", ts(0), dialog="dA")], root=d, now=NOW)
        tc.save(d1, root=d)
        d2 = tc.build([ev(72, LONG_REQ + "这是第二件事。", ts(5), dialog="dB")], root=d, now=NOW)
        tc.save(d2, root=d)
        check("当天 id 递增 001→002",
              [x["id"] for x in d1 + d2] == ["C-20261104-001", "C-20261104-002"],
              [x["id"] for x in d1 + d2])

        first_confirm = tc.confirm(d, d1[0]["id"], "老板", "T-100")
        check("首次 confirm 状态 confirmed", first_confirm.get("status") == "confirmed")
        check("首次 confirm 记录 task_id", first_confirm.get("task_id") == "T-100")
        check("首次 confirm 无 already", not first_confirm.get("already"))
        again = tc.confirm(d, d1[0]["id"], "老板", "T-999")
        check("重复 confirm 返回既有 + already=True", again.get("already") is True)
        check("重复 confirm 不覆盖 task_id", again.get("task_id") == "T-100")
        check("已 confirm 的候选不在 pending",
              all(x["id"] != d1[0]["id"] for x in tc.list_pending(root=d)))
        check("未知 id confirm 抛 CandidateError",
              raises(lambda: tc.confirm(d, "C-20261104-999", "老板", "T-1")))
        check("未知 id reject 抛 CandidateError",
              raises(lambda: tc.reject(d, "C-20261104-999", "老板", "无")))
        check("非法 id 抛 CandidateError", raises(lambda: tc.confirm(d, "../x", "老板", "T-1")))

        rej = tc.reject(d, d2[0]["id"], "老板", "不是任务")
        check("reject 状态 rejected", rej.get("status") == "rejected")
        check("reject 记录原因", rej.get("reject_reason") == "不是任务")
        rej2 = tc.reject(d, d2[0]["id"], "老板", "再说一次")
        check("重复 reject 幂等 + already=True", rej2.get("already") is True)
        check("已 reject 不能 confirm",
              raises(lambda: tc.confirm(d, d2[0]["id"], "老板", "T-1")))
        check("confirm 后不能 reject",
              raises(lambda: tc.reject(d, d1[0]["id"], "老板", "反悔")))

        # ---------------- rejected 不复活 ----------------
        e = base / "root_e"
        evs_e = [ev(81, LONG_REQ, ts(0), dialog="dE")]
        ce = tc.build(evs_e, root=e, now=NOW)
        tc.save(ce, root=e)
        tc.reject(e, ce[0]["id"], "老板", "不做")
        check("rejected 后同事件不再成候选", tc.build(evs_e, root=e, now=NOW) == [])
        keys_file = e / "candidates" / "rejected_keys.json"
        check("rejected_keys.json 已落盘", keys_file.is_file())
        if keys_file.is_file():
            doc = json.loads(keys_file.read_text(encoding="utf-8"))
            check("rejected_keys 含 1 个 key", len(doc.get("keys") or []) == 1, doc)
        check("被 reject 的候选文件仍在（可审计）",
              (e / "candidates" / f"{ce[0]['id']}.json").is_file())

        # ---------------- 坏 JSON 不炸 / 空事件不炸 ----------------
        f = base / "root_f"
        cdir = f / "candidates"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "broken.json").write_text("{not json", encoding="utf-8")
        (cdir / "rejected_keys.json").write_text("{", encoding="utf-8")
        check("坏 JSON 下 list_pending 不炸", tc.list_pending(root=f) == [])
        cf = tc.build([ev(91, LONG_REQ, ts(0), dialog="dF")], root=f, now=NOW)
        check("坏 JSON 下 build 仍可产出", len(cf) == 1, cf)
        tc.save(cf, root=f)
        check("坏 JSON 下 save/list 可读", len(tc.list_pending(root=f)) == 1)
        check("空事件列表不炸", tc.build([], root=f, now=NOW) == [])
        check("None 事件不炸", tc.build(None, root=f, now=NOW) == [])

        # ---------------- CLI 端到端 ----------------
        g = base / "root_g"
        events_file = base / "events.jsonl"
        events_file.write_text(
            json.dumps(ev(101, LONG_REQ, ts(0), dialog="dG"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["BOT_RUNTIME_DIR"] = str(g)
        env["BOT_EVENTS_FILE"] = str(events_file)
        env["PYTHONIOENCODING"] = "utf-8"
        script = str(ROOT / "tools" / "task_candidates.py")
        run = subprocess.run([sys.executable, "-B", script, "--build", "--json"],
                             capture_output=True, text=True, encoding="utf-8", env=env)
        cli_build = json.loads(run.stdout or "{}") if run.returncode == 0 else {}
        check("CLI --build 退出 0", run.returncode == 0, run.stderr[-300:])
        check("CLI --build 产出 1 条", cli_build.get("built") == 1, cli_build)
        run2 = subprocess.run([sys.executable, "-B", script, "--list", "--json"],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        cli_list = json.loads(run2.stdout or "{}") if run2.returncode == 0 else {}
        check("CLI --list 返回 1 条", len(cli_list.get("items") or []) == 1, cli_list)
        cid = (cli_list.get("items") or [{}])[0].get("id")
        run3 = subprocess.run([sys.executable, "-B", script, "--confirm", str(cid),
                               "--by", "老板", "--task-id", "T-777", "--json"],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        cli_confirm = json.loads(run3.stdout or "{}") if run3.returncode == 0 else {}
        check("CLI --confirm 成功", (cli_confirm.get("candidate") or {}).get("status") == "confirmed",
              run3.stdout[-300:] + run3.stderr[-200:])
    finally:
        for key, value in env_bak.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tmp.cleanup()

    total, passed = len(PASS) + len(FAIL), len(PASS)
    if FAIL:
        print(f"RESULT: FAIL（PASS {passed} / FAIL {len(FAIL)}）")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print(f"RESULT: PASS（PASS {passed} / FAIL 0）（{time.time() - t0:.2f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
