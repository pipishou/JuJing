# -*- coding: utf-8 -*-
"""报告封存：artifact_sha256 = 去掉封存字段后的规范化内容哈希。

用途（判例 F10/K-07）：手改 report.json 会让哈希失配；validate/finalize 据此拒绝交付。
"""
import hashlib
import json
from datetime import datetime, timezone

SEAL_FIELDS = ("artifact_sha256", "seal")


def canonical(report):
    body = {k: v for k, v in (report or {}).items() if k not in SEAL_FIELDS}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute(report):
    return hashlib.sha256(canonical(report).encode("utf-8")).hexdigest()


def seal(report, tool, note="", by=None, command=None):
    report["artifact_sha256"] = compute(report)
    report["seal"] = {
        "tool": tool,
        "by": by or "",
        "command": command or "",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    }
    return report["artifact_sha256"]


def verify(report):
    got = report.get("artifact_sha256")
    if not got:
        return False, "缺 artifact_sha256（报告未经封存，视为手改件）"
    if compute(report) != got:
        return False, "artifact_sha256 失配（报告内容被改动）"
    return True, ""
