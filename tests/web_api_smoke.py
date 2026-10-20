#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 工作台冒烟：HTTP API 建任务 → 上传 CSV/工作流 → run_task → 签字 → 状态守卫。

不依赖 Flask/FastAPI；只用 stdlib + pandas。运行：
  python tests/web_api_smoke.py
"""
import datetime as dt
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import task_server  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="bot-web-"))


def build_fixture():
    start = dt.date(2026, 9, 5)
    rows = []
    for i in range(37):
        d = start + dt.timedelta(days=i)
        rows.append({
            "日期": d.isoformat(), "产线": "B", "补料率": round(5.0 + 0.06 * i, 2), "产量": 1000 + i,
            "模具状态": "normal", "保养记录": "[]", "班组": "甲班", "原料批次": "RM-2026",
            "环境温湿度": "{}", "换模记录": "[]", "停机记录": "[]", "信息员": "李强",
        })
    csv = TMP / "task.csv"
    pd.DataFrame(rows).to_csv(csv, index=False, encoding="utf-8-sig")
    result = {
        "goal": "合成：B线补料率异常 Web 冒烟",
        "attribution": {
            "finding": [{"factor": "模具状态", "confidence": 0.63, "evidence": ["合成数据证据"]}],
            "cannot_attribute": ["信息员换人"],
            "scorecard": {"data_completeness": 4, "timeliness": 4, "significance": 5,
                          "exclusivity": 3, "history": 3, "human_ai_consistency": 0},
        },
        "advice": {"recommendation": [{"action": "补保养",
                                        "roi": {"cost": 500, "avoided": 2000, "anchor": "保养<换模+停机"},
                                        "irreversibility": "reversible"}]},
    }
    wf = TMP / "workflow-result.json"
    wf.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return csv, wf


def multipart(fields):
    boundary = "----botteam" + "7f3a"
    parts = []
    for name, filename, content in fields:
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append((f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n').encode())
        parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(("--" + boundary + "--\r\n").encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def req(base, method, path, body=None, headers=None, expect=None):
    data = body
    h = dict(headers or {})
    if isinstance(body, dict):
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        payload = raw.decode("utf-8", "replace")
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {code}: {payload}")
    return code, payload


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    csv, wf = build_fixture()
    runs = TMP / "runs"
    httpd = task_server.make_server(port=0, runs_dir=runs)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        req(base, "GET", "/api/health", expect=200)
        code, created = req(base, "POST", "/api/tasks", {
            "id": "T-WEB-SMOKE", "line": "B",
            "t_start": "2026-10-05", "t_end": "2026-10-11",
            "base_start": "2026-09-05", "base_end": "2026-10-04",
            "context": "B线补料率7日 5.1%→7.2%", "expected": "T+7 ≤5.40%",
            "owner": "smoke", "signer": "smoke",
        }, expect=201)
        assert created["task"]["id"] == "T-WEB-SMOKE"
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/run", expect=400)  # 没文件
        body, ctype = multipart([
            ("csv", "task.csv", csv.read_bytes()),
            ("field_map", "field-map.yaml", (ROOT / "templates/field-map.example.yaml").read_bytes()),
            ("workflow_result", "workflow-result.json", wf.read_bytes()),
        ])
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/files", body, {"Content-Type": ctype}, expect=200)
        code, run = req(base, "POST", "/api/tasks/T-WEB-SMOKE/run", expect=200)
        assert run["report"]["is_demo"] is False
        task = req(base, "GET", "/api/tasks/T-WEB-SMOKE", expect=200)[1]["task"]
        assert task["status"] == "ready"
        # 待签状态不能换文件/重跑
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/files", body, {"Content-Type": ctype}, expect=409)
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/run", expect=409)
        # 签字 → approved
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/sign", {"signer": "smoke"}, expect=200)
        task = req(base, "GET", "/api/tasks/T-WEB-SMOKE", expect=200)[1]["task"]
        assert task["status"] == "approved", task
        assert task["decision_status"] == "approved", task
        # 已签不能重复签/重跑/换文件
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/sign", {"signer": "smoke"}, expect=409)
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/run", expect=409)
        req(base, "POST", "/api/tasks/T-WEB-SMOKE/files", body, {"Content-Type": ctype}, expect=409)
        # 报告与文件可读取
        rpt = req(base, "GET", "/api/tasks/T-WEB-SMOKE/report", expect=200)
        assert rpt[1]["report"]["task_id"] == "T-WEB-SMOKE"
        print(json.dumps({"ok": True, "task": task["id"], "status": task["status"],
                          "decision": task["decision_status"], "score": run["report"].get("scorecard", {}).get("total")},
                         ensure_ascii=False))
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
