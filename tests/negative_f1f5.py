# -*- coding: utf-8 -*-
"""F1–F5 负测试：每个补丁都必须能拦住对应的坏形状（拦不住=补丁无效）。
退出码 0=全拦；1=有漏网。
用法：python tests/negative_f1f5.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import gates  # noqa: E402

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, ok, detail))


# T1 代数占位（F4）
case("F4 2.1X 判占位", gates.is_placeholder_evidence("ROI≈2.1X 元/月"))
case("F4 合法倍数不误伤", not gates.is_placeholder_evidence("约 2.1 倍（基准 X=1,000 元）"))


def base_report(**over):
    r = {
        "is_demo": False,
        "task_id": "T-TEST",
        "pack": {"pack_id": "manufacturing"},
        "finding": [{"factor": "模具磨损", "confidence": 0.55, "evidence": ["e1"]}],
        "cannot_attribute": ["缺 MES 版本"],
        "scorecard": {"total": 46.67, "verdict": "force_manual_review"},
        "recommendation": [{"action": "调整排班", "irreversibility": "irreversible",
                            "roi": {"cost": "100", "avoided": "200", "anchor": "mold_change"}}],
        "data_refs": [],
        "decision_log_entry": {},
    }
    r.update(over)
    return r


# T2 不可逆错挂（F2）：B2 换模不在 pack.irreversible_actions
errs = gates.real_report_errors(base_report(), ROOT)
case("F2 不可逆错挂被拦", any("不可逆标注错挂" in e for e in errs), str(errs)[:200])

# T3 纯推理档走 real 门（F5）
errs = gates.real_report_errors(base_report(evidence_tier="inference_only"), ROOT)
case("F5 inference_only 拦 real 门", any("evidence_tier" in e for e in errs), str(errs)[:200])

# T4 inference_only 置信度越级（K-03）
errs = gates.real_report_errors(base_report(evidence_tier="inference_only"), ROOT)
case("K-03 置信度上限 0.40", any("上限 0.40" in e for e in errs), str(errs)[:200])

# T5 CLI 契约字段（F1）：缺 verdict_one_line/sign_scope 必须 FAIL
bad = base_report(evidence_tier="measured")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "bad.json"
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    cp = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "tests" / "validate_report.py"),
                         str(p), "--require-real", "--base", str(ROOT)],
                        capture_output=True)
    out = (cp.stdout + cp.stderr).decode("utf-8", "replace")
    case("F1 缺裁决字段 CLI FAIL", cp.returncode != 0 and "verdict_one_line" in out,
         out[-300:])

# T6 安全门绑动作（F2）：有 gate 没 action → 拦
_gate_bad = base_report(evidence_tier="measured")
_gate_bad["security_gate"] = {"status": "pending_user_confirmation"}
errs = gates.real_report_errors(_gate_bad, ROOT)
case("F2 gate 缺 action 被拦", any("缺 action" in e for e in errs), str(errs)[:200])

bad_names = [n for n, ok, _ in CASES if not ok]
for n, ok, d in CASES:
    print(("[OK]   " if ok else "[FAIL] ") + n + ("" if ok else "  ← " + d))
print(f"negative_f1f5: {len(CASES) - len(bad_names)}/{len(CASES)} 拦住")
sys.exit(1 if bad_names else 0)
