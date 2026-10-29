"""M0 门：事项账本必须真的在（不是「应该有」）。

二次审查打回的第一条：方案假设「事项账本已有」，实测 `revision 0 / total 0`。
本测试把 M0 变成可跑的断言：

  正控：真实账本存在且 total >= MIN_ITEMS → PASS
  负控：空账本 / 不存在 / total 不足 → FAIL（必须红，红了才算门有效）

用法：
  python -B tests/test_ledger_present.py          # 拿真实账本跑
  python -B tests/test_ledger_present.py --file X # 指定账本
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIN_ITEMS = 40


def default_ledger() -> pathlib.Path:
    import os
    env = os.environ.get("BOT_WORKITEMS_FILE")
    return pathlib.Path(env) if env else ROOT / "runtime" / "workitems" / "current.json"


def check(path: pathlib.Path, min_items: int = MIN_ITEMS) -> dict:
    if not path.is_file():
        return {"ok": False, "reason": f"账本不存在：{path}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"账本不是合法 JSON：{type(exc).__name__}: {exc}"}
    items = data.get("items") or []
    total = len(items)
    if total < min_items:
        return {"ok": False, "reason": f"事项只有 {total} 条，M0 要求 ≥{min_items}", "total": total,
                "revision": data.get("revision")}
    source = data.get("source") or {}
    return {
        "ok": True,
        "total": total,
        "revision": data.get("revision"),
        "source_file": source.get("file"),
        "source_sha256": (source.get("sha256") or "")[:12],
        "imported_at": source.get("imported_at"),
    }


def negative_controls() -> list[tuple[str, bool, str]]:
    """三条负控：空账本、缺文件、条数不足，都必须判红。"""
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)

        missing = check(d / "nope.json")
        results.append(("缺文件", not missing["ok"], missing.get("reason", "")))

        empty = d / "empty.json"
        empty.write_text(json.dumps({"revision": 0, "source": {"kind": "empty"}, "items": []}), encoding="utf-8")
        r = check(empty)
        results.append(("空账本", not r["ok"], r.get("reason", "")))

        few = d / "few.json"
        few.write_text(json.dumps({"revision": 1, "items": [{"id": f"WI-{i:04d}"} for i in range(5)]}), encoding="utf-8")
        r = check(few)
        results.append(("条数不足(5)", not r["ok"], r.get("reason", "")))

        broken = d / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        r = check(broken)
        results.append(("坏 JSON", not r["ok"], r.get("reason", "")))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M0：事项账本存在性门")
    ap.add_argument("--file", help="账本路径（默认 env BOT_WORKITEMS_FILE 或 runtime/workitems/current.json）")
    ap.add_argument("--min", type=int, default=MIN_ITEMS, help=f"最少事项数（默认 {MIN_ITEMS}）")
    ap.add_argument("--no-negative", action="store_true", help="跳过负控（不建议）")
    args = ap.parse_args(argv)

    path = pathlib.Path(args.file) if args.file else default_ledger()
    pos = check(path, args.min)
    print(json.dumps({"positive": pos}, ensure_ascii=False, indent=2))

    if not args.no_negative:
        negs = negative_controls()
        print(json.dumps({"negative_controls": [{"case": c, "red_as_expected": ok, "detail": d} for c, ok, d in negs]},
                         ensure_ascii=False, indent=2))
        if not all(ok for _, ok, _ in negs):
            print("RESULT: FAIL（负控没变红 = 门是假的）")
            return 1

    if not pos["ok"]:
        print("RESULT: FAIL（M0 未通过）")
        return 1
    print(f"RESULT: PASS（M0 账本 {pos['total']} 条，rev {pos['revision']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
