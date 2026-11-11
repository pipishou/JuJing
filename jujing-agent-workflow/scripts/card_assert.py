#!/usr/bin/env python3
"""契约门：卡片断言数 ≥ params.json#team.card_assert_min（team 节从「仅文档引用」转为可执行）。

用法：
  python scripts/card_assert.py <report.json> [--min N] [--root <skill根>]
退出码：0 达标；1 不足/参数错；2 文件/字段缺失（fail-closed）。
"""
import argparse
import json
import sys
from pathlib import Path


def load_min(root: Path) -> int:
    conf = json.loads((root / "assets" / "params.json").read_text(encoding="utf-8-sig"))
    team = conf.get("team") or {}
    if "card_assert_min" not in team:
        raise KeyError("params.json#team.card_assert_min 缺失（fail-closed）")
    return int(team["card_assert_min"])


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--min", type=int, default=None)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    a = ap.parse_args(argv)
    root = Path(a.root)
    try:
        doc = json.loads(Path(a.report).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(f"[card_assert] 读报告失败（fail-closed）：{exc}")
        return 2
    try:
        need = a.min if a.min is not None else load_min(root)
    except Exception as exc:  # noqa: BLE001
        print(f"[card_assert] 门槛读取失败（fail-closed）：{exc}")
        return 2
    if need <= 0:
        print(f"[card_assert] 门槛非法 {need}（必须 >0，fail-closed）")
        return 2
    val = doc.get("card_assert")
    if val is None and isinstance(doc.get("assertions"), list):
        val = len(doc["assertions"])
    if val is None:
        print("[card_assert] 缺 card_assert/assertions 字段（fail-closed）")
        return 2
    try:
        got = int(val)
    except (TypeError, ValueError):
        print(f"[card_assert] card_assert 非整数：{val!r}")
        return 2
    if got < need:
        print(f"[card_assert] FAIL 断言数 {got} < 门 {need}（params.json#team.card_assert_min）")
        return 1
    print(f"[card_assert] PASS 断言数 {got} ≥ 门 {need}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
