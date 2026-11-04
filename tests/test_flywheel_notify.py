#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""flywheel_tick 的失败告警（发飞书）离线测试。

不联网：用一个假的 lark_send 脚本替代真发送，只验证「命令行构造 + 开关 + 文案」。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.flywheel_tick as tick  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


FAKE = '''# -*- coding: utf-8 -*-
import sys, json, pathlib
args = sys.argv[1:]
pathlib.Path(sys.argv[0]).with_suffix(".called").write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"code": 0, "argv": args}, ensure_ascii=False))
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="notify-"))
    fake = tmp / "lark_send.py"
    fake.write_text(FAKE, encoding="utf-8")
    called = fake.with_suffix(".called")

    # 1. 文案：标题带槽位与次数、失败步逐条、通过计数
    res = {"ok": False, "duration_s": 2.5, "finished_at": "2026-11-04T09:00:00+00:00",
           "steps": [{"step": "collect", "ok": False, "error": "boom"},
                     {"step": "weekly", "ok": True}, {"step": "wal", "ok": False}]}
    text = tick.compose_failure_text(res, slot="2026-11-04@17:00", attempts=2, max_attempts=3)
    check("标题含槽位", "2026-11-04@17:00" in text, text[:60])
    check("标题含次数", "第 2/3 次" in text, text[:60])
    check("列出失败步", "`collect`" in text and "`wal`" in text)
    check("不列成功步", "`weekly`" not in text)
    check("含通过计数", "通过 1/3 步" in text, text)
    check("含处理建议", "flywheel_tick.py" in text)

    # 2. max_attempts=0（手工跑）时不显示次数
    t2 = tick.compose_failure_text(res, slot="", max_attempts=0)
    check("手工跑不显示次数", "第 " not in t2, t2[:40])

    # 3. 无失败步时的兜底文案
    t3 = tick.compose_failure_text({"ok": False, "steps": [{"step": "collect", "ok": True}]}, slot="")
    check("无失败步有兜底", "未取到错误文本" in t3, t3)

    # 4. 开关：BOT_FLYWHEEL_NOTIFY=0 直接不发
    old_env = {k: os.environ.get(k) for k in ("BOT_FLYWHEEL_NOTIFY", "BOT_LARK_SEND")}
    os.environ["BOT_FLYWHEEL_NOTIFY"] = "0"
    os.environ["BOT_LARK_SEND"] = str(fake)
    r = tick.notify_failure(res)
    check("关闭开关不发", r == {"sent": False, "reason": "disabled_by_env"}, str(r))

    # 5. 找不到脚本时不炸
    os.environ["BOT_FLYWHEEL_NOTIFY"] = "1"
    os.environ["BOT_LARK_SEND"] = str(tmp / "nope.py")
    r = tick.notify_failure(res)
    check("脚本缺失不炸", r["sent"] is False and r["reason"] == "lark_send_not_found", str(r))

    # 6. dry-run：调用脚本但不带 --send，且 sent=False
    os.environ["BOT_LARK_SEND"] = str(fake)
    if called.exists():
        called.unlink()
    r = tick.notify_failure(res, slot="s", dry_run=True)
    check("dry-run 未 sent", r["sent"] is False and r["dry_run"] is True, str(r))
    argv = called.read_text(encoding="utf-8") if called.exists() else ""
    check("dry-run 不带 --send", "--send" not in argv, argv[:120])
    check("带 purpose", "mo_p2p" in argv, argv[:120])
    check("带 auth tenant", "tenant" in argv, argv[:120])

    # 7. 真发路径：带 --send，sent=True
    if called.exists():
        called.unlink()
    r = tick.notify_failure(res, slot="s")
    check("真发 sent=True", r["sent"] is True and r["rc"] == 0, str(r))
    argv = called.read_text(encoding="utf-8") if called.exists() else ""
    check("真发带 --send", "--send" in argv, argv[:160])
    check("文案随参数传入", "飞轮失败" in argv, argv[:200])

    # 8. 脚本崩了也不抛（rc≠0 → sent=False）
    bad = tmp / "bad.py"
    bad.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    os.environ["BOT_LARK_SEND"] = str(bad)
    r = tick.notify_failure(res)
    check("子进程失败不抛", r["sent"] is False and r["rc"] == 3, str(r))

    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    print(f"\nRESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
