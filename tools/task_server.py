#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot Team 本地任务工作台（stdlib only）。

用途：把「新建任务 → 上传 CSV/字段映射/归因结果 → 跑真实数据门 → 人工复核签字」收在
一个本地 HTTP 服务里；不替代 DSH workflow，s1–s4 仍由 `orchestration/team-workflow.mjs`
产出 `workflow-result.json`，本服务只做 s5 收口与文件/状态管理。

启动：
  python tools/task_server.py --port 8765
  # 浏览器打开 http://127.0.0.1:8765/workbench

边界：
  - 默认只绑 127.0.0.1；没有鉴权，禁止暴露到公网；
  - 只读写 --runs-dir（默认 bot-team/runs）和 ui/；
  - 上传上限 25MB；只接受 .csv/.xlsx/.xls/.yaml/.yml/.json；
  - 运行 = 调 tools/run_task.py --defer-sign；签字 = sign_delivery + finalize_delivery。
"""
import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
PY = sys.executable
API_VERSION = "1.0"
MAX_BODY = 25 * 1024 * 1024
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALLOWED_UPLOAD_SUFFIX = {".csv", ".xlsx", ".xls", ".yaml", ".yml", ".json"}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_task_id(raw):
    s = (raw or "").strip()
    if not s:
        return None
    if not TASK_ID_RE.match(s) or s in (".", ".."):
        return None
    return s


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def load_task(task_dir):
    return read_json(Path(task_dir) / "task.json")


def save_task(task_dir, data):
    data["updated_at"] = now_iso()
    write_json(Path(task_dir) / "task.json", data)


def task_summary(task_dir, task=None):
    task = task or load_task(task_dir) or {}
    report = read_json(Path(task_dir) / "report.json") or {}
    decision = read_json(Path(task_dir) / "loop" / "decision.json") or {}
    return {
        "id": task.get("id") or Path(task_dir).name,
        "status": task.get("status") or "unknown",
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "line": (task.get("input") or {}).get("line"),
        "context": (task.get("input") or {}).get("context"),
        "has_report": bool(report),
        "is_demo": report.get("is_demo"),
        "finding": len(report.get("finding") or []),
        "cannot_attribute": len(report.get("cannot_attribute") or []),
        "decision_status": decision.get("status") or task.get("decision_status"),
    }


def list_tasks(runs_dir):
    out = []
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return out
    for d in runs_dir.iterdir():
        if not d.is_dir() or not (d / "task.json").exists():
            continue
        try:
            out.append(task_summary(d))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "BotTeamWorkbench/" + API_VERSION
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------
    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, data)

    def _error(self, status, message, detail=None):
        self._json(status, {"ok": False, "error": message, "detail": detail})

    def _runs(self):
        return Path(getattr(self.server, "runs_dir", ROOT / "runs"))

    def _task_dir(self, task_id):
        tid = safe_task_id(task_id)
        if not tid:
            return None
        d = self._runs() / tid
        try:
            d.resolve().relative_to(self._runs().resolve())
        except Exception:
            return None
        return d

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ValueError(f"请求体超过 {MAX_BODY // 1024 // 1024}MB")
        return self.rfile.read(length)

    def _body_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):
        # 本地工作台：只保留错误行，避免每请求刷屏
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(fmt, *args)

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {
                "ok": True, "api_version": API_VERSION, "root": str(ROOT),
                "runs_dir": str(self._runs()), "python": sys.version.split()[0],
                "now": now_iso(), "tasks": len(list_tasks(self._runs())),
            })
        if path == "/api/tasks":
            return self._json(200, {"ok": True, "tasks": list_tasks(self._runs())})
        m = re.match(r"^/api/tasks/([^/]+)(?:/(report|log))?$", path)
        if m:
            d = self._task_dir(m.group(1))
            if not d or not (d / "task.json").exists():
                return self._error(404, "任务不存在")
            what = m.group(2)
            if what == "report":
                report = read_json(d / "report.json")
                if not report:
                    return self._error(404, "报告尚未生成")
                return self._json(200, {"ok": True, "report": report})
            if what == "log":
                log = (d / "run.log").read_text(encoding="utf-8", errors="replace") if (d / "run.log").exists() else ""
                return self._json(200, {"ok": True, "log": log[-20000:]})
            task = load_task(d) or {}
            task["summary"] = task_summary(d, task)
            task["report"] = read_json(d / "report.json")
            task["decision"] = read_json(d / "loop" / "decision.json")
            task["log"] = (d / "run.log").read_text(encoding="utf-8", errors="replace")[-20000:] if (d / "run.log").exists() else ""
            return self._json(200, {"ok": True, "task": task})
        return self._static(path)

    def _static(self, path):
        rel = unquote(path).lstrip("/")
        if rel in ("", "index.html"):
            rel = "index.html"
        elif rel in ("workbench", "workbench.html"):
            rel = "workbench.html"
        if ".." in rel or rel.startswith("/") or "\\" in rel:
            return self._error(400, "非法路径")
        candidate = (UI_DIR / rel).resolve()
        try:
            candidate.relative_to(UI_DIR.resolve())
        except Exception:
            return self._error(400, "非法路径")
        if not candidate.is_file():
            return self._error(404, "文件不存在")
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, candidate.read_bytes(), ctype)

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/tasks":
                return self._create_task()
            m = re.match(r"^/api/tasks/([^/]+)/(files|run|sign)$", path)
            if m:
                d = self._task_dir(m.group(1))
                if not d or not (d / "task.json").exists():
                    return self._error(404, "任务不存在")
                if m.group(2) == "files":
                    return self._upload(d)
                if m.group(2) == "run":
                    return self._run(d)
                return self._sign(d)
            return self._error(404, "未知接口")
        except ValueError as e:
            return self._error(400, str(e))
        except json.JSONDecodeError as e:
            return self._error(400, "JSON 解析失败", str(e))
        except Exception as e:
            return self._error(500, "服务端异常", f"{type(e).__name__}: {e}")

    def _create_task(self):
        body = self._body_json()
        tid = safe_task_id(body.get("id")) or "T-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        d = self._runs() / tid
        if d.exists():
            return self._error(409, f"任务已存在：{tid}")
        d.mkdir(parents=True, exist_ok=False)
        inp = {
            "line": str(body.get("line") or "B").strip(),
            "t_start": str(body.get("t_start") or "").strip(),
            "t_end": str(body.get("t_end") or "").strip(),
            "base_start": str(body.get("base_start") or "").strip(),
            "base_end": str(body.get("base_end") or "").strip(),
            "context": str(body.get("context") or "").strip(),
            "expected": str(body.get("expected") or "").strip(),
            "owner": str(body.get("owner") or "").strip(),
            "signer": str(body.get("signer") or "").strip(),
        }
        missing = [k for k in ("t_start", "t_end", "base_start", "base_end", "expected") if not inp[k]]
        if missing:
            return self._error(400, "缺少必填字段", missing)
        task = {
            "schema": "bot-team-task/1.0",
            "id": tid,
            "status": "created",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "input": inp,
            "files": {},
            "report": None,
            "loop": None,
            "signature": None,
            "last_log": None,
        }
        save_task(d, task)
        return self._json(201, {"ok": True, "task": task_summary(d, task)})

    def _upload(self, d):
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype:
            return self._error(400, "需要 multipart/form-data")
        raw = self._read_body()
        if not raw:
            return self._error(400, "空上传")
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        )
        if not msg.is_multipart():
            return self._error(400, "multipart 解析失败")
        task = load_task(d) or {}
        if task.get("status") in ("ready", "approved"):
            return self._error(409, "报告已生成（待签/已签），不能替换文件；请新建任务重跑")
        files = task.setdefault("files", {})
        saved = []
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if not name or not filename:
                continue
            suffix = Path(filename).suffix.lower()
            if suffix not in ALLOWED_UPLOAD_SUFFIX:
                return self._error(400, f"不支持的文件类型：{filename}")
            payload = part.get_payload(decode=True) or b""
            if len(payload) == 0:
                return self._error(400, f"空文件：{filename}")
            if name == "csv":
                target = d / ("input" + suffix)
                files["csv"] = target.name
            elif name == "field_map":
                target = d / "field-map.yaml"
                files["field_map"] = target.name
            elif name == "workflow_result":
                target = d / "workflow-result.json"
                files["workflow_result"] = target.name
            else:
                continue
            target.write_bytes(payload)
            saved.append({"field": name, "name": target.name, "bytes": len(payload)})
        if not saved:
            return self._error(400, "没有收到 csv / field_map / workflow_result")
        task["status"] = "uploaded"
        save_task(d, task)
        return self._json(200, {"ok": True, "saved": saved, "task": task_summary(d, task)})

    def _run(self, d):
        task = load_task(d) or {}
        inp = task.get("input") or {}
        files = task.get("files") or {}
        csv_name = files.get("csv")
        wf_name = files.get("workflow_result")
        if not csv_name or not (d / csv_name).exists():
            return self._error(400, "缺少 CSV/Excel 数据文件")
        if not wf_name or not (d / wf_name).exists():
            return self._error(400, "缺少 workflow-result.json（s1–s4 归因结果）")
        if task.get("status") == "running":
            return self._error(409, "任务正在运行")
        if task.get("status") in ("ready", "approved"):
            return self._error(409, "报告已生成（待签/已签），不能重复运行；请新建任务重跑")
        task["status"] = "running"
        save_task(d, task)
        cmd = [
            PY, "tools/run_task.py",
            "--csv", str(d / csv_name),
            "--line", inp.get("line") or "",
            "--t-start", inp["t_start"], "--t-end", inp["t_end"],
            "--base-start", inp["base_start"], "--base-end", inp["base_end"],
            "--outdir", str(d),
            "--workflow-result", str(d / wf_name),
            "--task-id", task["id"],
            "--context", inp.get("context") or f"{inp.get('line') or ''}线补料率异常",
            "--expected", inp["expected"],
            "--defer-sign",
        ]
        fmap = files.get("field_map")
        if fmap and (d / fmap).exists():
            cmd += ["--field-map", str(d / fmap)]
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            log = (p.stdout or "") + ("\n[stderr]\n" + p.stderr if p.stderr else "")
            (d / "run.log").write_text(log, encoding="utf-8")
            task["last_log"] = "run.log"
            if p.returncode != 0:
                task["status"] = "failed"
                save_task(d, task)
                return self._json(422, {"ok": False, "exit": p.returncode, "log": log[-20000:],
                                        "task": task_summary(d, task)})
            report = read_json(d / "report.json") or {}
            task["status"] = "ready"
            task["report"] = "report.json"
            task["loop"] = "loop"
            task["decision_status"] = "pending_signature"
            save_task(d, task)
            return self._json(200, {"ok": True, "exit": 0, "log": log[-20000:],
                                    "task": task_summary(d, task),
                                    "report": report})
        except subprocess.TimeoutExpired:
            task["status"] = "failed"
            save_task(d, task)
            return self._error(504, "运行超时（300s）")

    def _sign(self, d):
        body = self._body_json()
        signer = str(body.get("signer") or "").strip()
        if not signer:
            return self._error(400, "签字人不能为空")
        task = load_task(d) or {}
        if task.get("status") == "approved":
            return self._error(409, "任务已签字，不能重复签字")
        if task.get("status") != "ready":
            return self._error(409, "报告尚未就绪；请先运行收口")
        report = d / "report.json"
        if not report.exists():
            return self._error(404, "报告尚未生成")
        sig = d / "report.signature.json"
        loop = d / "loop"
        p1 = subprocess.run([PY, "tools/sign_delivery.py", str(report), "--signer", signer, "--out", str(sig)],
                            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p1.returncode != 0:
            return self._json(422, {"ok": False, "step": "sign", "log": (p1.stdout or "") + (p1.stderr or "")})
        p2 = subprocess.run([PY, "tools/finalize_delivery.py", "--report", str(report),
                             "--loop-dir", str(loop), "--signature", str(sig)],
                            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p2.returncode != 0:
            return self._json(422, {"ok": False, "step": "finalize", "log": (p2.stdout or "") + (p2.stderr or "")})
        task = load_task(d) or {}
        task["status"] = "approved"
        task["signature"] = "report.signature.json"
        task["decision_status"] = (read_json(loop / "decision.json") or {}).get("status")
        task["signer"] = signer
        save_task(d, task)
        return self._json(200, {"ok": True, "task": task_summary(d, task),
                                "log": (p1.stdout or "") + (p2.stdout or "")})


def make_server(host="127.0.0.1", port=8765, runs_dir=None):
    runs_dir = Path(runs_dir or (ROOT / "runs")).resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.runs_dir = runs_dir
    return httpd


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="默认只绑本机；不要暴露到公网")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--runs-dir", default=str(ROOT / "runs"))
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[WARN] 非回环地址没有鉴权；建议只在本机使用。", file=sys.stderr)
    httpd = make_server(args.host, args.port, args.runs_dir)
    url = f"http://{args.host}:{httpd.server_address[1]}/workbench"
    print(f"[bot-team] task server: {url}")
    print(f"[bot-team] runs dir: {Path(args.runs_dir).resolve()}")
    print("[bot-team] Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[bot-team] stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
