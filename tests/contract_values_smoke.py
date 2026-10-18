#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""值级数据合同冒烟：键齐但值错必须被拒。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="bot-contract-"))


def run(cmd):
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    demo = ROOT / "demo" / "material_refill_T.json"
    p = run([PY, "tools/check_data_contract.py", "--t", str(demo), "--strict"])
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr, file=sys.stderr)
        raise SystemExit(1)

    src = json.loads(demo.read_text(encoding="utf-8"))
    recs = src["records"]
    recs[0]["date"] = recs[1]["date"]
    recs[0]["refill_rate"] = 500
    recs[1]["output_volume"] = -1
    bad = TMP / "bad_T.json"
    bad.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    p = run([PY, "tools/check_data_contract.py", "--t", str(bad), "--strict"])
    if p.returncode == 0:
        print("bad data unexpectedly passed", file=sys.stderr)
        print(p.stdout)
        raise SystemExit(1)
    out = json.loads(p.stdout)
    if not out.get("value_errors") or not out.get("duplicate_dates"):
        print("missing value_errors/duplicate_dates", file=sys.stderr)
        print(p.stdout)
        raise SystemExit(1)
    print(json.dumps({"ok": True, "demo": "normal", "bad_exit": p.returncode,
                      "value_errors": len(out["value_errors"]), "duplicate_dates": out["duplicate_dates"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
