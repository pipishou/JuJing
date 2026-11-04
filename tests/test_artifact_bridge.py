#!/usr/bin/env python3
"""artifact_bridge 与候选聚合（一条一候选）的离线自检。"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import artifact_bridge as AB   # noqa: E402
import task_candidates as TC   # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def tmpdir(name):
    d = Path("D:/tmp") / f"ab_{name}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def main():
    base = tmpdir("bridge")
    src = base / "deliverables"
    src.mkdir()
    (src / "00_先读我.md").write_text("说明件，不该回交", encoding="utf-8")
    (src / "决策简报.md").write_text("# 简报\n要点一", encoding="utf-8")
    (src / "可行性评估.md").write_text("# 评估\n结论", encoding="utf-8")

    files = AB.scan(src)
    check("scan 跳过说明件", [f["name"] for f in files] == ["可行性评估.md", "决策简报.md"] or
          sorted(f["name"] for f in files) == sorted(["决策简报.md", "可行性评估.md"]), str(files))

    out = AB.bridge(src, by="工作助理", runs_root=base / "runs",
                    state=base / "state.json", runtime=base / "rt")
    check("建单成功", out["ok"] and out["created"] and out["task_id"].startswith("T-"), str(out))
    tdir = base / "runs" / out["task_id"]
    check("deliverable 落盘", (tdir / "deliverable.md").is_file())
    names = sorted(x.name for x in (tdir / "artifacts").iterdir() if x.name != "manifest.json")
    check("产物复制进 artifacts", names == ["决策简报.md", "可行性评估.md"], str(names))
    check("manifest 计数", out["manifest"]["count"] == 2, str(out["manifest"]))
    check("状态落盘", (base / "state.json").is_file())

    again = AB.bridge(src, runs_root=base / "runs", state=base / "state.json", runtime=base / "rt")
    check("幂等：无新文件不重复建单", again.get("created") is False, str(again))

    (src / "新交付.md").write_text("# 新\n内容", encoding="utf-8")
    third = AB.bridge(src, runs_root=base / "runs", state=base / "state.json", runtime=base / "rt")
    check("新增文件触发新单", third.get("created") is True and third["task_id"] != out["task_id"])

    try:
        AB.scan(base / "nope")
        check("缺目录报错", False)
    except AB.BridgeError:
        check("缺目录报错", True)

    # 候选：一条消息一候选
    events = [
        {"event": "bot_team_msg_human", "ts": "2026-11-04T10:00:00+08:00", "seq": 1,
         "payload": {"body": "你好" * 30}, "dialog_id": "d1"},
        {"event": "bot_team_msg_human", "ts": "2026-11-04T10:01:00+08:00", "seq": 2,
         "payload": {"body": "帮我把企微+飞书的询价副驾做完，出决策简报"}, "dialog_id": "d1"},
        {"event": "bot_team_msg_human", "ts": "2026-11-04T10:02:00+08:00", "seq": 3,
         "payload": {"body": "另外要查一下交付物清单并出一版表格"}, "dialog_id": "d1"},
    ]
    cands = TC.build(events, min_chars=10, root=base / "cand")
    check("一条消息一候选（不合并）", len(cands) == 2, f"got {len(cands)}")
    check("标题不是寒暄", all("你好" not in c["title"] for c in cands), str([c["title"] for c in cands]))
    check("同源事件不重复", len({tuple(c["source_event_ids"]) for c in cands}) == len(cands))

    print(f"\nRESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
