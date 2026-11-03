#!/usr/bin/env python3
"""sync_shared 的离线测试（stdlib 自跑，不依赖 pytest）。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sync_shared as ss  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def make_fixture(tmp):
    runs = Path(tmp) / "runs"
    ad = runs / "T-20261103-001" / "artifacts"
    ad.mkdir(parents=True)
    (ad / "manifest.json").write_text(json.dumps({
        "schema": "artifact.manifest.v1",
        "task_id": "T-20261103-001",
        "generated_at": "2026-11-03T02:00:00+00:00",
        "artifacts": [
            {"name": "report.md", "type": "md", "size": 120, "sha256": "a" * 64, "is_demo": False},
            {"name": "card.json", "type": "json", "size": 340, "sha256": "b" * 64, "is_demo": True},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    ev = Path(tmp) / "events.jsonl"
    lines = [
        {"seq": 1, "ts": "2026-11-03T01:00:00+00:00", "event": "bot_team_card_issued",
         "refs": {"task_id": "T-20261103-001"},
         "payload": {"card_kind": "attribution", "verdict": "output", "verdict_label": "可直出", "api_key": "sk-xxx"}},
        {"seq": 2, "ts": "2026-11-03T01:05:00+00:00", "event": "bot_team_card_signed",
         "refs": {"task_id": "T-20261103-001"}, "payload": {"signer": "老板"}},
        {"seq": 3, "ts": "2026-11-03T01:06:00+00:00", "event": "bot_team_artifact_saved",
         "refs": {"task_id": "T-20261103-001"}, "payload": {"name": "report.md"}},
        {"seq": 4, "ts": "2026-11-03T01:07:00+00:00", "event": "bot_team_outcome_recorded",
         "refs": {"task_id": "T-20261103-001"}, "payload": {"observed_outcome": "hit"}},
        {"seq": 5, "ts": "2026-11-03T01:08:00+00:00", "event": "bot_team_attention_parked", "payload": {}},
        {"seq": 6, "ts": "2026-11-03T01:09:00+00:00", "event": "other_prefix", "payload": {}},
    ]
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)
    text += "{not json}\n\n"
    ev.write_text(text, encoding="utf-8")
    return runs, ev


def main():
    with tempfile.TemporaryDirectory() as tmp:
        runs, ev = make_fixture(tmp)
        doc = ss.build(runs, ev)
        check("counts.artifacts=2", doc["counts"]["artifacts"] == 2, doc["counts"])
        check("counts.timeline=4（非白名单/坏行被滤）", doc["counts"]["timeline"] == 4, doc["counts"])
        check("出卡=1", doc["counts"]["cards_issued"] == 1)
        check("签字=1", doc["counts"]["cards_signed"] == 1)
        check("回填=1", doc["counts"]["outcomes"] == 1)
        check("事件名去前缀", doc["timeline"][0]["event"] == "card_issued")
        check("按时间顺序保留", doc["timeline"][0]["seq"] == 1 and doc["timeline"][-1]["seq"] == 4)
        check("凭据字段被丢", "api_key" not in doc["timeline"][0]["payload"], doc["timeline"][0]["payload"])
        check("产物带 sha256", doc["artifacts"][0]["sha256"] == "a" * 64)
        check("is_demo 透传", doc["artifacts"][1]["is_demo"] is True)
        check("task_id 从 manifest 取", doc["artifacts"][0]["task_id"] == "T-20261103-001")

        shared = Path(tmp) / "_shared"
        res = ss.write(shared, doc)
        check("写 4 个文件", len(res["written"]) == 4, res["written"])
        check("artifacts-index 有 2 行", len((shared / "artifacts-index.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 2)
        check("timeline 有 4 行", len((shared / "timeline.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 4)
        state = (shared / "PROJECT_STATE.md").read_text(encoding="utf-8")
        check("STATE 含任务号", "T-20261103-001" in state)
        check("STATE 含数字表", "| 出卡 | 1 |" in state)
        check("STATE 含只读声明", "只读" in state)
        check("README 生成", (shared / "README.md").is_file())

        # 二次写：README 不覆盖（存在即跳过）
        (shared / "README.md").write_text("HAND-EDIT", encoding="utf-8")
        res2 = ss.write(shared, doc)
        check("README 不覆盖", (shared / "README.md").read_text(encoding="utf-8") == "HAND-EDIT")
        check("二次写仍成功", res2["ok"] if "ok" in res2 else True)

        # 空输入不炸
        empty = ss.build(Path(tmp) / "nope", Path(tmp) / "nope.jsonl")
        check("空输入不炸", empty["counts"]["artifacts"] == 0 and empty["counts"]["timeline"] == 0)
        st_empty = ss.render_state(empty)
        check("空输入 STATE 有占位", "（暂无事件）" in st_empty)

        # CLI dry-run 不写
        before = set(p.name for p in shared.iterdir())
        rc = ss.main(["--runs", str(runs), "--events", str(ev), "--shared-dir", str(Path(tmp) / "dry"), "--dry-run"])
        check("dry-run rc=0", rc == 0)
        check("dry-run 不建目录", not (Path(tmp) / "dry").exists())

        # 大对象截断
        big = {"payload": {"note": "x" * 2000}}
        check("长字符串截断 500", len(ss._clean(big)["payload"]["note"]) <= 501)

    print(f"\nRESULT: {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 项通过 / {FAIL} 项失败）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
