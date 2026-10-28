#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日常接单器机检：只验「一句话 → 单」这一段，不依赖服务。

跑法：python -B tests/test_daily_intake.py     （零依赖，rc=0 全绿）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "daily_intake.py"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def load_tool():
    spec = importlib.util.spec_from_file_location("daily_intake", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def ns(**kw) -> argparse.Namespace:
    base = dict(
        plant="塑胶厂", line=None, metric=None, metric_id=None, start=None, end=None,
        days=7, baseline_days=30, baseline_value=None, target=None, data=None,
        data_owner="数据负责人（待填）", line_owner="产线负责人（待填）", confounder=None,
        runner="external", notes=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def main() -> int:
    tool = load_tool()

    # 1. 抽产线
    h = tool.parse_text("B线补料率这周 7.2%，怀疑模具磨损")
    check("抽到产线 B", h.get("line") == "B", str(h.get("line")))

    # 2. 抽指标 + pack 口径
    check("指标=补料率", h.get("metric") == "补料率", str(h.get("metric")))
    check("metric_id=refill_rate", h.get("metric_id") == "refill_rate", str(h.get("metric_id")))

    # 3. 口述现值不冒充基线
    check("reported_value=7.2", h.get("reported_value") == 7.2, str(h.get("reported_value")))
    intake, _tables = tool.build_intake("B线补料率这周 7.2%，怀疑模具磨损", ns())
    check("不写 baseline_value", "baseline_value" not in intake, str(intake.get("baseline_value")))
    check("口述值进 assumptions",
          any("7.2" in a for a in intake["intake"]["assumptions"]),
          json.dumps(intake["intake"]["assumptions"], ensure_ascii=False))

    # 4. 嫌疑因素映射成业务语言
    check("模具→模具状态", "模具状态" in intake.get("known_confounders", []),
          json.dumps(intake.get("known_confounders"), ensure_ascii=False))

    # 5. 窗口默认：最近 7 天（截止昨天），基线为窗口前 30 天
    w, b = intake["window"], intake["baseline_window"]
    check("window days=7", w["days"] == 7, str(w))
    check("baseline 在前", b["end"] < w["start"], f"{b['end']} vs {w['start']}")
    check("baseline days=30", b["days"] == 30, str(b["days"]))

    # 6. 原话必须留档（可追溯）
    check("原话留档", intake["intake"]["raw_text"].startswith("B线补料率"), intake["intake"]["raw_text"][:20])

    # 7. strict：缺指标必须拒
    quiet, _qt = tool.build_intake("今天车间有点乱", ns())
    check("strict 拒缺指标", bool(tool.strict_errors(quiet)), str(tool.strict_errors(quiet)))
    check("不 strict 也建单（标假设）",
          any("未识别" in a for a in quiet["intake"]["assumptions"]),
          json.dumps(quiet["intake"]["assumptions"], ensure_ascii=False))

    # 8. 未登记口径不冒充 metric_id
    odd, _ot = tool.build_intake("A线换模次数超标", ns(line="A", metric="换模次数"))
    check("未登记口径 metric_id 空", odd.get("metric_id") is None, str(odd.get("metric_id")))

    # 9. CLI dry-run 可跑且是合法 JSON
    proc = subprocess.run(
        [sys.executable, "-B", str(TOOL), "--text", "B线废品率 3.4%，怀疑原料批次", "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    check("dry-run rc=0", proc.returncode == 0, proc.stderr[-200:])
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        payload = {}
        check("dry-run 输出 JSON", False, str(exc))
    check("dry-run 输出 JSON", bool(payload), proc.stdout[:120])
    check("dry-run 抽到废品率", payload.get("metric") == "废品率", str(payload.get("metric")))
    check("dry-run metric_id=scrap_rate", payload.get("metric_id") == "scrap_rate", str(payload.get("metric_id")))

    # 10. strict CLI 拒单 rc=2
    proc2 = subprocess.run(
        [sys.executable, "-B", str(TOOL), "--text", "今天车间有点乱", "--strict", "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    check("strict CLI rc=2", proc2.returncode == 2, f"rc={proc2.returncode}")

    # 11. 上传体积：8MB 上限前置（本地先挡，不浪费一次请求）
    big = ROOT / "_tmp_daily_intake_big.bin"
    try:
        big.write_bytes(b"0" * (8 * 1024 * 1024 + 1))
        raised = False
        try:
            tool.build_intake("B线补料率 6%", ns(data=[str(big)]))
        except SystemExit:
            raised = True
        check("超 8MB 文件被挡", raised, "未抛 SystemExit")
    finally:
        big.unlink(missing_ok=True)

    # 12. 表格走 /data 而不是建单（CSV 内联必被服务端当 JSON 拒）
    csv_path = ROOT / "_tmp_daily_intake_rows.csv"
    json_path = ROOT / "_tmp_daily_intake_rows.json"
    try:
        csv_path.write_text("date,line,refill_rate" + chr(10) + "2026-10-20,B,5.4" + chr(10), encoding="utf-8")
        json_path.write_text('{"rows": []}', encoding="utf-8")
        mixed, tables = tool.build_intake("B线补料率 7.2%", ns(data=[str(csv_path), str(json_path)]))
        check("CSV 归到 tables", [q.name for q in tables] == [csv_path.name], str([q.name for q in tables]))
        check("CSV 不进 data_files",
              all("csv" not in str(f.get("name", "")) for f in mixed.get("data_files", [])),
              str(mixed.get("data_files")))
        check("JSON 内联进 data_files",
              [f.get("name") for f in mixed.get("data_files", [])] == [json_path.name],
              str(mixed.get("data_files")))
        check("pending_tables 留痕",
              mixed["intake"].get("pending_tables") == [csv_path.name],
              str(mixed["intake"].get("pending_tables")))
    finally:
        csv_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    for name, ok, detail in CHECKS:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail}"))
    print("-" * 60)
    print(f"{passed}/{len(CHECKS)} 通过")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
