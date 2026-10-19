#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot-team 本地服务（M1 构建规格 §2.6，纯 Python stdlib，无第三方依赖）。

启动（推荐 venv python 绝对路径）：
    "D:\\work\\产品类\\个人产品\\bot-2\\venv\\Scripts\\python.exe" -B server/app.py
    ... --port 8787 --root "D:\\work\\产品类\\个人产品\\bot-2\\JuJing\\bot-team"

端点（M1 spec §2.6 + M1.5 spec §2.5）：
    GET  /                                    ui/index.html
    GET  /ui/<path>                           ui/ 静态资源
    GET  /api/health                          {ok, pack, version, owner, allow_signers, ...}
    GET  /api/packs                           已装行业包列表
    GET  /api/team                            core.team.load_team：employees/crews/shelf/roster/pack
    GET  /api/tasks                           [{task_id,title,state,updated_at,...}]（扫描 runs/）
    POST /api/tasks                           body=intake JSON，可含 data_files[{name,content[,encoding]}]
    GET  /api/tasks/<id>                      {task,report,status,loop,signature,gate,task_id}
    GET  /api/tasks/<id>/delivery             交付卡字段投影（finding/scorecard/roi/decision_log/signature）
    GET  /api/tasks/<id>/log?after=<n>        旧增量端点（保留兼容，读 run.log）
    GET  /api/tasks/<id>/events?since=<n>     事件增量 [{seq,ts,phase,role,agent,kind,line}]
    GET  /api/tasks/<id>/events?since=<n>&stream=1
                                              SSE（id: <seq> / data: <json> / 15s ": ping"，断线续传）
    POST /api/tasks/<id>/data                 body {files:[{name,content[,encoding]}]} 追加数据文件
    POST /api/tasks/<id>/run                  body {runner:"fixture"|"external"|"dsh"}，立即 202
    POST /api/tasks/<id>/gate                 body {action:"confirm"|"reject",confirmer,comment}
    POST /api/tasks/<id>/sign                 body {signer} -> tools/sign_delivery.py -> {sha256,signer}
    POST /api/tasks/<id>/finalize             tools/finalize_delivery.py，不通过 409 + 原因

身份与 runner（M1.5 spec §2.5/§2.7）：
- `--owner`（默认 "老板"）、`--allow-signers`（默认 "老板,boss"）；/sign、/gate、/finalize 的
  signer/confirmer 必须在白名单（不读环境变量兜底），响应均带 identity。
- runner=fixture（默认）：把 runs/demo-result-2026-10-19.json 复制成 outdir/result.json 再调
  run_task --demo，报告必须 is_demo=true，禁止冒充真实；external：等 outdir/result.json 出现
  （最长 600s）后跑真实链路；dsh：501（M2 实现）。
- 数据上传（POST /api/tasks[/<id>/data]）：单文件 ≤8MB、总 ≤24MB，落 runs/<id>/data/，
  文件名走 core.paths.safe_relative（缺失时用 server 内置同语义兜底），校验 meta.field_contract /
  meta.schema_version / records 非空。

设计约束：
- 只绑定 127.0.0.1；--host/--port/--root 可配。
- 所有子进程用 venv python 绝对路径（环境变量 BOT_PY 可覆盖）。
- 失败统一 {ok:false,error} + 4xx/5xx；单个请求/子进程失败不会让服务退出。
- 状态机：created -> running -> assembled/awaiting_sign -> signed；异常 -> failed。
- **单写者约定（B1 确认）**：`runs/<id>/run.log` 与 `status.json` 由 tools/run_task.py 写，
  server 只在 (a) POST /tasks 写 created、(b) /run 子进程退出后读状态并在缺失/卡在 running 时兜底、
  (c) /sign、/finalize 子进程成功后才更新状态。server 不向 run.log 追加事件，避免双写。
- GET /log 读取 run.log 并补 1-based 序号 n，返回 [{n,ts,phase,line}]（spec §2.5/§2.6）。
- 任务编号：优先 core.ids.allocate（B1 落盘后自动生效），否则回退到扫描 runs/T-YYYYMMDD-* 取下一个序号。
"""
import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
TZ = timezone(timedelta(hours=8))
VENV_PY = r"D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
TASK_ROUTE_RE = re.compile(r"^/api/tasks/([^/]+)(?:/([a-z]+))?$")
SIX_DIMS = ("data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency")
DEFAULT_OWNER = "老板"
DEFAULT_ALLOW_SIGNERS = "老板,boss"
DEMO_RESULT = "demo-result-2026-10-19.json"
MAX_UPLOAD_FILE = 8 * 1024 * 1024          # 单文件 8MB
MAX_UPLOAD_TOTAL = 24 * 1024 * 1024        # 总量 24MB
MAX_BODY_BYTES = 36 * 1024 * 1024          # 24MB 数据 base64 膨胀 4/3 + JSON 余量
EXTERNAL_WAIT_SECONDS = 600                # runner=external 最长等待
SSE_POLL_SECONDS = 0.5
SSE_PING_SECONDS = 15.0
GATE_FILE = "gate.json"
PHASE_ALIAS = {
    "ingest": "s1", "snapshot": "s1", "contract": "s2", "factor": "s2", "factors": "s2",
    "attribution": "s3", "exclusion": "s3", "exclude": "s3", "advice": "s4", "recommend": "s4",
    "assemble": "s5", "report": "s5", "validate": "s5", "validate-report": "s5", "validate-report-real": "s5",
    "validate-loop": "s5", "score": "s5", "scorecard": "s5", "sign": "s5", "finalize": "s5",
}
PHASE_ROLE = {"s1": "analyst", "s2": "biz", "s3": "attr", "s4": "biz", "s5": "host"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".woff2": "font/woff2", ".woff": "font/woff", ".map": "application/json; charset=utf-8",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _enable_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def log(message):
    try:
        print("[server] " + str(message), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def iso_mtime(path):
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, TZ).isoformat(timespec="seconds")
    except OSError:
        return now_iso()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log(f"read_json 失败 {path}: {exc}")
        return default


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_bytes_atomic(path, data):
    """原子写二进制（temp + fsync + os.replace），失败清理 temp。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# core/ 模块接入（B4 并行落盘：按 spec 契约调用，缺失时给明确 500）
# --------------------------------------------------------------------------- #
_CORE_PATH_LOCK = threading.Lock()


def ensure_core_path(cfg):
    root = str(cfg.root)
    with _CORE_PATH_LOCK:
        if root not in sys.path:
            sys.path.insert(0, root)


def import_core(cfg, name):
    """导入 core.<name>；未落盘时抛 500 + 明确错误（不静默降级）。"""
    ensure_core_path(cfg)
    try:
        return importlib.import_module("core." + name)
    except Exception as exc:  # noqa: BLE001 - ImportError / 模块内错误
        raise ApiError(500,
                       f"core.{name} 不可用（B4 尚未落盘或导入失败）：{exc}；"
                       f"契约见 audit/M1.5-build-spec.md §2")


def core_paths_module(cfg):
    """core.paths 优先；未就绪时返回 None（调用方用内置兜底，handoff 记录）。"""
    try:
        return import_core(cfg, "paths")
    except ApiError:
        return None


def _fallback_safe_relative(name):
    """与 core.paths.safe_relative 同语义的兜底：拒绝绝对路径/盘符/../ /控制字符，
    仅允许 [\\w.\\-] 与最多单层子目录（保留中文等非 ASCII 文字字符）。"""
    raw = str(name or "").replace("\\", "/").strip()
    if not raw:
        raise ApiError(400, "数据文件名不能为空")
    if "\x00" in raw or any(ord(ch) < 0x20 for ch in raw):
        raise ApiError(400, f"数据文件名含控制字符: {name!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ApiError(400, f"数据文件名不允许绝对路径/盘符: {name!r}")
    parts = raw.split("/")
    if len(parts) > 2:
        raise ApiError(400, f"数据文件名只允许单层子目录: {name!r}")
    for part in parts:
        if part in ("", ".", ".."):
            raise ApiError(400, f"数据文件名非法（含空段/./..）: {name!r}")
        if not re.match(r"^[\w.\-]+$", part, flags=re.UNICODE):
            raise ApiError(400, f"数据文件名含非法字符: {name!r}")
    return "/".join(parts)


def safe_relative(cfg, name):
    """数据文件名安全化：优先 core.paths.safe_relative（spec §2.4）。"""
    module = core_paths_module(cfg)
    if module is not None:
        try:
            return str(module.safe_relative(name))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转 400
            raise ApiError(400, f"数据文件名非法: {name!r}（{exc}）")
    return _fallback_safe_relative(name)


def child_env():
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("BOT_SIGNERS", "老板,boss")
    return env


def find_python(cfg=None):
    candidates = [os.environ.get("BOT_PY", ""), VENV_PY]
    if cfg is not None:
        for rel in ("venv/Scripts/python.exe", ".venv/Scripts/python.exe", "venv/bin/python", ".venv/bin/python"):
            candidates.append(str(cfg.root.parent / rel))
    candidates.append(sys.executable)
    for cand in candidates:
        if cand and Path(cand).is_file():
            return str(Path(cand))
    return sys.executable


def _quote_arg(arg):
    text = str(arg)
    return f'"{text}"' if (" " in text or "\t" in text) else text


def _cmd_display(cmd):
    return " ".join(_quote_arg(part) for part in cmd)


# --------------------------------------------------------------------------- #
# run.log（JSON lines）
# --------------------------------------------------------------------------- #
class RunLog:
    """只读 run.log（写者是 tools/run_task.py，server 不双写）。"""

    def __init__(self, path):
        self.path = Path(path)

    def events(self, after=0):
        try:
            with open(self.path, "r", encoding="utf-8-sig", errors="replace") as fh:
                out = []
                for idx, raw in enumerate(fh, start=1):
                    if idx <= after:
                        continue
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        record = {"phase": "raw", "line": raw}
                    if not isinstance(record, dict):
                        record = {"phase": "raw", "line": str(record)}
                    out.append({
                        "n": idx,
                        "ts": record.get("ts") or "",
                        "phase": record.get("phase") or "run",
                        "role": record.get("role") or "host",
                        "line": record.get("line") or "",
                    })
                return out
        except FileNotFoundError:
            return []


# --------------------------------------------------------------------------- #
# 服务配置 / 任务目录
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.extra = extra or {}


class Config:
    def __init__(self, root, owner=DEFAULT_OWNER, allow_signers=DEFAULT_ALLOW_SIGNERS):
        self.root = Path(root).resolve()
        self.runs = self.root / "runs"
        self.ui = self.root / "ui"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.owner = str(owner or DEFAULT_OWNER).strip() or DEFAULT_OWNER
        self.allow_signers = parse_signers(allow_signers or DEFAULT_ALLOW_SIGNERS)
        self.alloc_lock = threading.Lock()
        self.map_lock = threading.Lock()
        self.running_lock = threading.Lock()
        self.running = set()
        self._task_locks = {}
        # core/ 是 B4 的产物，按 spec 契约调用；把它加进 sys.path 一次（包内相互 import 需要）
        ensure_core_path(self)

    def task_lock(self, task_id):
        with self.map_lock:
            return self._task_locks.setdefault(task_id, threading.Lock())

    def identity(self):
        return {"owner": self.owner, "allow_signers": list(self.allow_signers)}


def parse_signers(raw):
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw]
    else:
        parts = [item.strip() for item in re.split(r"[,，;；\s]+", str(raw or ""))]
    return tuple(dict.fromkeys(item for item in parts if item))


def task_title(task):
    if not isinstance(task, dict):
        return "未命名任务"
    for key in ("title", "context", "summary", "goal"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts = []
    plant = task.get("plant")
    if isinstance(plant, str) and plant.strip():
        parts.append(plant.strip())
    line = task.get("line")
    if line not in (None, ""):
        parts.append(f"{line}线")
    metric = task.get("metric")
    if isinstance(metric, str) and metric.strip():
        parts.append(metric.strip())
    return " · ".join(parts) if parts else str(task.get("task_id") or "未命名任务")


def task_dir(cfg, task_id):
    if not TASK_ID_RE.match(task_id or ""):
        raise ApiError(400, f"非法 task_id: {task_id!r}")
    target = (cfg.runs / task_id).resolve()
    try:
        target.relative_to(cfg.runs.resolve())
    except ValueError:
        raise ApiError(400, f"非法 task_id（越界）: {task_id!r}")
    if not target.is_dir():
        raise ApiError(404, f"任务不存在: {task_id}")
    return target


def status_path(tdir):
    return Path(tdir) / "status.json"


def read_status(tdir):
    doc = read_json(status_path(tdir))
    if isinstance(doc, dict):
        return doc
    return {"state": "created", "step": "intake", "updated_at": iso_mtime(tdir)}


def write_status(tdir, **fields):
    doc = read_status(tdir)
    doc.update(fields)
    doc["updated_at"] = now_iso()
    write_json_atomic(status_path(tdir), doc)
    return doc


def signature_path(tdir):
    for name in ("signature.json", "report.signature.json"):
        candidate = Path(tdir) / name
        if candidate.is_file():
            return candidate
    return None


def signature_doc(tdir):
    for name in ("signature.json", "report.signature.json"):
        doc = read_json(Path(tdir) / name)
        if isinstance(doc, dict):
            return doc
    return None


def read_loop(tdir):
    loop_dir = Path(tdir) / "loop"
    out = {"evidence": None, "analysis": None, "decision": None, "closure": None, "cost": None, "goal": None}
    if not loop_dir.is_dir():
        return out
    for path in sorted(loop_dir.glob("*.json")):
        key = path.stem
        value = read_json(path)
        if key == "evidence":
            out["evidence"] = value if value is not None else []
        elif key in out:
            out[key] = value
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# 任务编号
# --------------------------------------------------------------------------- #
def _core_allocate(cfg):
    """优先用 B1 的 core.ids.allocate（同日不重复、跨进程锁）；不可用则返回空串。"""
    root = str(cfg.root)
    try:
        inserted = False
        if root not in sys.path:
            with cfg.alloc_lock:
                if root not in sys.path:
                    sys.path.insert(0, root)
                    inserted = True
        try:
            from core.ids import allocate  # type: ignore  # B1 产物，可能尚未落盘
        finally:
            if inserted:
                try:
                    sys.path.remove(root)
                except ValueError:
                    pass
        return str(allocate(str(cfg.runs), "task"))
    except Exception as exc:  # noqa: BLE001 - 回退是预期路径（B1 未就绪时）
        log(f"core.ids.allocate 不可用，回退本地分配: {exc}")
        return ""


def allocate_task_id(cfg, explicit=None):
    if explicit:
        if not TASK_ID_RE.match(explicit):
            raise ApiError(400, f"非法 task_id: {explicit!r}")
        if (cfg.runs / explicit).exists():
            raise ApiError(409, f"task_id 已存在: {explicit}")
        return explicit
    allocated = _core_allocate(cfg)
    if allocated:
        return allocated
    day = datetime.now(TZ).strftime("%Y%m%d")
    prefix = f"T-{day}-"
    with cfg.alloc_lock:
        seq = 0
        for child in cfg.runs.iterdir():
            if not child.is_dir():
                continue
            match = re.match(r"^" + re.escape(prefix) + r"(\d{3,})$", child.name)
            if match:
                seq = max(seq, int(match.group(1)))
        while True:
            seq += 1
            task_id = f"{prefix}{seq:03d}"
            if not (cfg.runs / task_id).exists():
                return task_id


# --------------------------------------------------------------------------- #
# 任务读写
# --------------------------------------------------------------------------- #
def list_tasks(cfg):
    items = []
    if not cfg.runs.is_dir():
        return items
    for child in cfg.runs.iterdir():
        if not child.is_dir():
            continue
        task = read_json(child / "task.json")
        if not isinstance(task, dict):
            continue
        status = read_status(child)
        report = read_json(child / "report.json")
        signature = signature_doc(child)
        closure = read_json(child / "loop" / "closure.json")
        gate_doc = read_json(child / GATE_FILE)
        if not isinstance(closure, dict):
            closure = {}
        if not isinstance(gate_doc, dict):
            gate_doc = {}
        status_task = status.get("task") if isinstance(status.get("task"), dict) else {}
        verdict = closure.get("verdict")
        items.append({
            "task_id": str(task.get("task_id") or child.name),
            "title": task_title(task),
            "state": str(status.get("state") or "created"),
            "updated_at": status.get("updated_at") or iso_mtime(child / "task.json"),
            "step": status.get("step"),
            "error": status.get("error"),
            "has_report": isinstance(report, dict),
            "signed": signature is not None,
            "is_demo": report.get("is_demo") if isinstance(report, dict) else None,
            "review_due": closure.get("due") or closure.get("review_due"),
            "reviewed": verdict not in (None, "", "unverified"),
            "data_ready": bool(status.get("data_ready") or status_task.get("data_ready")),
            "runner": task.get("runner"),
            "gate": gate_doc.get("status"),
        })
    items.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return items


def validate_dataset(doc, name=""):
    """T/T30 数据文件 schema 校验（M1.5 spec §2.5）。返回错误列表（空=通过）。"""
    errors = []
    if not isinstance(doc, dict):
        return ["数据文件必须是 JSON 对象（meta/records）"]
    meta = doc.get("meta")
    records = doc.get("records")
    if not isinstance(meta, dict):
        errors.append("meta 缺失或不是对象")
    else:
        contract = meta.get("field_contract")
        if not (isinstance(contract, list) and contract
                and all(isinstance(item, str) and item.strip() for item in contract)):
            errors.append("meta.field_contract 缺失或不是非空字符串数组")
        version = meta.get("schema_version")
        if not (isinstance(version, str) and version.strip()):
            errors.append("meta.schema_version 缺失或为空")
    if not (isinstance(records, list) and records):
        errors.append("records 缺失或为空数组")
    elif not all(isinstance(record, dict) for record in records):
        errors.append("records 内每条记录必须是对象")
    if name:
        errors = [f"{name}: {item}" for item in errors]
    return errors


def _upload_items(cfg, body, key="data_files"):
    """把 body 里的上传项分成 (uploads, kept_paths, manifest_entries)。

    - data_files 里的字符串 = 既有数据文件路径（保留给 task.json.data_files）
    - data_files 里的对象 = 上传（{name, content[, encoding]}）
    """
    raw = body.get(key)
    uploads = []
    kept = []
    if raw in (None, ""):
        return uploads, kept
    if not isinstance(raw, list):
        raise ApiError(400, f"{key} 必须是数组（字符串路径或 {{name,content}} 上传项）")
    total = 0
    for item in raw:
        if isinstance(item, str):
            kept.append(item)
            continue
        if not isinstance(item, dict):
            raise ApiError(400, f"{key} 内元素必须是字符串或对象")
        name = ""
        for field in ("name", "path", "filename", "file"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        if not name:
            raise ApiError(400, f"{key}[] 缺 name")
        safe_name = safe_relative(cfg, name)
        content = item.get("content")
        if isinstance(content, (bytes, bytearray)):
            data = bytes(content)
        elif isinstance(content, str):
            encoding = str(item.get("encoding") or ("base64" if item.get("base64") else "text")).lower()
            if encoding in ("base64", "b64"):
                try:
                    data = base64.b64decode(content, validate=True)
                except (ValueError, TypeError):
                    raise ApiError(400, f"数据文件 base64 解码失败: {safe_name}")
            else:
                data = content.encode("utf-8")
        else:
            raise ApiError(400, f"数据文件 content 必须是字符串: {safe_name}")
        if len(data) > MAX_UPLOAD_FILE:
            raise ApiError(400, f"单文件超过 8MB 上限: {safe_name}（{len(data)} bytes）")
        total += len(data)
        if total > MAX_UPLOAD_TOTAL:
            raise ApiError(400, f"数据文件总量超过 24MB 上限（{total} bytes）")
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(400, f"数据文件不是合法 UTF-8 JSON: {safe_name}（{exc}）")
        errors = validate_dataset(doc, safe_name)
        if errors:
            raise ApiError(400, "数据文件 schema 校验失败: " + "; ".join(errors))
        uploads.append({"name": safe_name, "data": data, "sha256": sha256_bytes(data)})
    return uploads, kept


def apply_uploads(cfg, tdir, task_id, uploads):
    """落盘 runs/<id>/data/ 并更新 task.json.data_files + status.task.data_ready。"""
    if not uploads:
        return [], []
    data_dir = Path(tdir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    data_root = data_dir.resolve()
    files = []
    written = []
    for item in uploads:
        target = (data_dir / item["name"]).resolve()
        try:
            target.relative_to(data_root)
        except ValueError:
            raise ApiError(400, f"数据文件越界: {item['name']!r}")
        write_bytes_atomic(target, item["data"])
        rel = target.relative_to(Path(cfg.root).resolve()).as_posix()
        record = {"name": item["name"], "path": rel, "sha256": item["sha256"], "size": len(item["data"])}
        files.append(record)
        written.append(rel)
    with cfg.task_lock(task_id):
        task = read_json(Path(tdir) / "task.json") or {}
        if not isinstance(task, dict):
            task = {}
        data_files = [str(entry) for entry in (task.get("data_files") or []) if isinstance(entry, str)]
        for rel in written:
            if rel not in data_files:
                data_files.append(rel)
        manifest = [entry for entry in (task.get("data_manifest") or []) if isinstance(entry, dict)]
        names = {entry.get("path") for entry in manifest}
        for record in files:
            if record["path"] in names:
                manifest = [entry for entry in manifest if entry.get("path") != record["path"]]
            manifest.append(record)
        task["data_files"] = data_files
        task["data_manifest"] = manifest
        task["updated_at"] = now_iso()
        write_json_atomic(Path(tdir) / "task.json", task)
        status = read_status(tdir)
        status_task = status.get("task") if isinstance(status.get("task"), dict) else {}
        status_task = dict(status_task)
        status_task.update({"data_ready": True, "data_files": data_files, "data_manifest": manifest})
        write_status(tdir, task=status_task, data_ready=True)
    return files, data_files


def create_task(cfg, body):
    if not isinstance(body, dict):
        raise ApiError(400, "body 必须是 intake JSON 对象")
    intake = dict(body)
    uploads, _kept = _upload_items(cfg, body, "data_files")
    if isinstance(body.get("data_files"), list):
        kept = [item for item in body["data_files"] if isinstance(item, str)]
        if kept:
            intake["data_files"] = kept
        else:
            intake.pop("data_files", None)
    explicit = str(body.get("task_id") or "").strip()
    task_id = allocate_task_id(cfg, explicit or None)
    tdir = cfg.runs / task_id
    if tdir.exists():
        raise ApiError(409, f"task_id 已存在: {task_id}")
    tdir.mkdir(parents=True, exist_ok=True)
    task = intake
    task["task_id"] = task_id
    task.setdefault("created_at", now_iso())
    task.setdefault("runner", str(body.get("runner") or "fixture"))
    write_json_atomic(tdir / "task.json", task)
    write_json_atomic(status_path(tdir), {
        "state": "created", "step": "intake",
        "created_at": task.get("created_at"), "updated_at": now_iso(),
    })
    files, data_files = apply_uploads(cfg, tdir, task_id, uploads)
    response = dict(task)
    response["ok"] = True
    response["data_files"] = data_files
    response["files"] = files
    response["data_ready"] = bool(files) or bool(data_files)
    return response


def post_upload_data(cfg, task_id, body):
    """POST /api/tasks/<id>/data：追加数据文件（与 POST /api/tasks 的 data_files 同校验）。"""
    tdir = task_dir(cfg, task_id)
    if not isinstance(body, dict):
        raise ApiError(400, "body 必须是 JSON 对象")
    raw = body.get("files")
    if raw is None:
        raw = body.get("data_files")
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "body.files 必须是非空数组（{name,content[,encoding]}）")
    uploads, _kept = _upload_items(cfg, {"data_files": raw}, "data_files")
    if not uploads:
        raise ApiError(400, "body.files 内没有可落盘的上传项")
    files, data_files = apply_uploads(cfg, tdir, task_id, uploads)
    status = read_status(tdir)
    return 200, {"ok": True, "task_id": task_id, "files": files,
                 "data_files": data_files, "data_ready": True,
                 "status": status, "identity": cfg.identity()}


def get_task(cfg, task_id):
    tdir = task_dir(cfg, task_id)
    gate = read_json(tdir / GATE_FILE)
    return {
        "task": read_json(tdir / "task.json"),
        "report": read_json(tdir / "report.json"),
        "status": read_status(tdir),
        "loop": read_loop(tdir),
        "signature": signature_doc(tdir),
        "gate": gate if isinstance(gate, dict) else None,
        "task_id": task_id,
        "identity": cfg.identity(),
    }


def delivery_view(cfg, task_id):
    tdir = task_dir(cfg, task_id)
    report = read_json(tdir / "report.json")
    if not isinstance(report, dict):
        raise ApiError(404, f"report.json 不存在: {task_id}（先 POST /api/tasks/{task_id}/run）")
    recommendation = report.get("recommendation") or []
    first = recommendation[0] if recommendation and isinstance(recommendation[0], dict) else {}
    signature = signature_doc(tdir) or {}
    if not signature and (tdir / "report.signature.json").is_file():
        signature = read_json(tdir / "report.signature.json") or {}
    return {
        "ok": True,
        "task_id": report.get("task_id") or task_id,
        "is_demo": report.get("is_demo"),
        "goal": report.get("goal"),
        "generated_at": report.get("generated_at"),
        "finding": report.get("finding") or [],
        "cannot_attribute": report.get("cannot_attribute") or [],
        "scorecard": report.get("scorecard") or {},
        "roi": first.get("roi") if isinstance(first, dict) else None,
        "recommendation": recommendation,
        "decision_log": report.get("decision_log_entry") or {},
        "signature": signature,
        "signature_sha256": signature.get("report_sha256"),
    }


def get_log(cfg, task_id, after):
    """旧端点：run.log 由 tools/run_task.py 写；这里补 1-based 序号并返回 spec 的 4 字段。"""
    tdir = task_dir(cfg, task_id)
    events = RunLog(tdir / "run.log").events(max(0, after))
    return [{"n": ev["n"], "ts": ev["ts"], "phase": ev["phase"], "line": ev["line"]}
            for ev in events]


# --------------------------------------------------------------------------- #
# 团队数据（GET /api/team：core.team.load_team，M1.5 spec §2.1）
# --------------------------------------------------------------------------- #
AGENT_BY_ROLE = {
    "host": "工作助理",
    "analyst": "咨询分析师",
    "attr": "AI 归因专家",
    "biz": "制造业业务专家",
    "quality": "合规质检",
    "data": "取数员",
}


def team_view(cfg):
    module = import_core(cfg, "team")
    try:
        doc = module.load_team(str(cfg.root))
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError(500, f"core.team.load_team 失败: {exc}")
    if not isinstance(doc, dict):
        raise ApiError(500, "core.team.load_team 返回不是对象（契约：dict）")
    if not isinstance(doc.get("employees"), list):
        raise ApiError(500, "core.team.load_team 返回缺 employees（契约见 M1.5 spec §2.1）")
    out = dict(doc)
    out["ok"] = True
    out["team_id"] = str(out.get("team_id") or "manufacturing-refill")
    warnings = [str(item) for item in (out.get("warnings") or [])]
    crews = out.get("crews") if isinstance(out.get("crews"), list) else []
    crew_ids = sorted({str(crew.get("id")) for crew in crews if isinstance(crew, dict) and crew.get("id")})
    pack = out.get("pack") if isinstance(out.get("pack"), dict) else {}
    crew_id = str(pack.get("crew_id") or "")
    if crew_id and crew_id not in crew_ids:
        raise ApiError(500, f"团队数据不一致：pack.crew_id={crew_id!r} 不在 crews {crew_ids}")
    required = ("id", "name", "layer", "category", "state", "skills", "sample", "sprite_key")
    for emp in out["employees"]:
        if not isinstance(emp, dict):
            raise ApiError(500, "core.team.load_team 的 employees 内存在非对象条目")
        missing = [key for key in required if emp.get(key) in (None, "", [])]
        if missing:
            warnings.append(f"员工 {emp.get('id')} 缺字段: {missing}")
    out["warnings"] = warnings
    out["identity"] = cfg.identity()
    return 200, out


# --------------------------------------------------------------------------- #
# 事件流（GET /api/tasks/<id>/events，M1.5 spec §2.2/§2.5）
# --------------------------------------------------------------------------- #
def _normalize_event(ev, fallback_seq):
    phase = str(ev.get("phase") or "status")
    role = str(ev.get("role") or "host")
    try:
        seq = int(ev.get("seq"))
    except (TypeError, ValueError):
        seq = int(fallback_seq)
    record = {
        "seq": seq,
        "ts": str(ev.get("ts") or ""),
        "phase": phase,
        "role": role,
        "agent": str(ev.get("agent") or AGENT_BY_ROLE.get(role, role)),
        "kind": str(ev.get("kind") or "status"),
        "line": str(ev.get("line") or ""),
    }
    meta = ev.get("meta")
    if isinstance(meta, dict):
        record["meta"] = meta
    return record


def read_events(cfg, run_log, since=0):
    """事件列表：优先 core.events.iter_events（spec §2.2）；core 未就绪时回退旧 run.log 解析。"""
    try:
        since = max(0, int(since or 0))
    except (TypeError, ValueError):
        since = 0
    try:
        module = import_core(cfg, "events")
        raw = module.iter_events(str(run_log), since)
        if isinstance(raw, list):
            out = []
            for index, ev in enumerate(raw, start=1):
                if isinstance(ev, dict):
                    item = _normalize_event(ev, since + index)
                    if item["seq"] > since:
                        out.append(item)
            out.sort(key=lambda item: item["seq"])
            return out
        log("core.events.iter_events 未返回 list，回退 run.log 解析")
    except ApiError:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"core.events.iter_events 失败（{exc}），回退 run.log 解析")
    out = []
    for ev in RunLog(run_log).events(since):
        role = str(ev.get("role") or "host")
        out.append({
            "seq": int(ev["n"]),
            "ts": str(ev.get("ts") or ""),
            "phase": str(ev.get("phase") or "status"),
            "role": role,
            "agent": AGENT_BY_ROLE.get(role, role),
            "kind": "status",
            "line": str(ev.get("line") or ""),
        })
    return out


def get_events(cfg, task_id, since):
    tdir = task_dir(cfg, task_id)
    return read_events(cfg, tdir / "run.log", since)


def get_events_meta(cfg, task_id, since):
    tdir = task_dir(cfg, task_id)
    events = read_events(cfg, tdir / "run.log", since)
    last = events[-1]["seq"] if events else max(0, int(since or 0))
    return {"ok": True, "task_id": task_id, "since": max(0, int(since or 0)),
            "last_seq": last, "events": events, "identity": cfg.identity()}


# --------------------------------------------------------------------------- #
# 子进程 CLI 探测与调用（run_task.py 契约以 B1 落地为准，做兼容映射）
# --------------------------------------------------------------------------- #
_HELP_CACHE = {}


def run_task_help_flags(cfg):
    script = cfg.root / "tools" / "run_task.py"
    if not script.is_file():
        return None
    try:
        stamp = script.stat().st_mtime
    except OSError:
        stamp = 0
    key = (str(script), stamp)
    cached = _HELP_CACHE.get(key)
    if cached is not None:
        return cached
    flags = set()
    try:
        proc = subprocess.run(
            [find_python(cfg), "-B", str(script), "--help"],
            cwd=str(cfg.root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, env=child_env(),
        )
        flags = set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9_-]*",
                               (proc.stdout or "") + "\n" + (proc.stderr or "")))
        flags.discard("--help")
    except Exception as exc:  # noqa: BLE001
        log(f"run_task.py --help 探测失败: {exc}")
    _HELP_CACHE[key] = flags
    return flags


def _add_opt(cmd, flags, name, value):
    if name in flags and value not in (None, ""):
        cmd.extend([name, str(value)])


def _window_range(task, *keys):
    for key in keys:
        value = task.get(key)
        if isinstance(value, dict):
            start = value.get("start") or value.get("from") or ""
            end = value.get("end") or value.get("to") or ""
            if start and end:
                return str(start), str(end)
        elif isinstance(value, str) and value.strip():
            parts = [part.strip() for part in re.split(r"[~→]|至|\.\.", value) if part.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
    return "", ""


def _resolve(cfg, value):
    if not value:
        return ""
    path = Path(str(value))
    if not path.is_absolute():
        path = cfg.root / path
    return str(path)


def _first_file(cfg, candidates):
    for cand in candidates:
        if not cand:
            continue
        path = Path(str(cand))
        if not path.is_absolute():
            path = cfg.root / path
        if path.is_file():
            return str(path)
    return ""


def _workflow_result(cfg, task, tdir):
    explicit = task.get("workflow_result") or task.get("result_json")
    if explicit:
        return _resolve(cfg, explicit)
    return _first_file(cfg, [
        tdir / "result.json",
        tdir / "workflow-result.json",
        tdir / "team-workflow-result.json",
        tdir / "s1-s4-result.json",
    ])


def _append_common_run_opts(cfg, task, tdir, flags, cmd, include_signer=True):
    title = task_title(task)
    _add_opt(cmd, flags, "--task-id", task.get("task_id") or tdir.name)
    _add_opt(cmd, flags, "--outdir", str(tdir))
    _add_opt(cmd, flags, "--context", task.get("context") or title)
    _add_opt(cmd, flags, "--goal", task.get("goal") or title)
    _add_opt(cmd, flags, "--expected", task.get("target") or task.get("expected"))
    # 只透传 intake 里显式给的签字人；缺省不在 run 阶段自动签字，
    # 让 UI 走「run → assembled/awaiting_sign → POST /sign → POST /finalize」两步流。
    # runner=fixture 组装的是 demo 报告，签字/收口一律交给后续显式 API，不在 run 里做。
    explicit_signer = task.get("signer") or task.get("approver") or task.get("boss_signer")
    _add_opt(cmd, flags, "--signer", explicit_signer if include_signer else "")
    _add_opt(cmd, flags, "--workflow-result", _workflow_result(cfg, task, tdir))
    _add_opt(cmd, flags, "--analysis-json", _resolve(cfg, task.get("analysis_json")))
    _add_opt(cmd, flags, "--goal-json", _resolve(cfg, task.get("goal_json")))
    _add_opt(cmd, flags, "--cost-json", _resolve(cfg, task.get("cost_json")))
    _add_opt(cmd, flags, "--field-map", _resolve(cfg, task.get("field_map")))
    _add_opt(cmd, flags, "--line", task.get("line"))
    return cmd


def build_run_command(cfg, task, tdir, extra_args=None, include_signer=True):
    """把 task.json 映射成 tools/run_task.py 调用；识别不了契约时抛 ApiError(422)。

    extra_args：runner 特有 flag（fixture → --demo 等），插在脚本名之后。
    """
    if not isinstance(task, dict):
        task = {}
    extra_args = [str(item) for item in (extra_args or [])]
    script = cfg.root / "tools" / "run_task.py"
    if not script.is_file():
        raise ApiError(503, "tools/run_task.py 不存在（B1 后端尚未落盘），无法运行；请先在 $BT 下补齐 tools/")
    explicit_args = task.get("run_args")
    if isinstance(explicit_args, list) and explicit_args and all(isinstance(x, str) for x in explicit_args):
        return [find_python(cfg), "-B", str(script)] + extra_args + explicit_args

    flags = run_task_help_flags(cfg)
    if not flags:
        raise ApiError(422, "无法识别 tools/run_task.py 参数（--help 未返回可用 flag）；"
                            "请在 task.json 里写 run_args 显式传参，或让 B1 落定 CLI 契约")
    cmd = [find_python(cfg), "-B", str(script)] + extra_args

    task_json_flags = [name for name in ("--task-json", "--intake", "--task-file", "--task") if name in flags]
    if task_json_flags:
        cmd.extend([task_json_flags[0], str(tdir / "task.json")])
        _append_common_run_opts(cfg, task, tdir, flags, cmd, include_signer=include_signer)
        return cmd

    if "--csv" in flags:
        csv = (task.get("csv") or task.get("input_csv") or task.get("data_csv")
               or task.get("raw_csv") or "")
        if not csv:
            raise ApiError(422, "task.json 缺 csv：当前 run_task.py 是旧版 CSV 入口，"
                                "需要原始日台账 CSV 绝对路径（字段 csv/input_csv/data_csv）；"
                                "请在 intake 里填 csv，或等 B1 落 --task-json 入口")
        csv = _resolve(cfg, csv)
        if not Path(csv).is_file():
            raise ApiError(422, f"csv 不存在: {csv}")
        t_start, t_end = _window_range(task, "window", "t_window")
        b_start, b_end = _window_range(task, "baseline_window", "baseline", "base_window")
        missing = []
        if not (t_start and t_end):
            missing.append("window.start/window.end")
        if not (b_start and b_end):
            missing.append("baseline_window.start/baseline_window.end")
        if not (task.get("target") or task.get("expected")):
            missing.append("target/expected")
        if missing:
            raise ApiError(422, "旧版 run_task.py 缺少必填字段: " + ", ".join(missing))
        cmd.extend(["--csv", csv, "--t-start", t_start, "--t-end", t_end,
                    "--base-start", b_start, "--base-end", b_end])
        _append_common_run_opts(cfg, task, tdir, flags, cmd, include_signer=include_signer)
        if "--workflow-result" in flags and not _workflow_result(cfg, task, tdir):
            raise ApiError(422, "缺 workflow result：把 team-workflow.mjs 的返回写到 "
                                "runs/<id>/result.json（或 workflow-result.json），"
                                "或在 task.json 里写 workflow_result")
        return cmd

    raise ApiError(422, "无法识别 tools/run_task.py 参数：--help flags=" + ", ".join(sorted(flags)))


def _fail_task(cfg, tdir, message):
    try:
        with cfg.task_lock(tdir.name):
            write_status(tdir, state="failed", step="run", error=str(message),
                         finished_at=now_iso())
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())


def _read_status_state(tdir):
    doc = read_json(status_path(tdir))
    return str(doc.get("state") or "") if isinstance(doc, dict) else ""


def run_task_thread(cfg, tdir, task, runner="external", extra_args=None, include_signer=True):
    """后台执行 tools/run_task.py。

    runner=external 时先等 outdir/result.json（最长 600s，状态 running/waiting_result）；
    runner=fixture 时由 post_run 预先写好 result.json，并强制校验报告 is_demo=true。
    run.log / status.json 由 run_task.py 单写（B1 约定）；server 不双写，只在子进程退出后：
    - 非 0 退出且状态未落 failed -> 兜底 failed + error（无 run.log 写入）；
    - 0 退出但状态仍停在 created/running（旧版 CLI 不写 status）-> 按产物推导终态。
    """
    task_id = tdir.name
    captured = []
    try:
        if runner == "external":
            with cfg.task_lock(task_id):
                write_status(tdir, state="running", step="waiting_result", runner=runner)
            target = tdir / "result.json"
            deadline = time.time() + EXTERNAL_WAIT_SECONDS
            while time.time() < deadline:
                if target.is_file():
                    break
                time.sleep(1.0)
            if not target.is_file():
                message = f"runner=external 等待 result.json 超时（{EXTERNAL_WAIT_SECONDS}s）：{target} 未出现"
                _fail_task(cfg, tdir, message)
                log(f"run timeout {task_id}: {message}")
                return
        cmd = build_run_command(cfg, task, tdir, extra_args=extra_args, include_signer=include_signer)
        log(f"run start {task_id}: runner={runner} {_cmd_display(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(cfg.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=child_env(),
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            captured.append(line)
            if len(captured) > 200:
                del captured[:100]
            log(f"[{task_id}] {line}")
        code = proc.wait()
        report = read_json(tdir / "report.json")
        signature = signature_doc(tdir)
        tip = ""
        for line in reversed(captured[-60:]):
            if re.search(r"(?i)(error|failed|traceback|拒绝|失败)", line):
                tip = line[:400]
                break
        if runner == "fixture" and code == 0:
            if not isinstance(report, dict) or report.get("is_demo") is not True:
                with cfg.task_lock(task_id):
                    write_status(tdir, state="failed", step="fixture", exit_code=0,
                                 error="runner=fixture 必须产出 is_demo=true 的演示报告（禁止冒充真实）",
                                 runner=runner, finished_at=now_iso())
                log(f"run rejected {task_id}: fixture 报告 is_demo 不为 true")
                return
        with cfg.task_lock(task_id):
            state = _read_status_state(tdir)
            if code != 0:
                if state != "failed":
                    write_status(tdir, state="failed", step="run", exit_code=code,
                                 error=tip or f"run_task.py 退出码 {code}",
                                 runner=runner, finished_at=now_iso())
            elif state in ("", "created", "running"):
                if signature:
                    state, step = "signed", "signed"
                elif isinstance(report, dict):
                    state, step = "awaiting_sign", "sign"
                else:
                    state, step = "assembled", "assemble"
                write_status(tdir, state=state, step=step, exit_code=0,
                             error=None, runner=runner, finished_at=now_iso())
            elif runner:
                write_status(tdir, runner=runner)
            if isinstance(report, dict) and "is_demo" in report:
                write_status(tdir, is_demo=report.get("is_demo"))
        log(f"run done {task_id}: exit={code} state={_read_status_state(tdir)}")
    except ApiError as exc:
        _fail_task(cfg, tdir, exc.message)
        log(f"run rejected {task_id}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        log(traceback.format_exc())
        _fail_task(cfg, tdir, f"内部错误: {exc}")
    finally:
        with cfg.running_lock:
            cfg.running.discard(task_id)


# --------------------------------------------------------------------------- #
# 动作：run / sign / finalize
# --------------------------------------------------------------------------- #
RUNNERS = ("fixture", "external", "dsh")


def demo_result_path(cfg):
    path = cfg.runs / DEMO_RESULT
    if not path.is_file():
        raise ApiError(409, f"runner=fixture 需要 demo 数据源但不存在: {path}")
    return path


def prepare_fixture_result(cfg, tdir):
    """fixture runner：复制 runs/demo-result-2026-10-19.json -> outdir/result.json。"""
    source = demo_result_path(cfg)
    doc = read_json(source)
    required = ("snapshot", "factorTable", "attribution", "advice")
    if not isinstance(doc, dict) or any(key not in doc for key in required):
        raise ApiError(500, f"demo 数据源结构不合法（需要 {required}）: {source}")
    write_json_atomic(Path(tdir) / "result.json", doc)
    return source


def post_run(cfg, task_id, body=None):
    body = body if isinstance(body, dict) else {}
    tdir = task_dir(cfg, task_id)
    task = read_json(tdir / "task.json") or {}
    if not isinstance(task, dict):
        task = {}
    raw_runner = body.get("runner") if body.get("runner") not in (None, "") else task.get("runner")
    runner = str(raw_runner or "fixture").strip().lower()
    aliases = {"demo": "fixture", "fixtures": "fixture", "手动": "external", "外部": "external", "wait": "external"}
    runner = aliases.get(runner, runner)
    if runner not in RUNNERS:
        raise ApiError(400, f"非法 runner={runner!r}（可选：{'/'.join(RUNNERS)}）")
    if runner == "dsh":
        raise ApiError(501, "runner=dsh 未实现：M2 实现（DSH headless/SDK 自动驱动）；"
                            "当前可用 runner=fixture（演示）或 runner=external（等待外部执行）")
    with cfg.running_lock:
        if task_id in cfg.running:
            raise ApiError(409, f"任务已在运行: {task_id}")
        cfg.running.add(task_id)
    extra_args = []
    # 服务端 run 只负责「跑到 assembled」；签字/终审走 /sign、/gate、/finalize，
    # 避免不可逆报告在 run_task 内部自签后 finalize 撞 gate 被标 failed（丢掉可审批状态）。
    include_signer = False
    try:
        task["runner"] = runner
        write_json_atomic(tdir / "task.json", task)
        if runner == "fixture":
            source = prepare_fixture_result(cfg, tdir)
            extra_args = ["--demo"]
            include_signer = False
            log(f"fixture result copied: {source.name} -> {tdir / 'result.json'}")
        cmd = build_run_command(cfg, task, tdir, extra_args=extra_args, include_signer=include_signer)
    except ApiError as exc:
        with cfg.running_lock:
            cfg.running.discard(task_id)
        # 未起子进程：run_task 不会写状态，server 落 failed 供 UI 展示
        with cfg.task_lock(task_id):
            write_status(tdir, state="failed", step="run", error=exc.message, runner=runner)
        raise
    if runner == "external":
        with cfg.task_lock(task_id):
            write_status(tdir, state="running", step="waiting_result", runner=runner)
    thread = threading.Thread(
        target=run_task_thread,
        args=(cfg, tdir, task, runner, extra_args, include_signer),
        name=f"run-{task_id}", daemon=True,
    )
    thread.start()
    return 202, {"ok": True, "task_id": task_id, "state": "running", "accepted": True,
                 "runner": runner, "command": cmd, "identity": cfg.identity()}


def _tail_first_line(text, fallback):
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:500]
    return fallback


def post_sign(cfg, task_id, body):
    if not isinstance(body, dict):
        body = {}
    signer = str(body.get("signer") or body.get("confirmer") or "").strip() or cfg.owner
    require_allowed(cfg, signer, "signer")
    tdir = task_dir(cfg, task_id)
    report = tdir / "report.json"
    if not report.is_file():
        raise ApiError(409, f"report.json 不存在，无法签字（先 POST /api/tasks/{task_id}/run）")
    tool = cfg.root / "tools" / "sign_delivery.py"
    if not tool.is_file():
        raise ApiError(503, "tools/sign_delivery.py 不存在（B1 后端尚未落盘）")
    out = tdir / "signature.json"
    cmd = [find_python(cfg), "-B", str(tool), str(report), "--signer", signer, "--out", str(out)]
    # fixture/演示报告允许「练签」，但 core.gates 仍会拒其 finalize（is_demo 不得真实交付）
    report_doc = read_json(report) or {}
    if isinstance(report_doc, dict) and report_doc.get("is_demo") is True:
        cmd.append("--allow-demo")
    proc = subprocess.run(cmd, cwd=str(cfg.root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env(), timeout=120)
    if proc.returncode != 0:
        detail = _tail_first_line(proc.stderr or proc.stdout,
                                  f"sign_delivery.py exit={proc.returncode}")
        raise ApiError(400, detail)
    sig = read_json(out)
    if not isinstance(sig, dict) or not sig.get("report_sha256"):
        sha = ""
        try:
            parsed = json.loads((proc.stdout or "").strip().splitlines()[-1])
            sha = str(parsed.get("sha256") or "")
            if not sig:
                sig = {"report_sha256": sha, "signer": signer}
        except Exception:  # noqa: BLE001
            sha = ""
        if not sha:
            raise ApiError(500, "sign_delivery.py 未产出 report_sha256（契约缺口）")
        sig["report_sha256"] = sha
    sha256 = str(sig.get("report_sha256"))
    with cfg.task_lock(task_id):
        # 签名落盘后仍是「待 approved」：awaiting_sign，等 /finalize 才变 signed
        write_status(tdir, state="awaiting_sign", step="signature_written", signer=signer,
                     signature_sha256=sha256, signed_at=sig.get("signed_at"))
    return 200, {"ok": True, "sha256": sha256, "signer": sig.get("signer") or signer,
                 "signed_at": sig.get("signed_at"), "path": str(out),
                 "signature_file": str(out), "identity": cfg.identity()}


def post_finalize(cfg, task_id):
    tdir = task_dir(cfg, task_id)
    report = tdir / "report.json"
    loop_dir = tdir / "loop"
    sig = signature_path(tdir)
    if not report.is_file():
        raise ApiError(409, f"report.json 不存在: {task_id}")
    if not loop_dir.is_dir():
        raise ApiError(409, f"loop/（四卡）不存在: {task_id}")
    if sig is None:
        raise ApiError(409, "signature.json 不存在，先 POST /api/tasks/<id>/sign")
    tool = cfg.root / "tools" / "finalize_delivery.py"
    if not tool.is_file():
        raise ApiError(503, "tools/finalize_delivery.py 不存在（B1 后端尚未落盘）")
    # 白名单为唯一事实源（--allow-signers，默认 老板,boss），不读 env 兜底（M1.5 spec §2.5）
    allow_signers = ",".join(cfg.allow_signers)
    cmd = [find_python(cfg), "-B", str(tool), "--report", str(report),
           "--loop-dir", str(loop_dir), "--signature", str(sig),
           "--allow-signers", allow_signers]
    proc = subprocess.run(cmd, cwd=str(cfg.root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env(), timeout=120)
    parsed = None
    raw_out = (proc.stdout or "").strip()
    # finalize 拒绝时输出 indent=2 的多行 JSON，成功时也是多行——先整段解析，再退到末行。
    candidates = [raw_out] + ([raw_out.splitlines()[-1]] if raw_out else [])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            break
        except Exception:  # noqa: BLE001
            parsed = None
    if proc.returncode != 0:
        reasons = []
        if isinstance(parsed, dict):
            if parsed.get("error"):
                reasons.append(str(parsed["error"]))
            for key in ("reasons", "failures", "checks", "missing"):
                value = parsed.get(key)
                if isinstance(value, list) and value:
                    reasons.extend(str(item) for item in value)
        message = "; ".join(reasons) or _tail_first_line(
            proc.stderr or proc.stdout, f"finalize_delivery.py exit={proc.returncode}")
        raise ApiError(409, message)
    decision = read_json(loop_dir / "decision.json") or {}
    if not isinstance(decision, dict):
        decision = {}
    with cfg.task_lock(task_id):
        write_status(tdir, state="signed", step="finalized",
                     decision_status=decision.get("status"), approval=decision.get("approval"))
    sha256 = (parsed or {}).get("signature_sha256") if isinstance(parsed, dict) else ""
    if not sha256:
        sha256 = (read_json(sig) or {}).get("report_sha256")
    return 200, {"ok": True, "decision": decision.get("id"), "status": decision.get("status"),
                 "signer": (decision.get("approval") or {}).get("by"),
                 "signature_sha256": sha256, "identity": cfg.identity()}


def require_allowed(cfg, who, label):
    """身份白名单校验（M1.5 spec §2.5）：signer/confirmer 必须在 --allow-signers 内。"""
    name = str(who or "").strip()
    if not name:
        raise ApiError(400, f"缺 {label}")
    if name not in cfg.allow_signers:
        raise ApiError(403, f"{label}={name!r} 不在白名单（允许：{', '.join(cfg.allow_signers)}）；"
                             f"用 --allow-signers 配置")
    return name


def post_gate(cfg, task_id, body):
    """POST /api/tasks/<id>/gate：不可逆动作的人工确认/拒绝（core.gate.confirm/reject）。"""
    if not isinstance(body, dict):
        body = {}
    raw_action = str(body.get("action") or body.get("decision") or "").strip().lower()
    aliases = {
        "approve": "confirm", "confirmed": "confirm", "pass": "confirm", "ok": "confirm",
        "deny": "reject", "rejected": "reject", "refuse": "reject", "no": "reject",
    }
    action = aliases.get(raw_action, raw_action)
    if action not in ("confirm", "reject"):
        raise ApiError(400, "action 必须是 confirm 或 reject")
    confirmer = str(body.get("confirmer") or body.get("signer") or "").strip() or cfg.owner
    require_allowed(cfg, confirmer, "confirmer")
    comment = str(body.get("comment") or body.get("note") or "").strip()
    tdir = task_dir(cfg, task_id)
    report = read_json(tdir / "report.json")
    if not isinstance(report, dict):
        raise ApiError(409, f"report.json 不存在: {task_id}（先 POST /api/tasks/{task_id}/run）")
    module = import_core(cfg, "gate")
    try:
        if action == "confirm":
            gate = module.confirm(str(tdir), confirmer, comment, report)
        else:
            gate = module.reject(str(tdir), confirmer, comment)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError(409, f"securityGate {action} 失败: {exc}")
    if not isinstance(gate, dict):
        gate = read_json(tdir / GATE_FILE)
        if not isinstance(gate, dict):
            gate = {}
    payload = dict(gate)
    payload.setdefault("status", "confirmed" if action == "confirm" else "rejected")
    payload.update({
        "ok": True,
        "task_id": task_id,
        "action": action,
        "gate": gate,
        "identity": cfg.identity(),
    })
    try:
        with cfg.task_lock(task_id):
            write_status(tdir, step=f"gate_{action}", gate=payload.get("status"),
                         gate_confirmer=confirmer,
                         gate_at=payload.get("at") or payload.get("confirmed_at"))
    except Exception:  # noqa: BLE001 - 状态写失败不回滚 gate 本身
        log(traceback.format_exc())
    return 200, payload


# --------------------------------------------------------------------------- #
# 查询端点
# --------------------------------------------------------------------------- #
def health(cfg):
    pack = read_json(cfg.root / "packs" / "manufacturing" / "pack.json") or {}
    if not isinstance(pack, dict):
        pack = {}
    return {
        "ok": True,
        "service": "bot-team-local",
        "pack": pack.get("pack_id") or "manufacturing",
        "version": pack.get("version") or "0.0.0",
        "industry": pack.get("industry") or "制造业",
        "root": str(cfg.root),
        "python": find_python(cfg),
        "pid": os.getpid(),
        "time": now_iso(),
        "owner": cfg.owner,
        "allow_signers": list(cfg.allow_signers),
        "identity": cfg.identity(),
        "runners": ["fixture", "external", "dsh"],
    }


def packs(cfg):
    out = []
    packs_dir = cfg.root / "packs"
    if not packs_dir.is_dir():
        return out
    for path in sorted(packs_dir.glob("*/pack.json")):
        doc = read_json(path)
        if not isinstance(doc, dict):
            continue
        out.append({
            "pack_id": doc.get("pack_id") or path.parent.name,
            "version": doc.get("version"),
            "industry": doc.get("industry"),
            "crew_id": doc.get("crew_id"),
            "path": str(path),
        })
    return out


def _int_param(query, name, default):
    raw = (query.get(name) or [None])[0]
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _since_param(query):
    """?since=n 严格校验：非整数/负数 → 400（SSE 与 JSON 增量同一口径）。"""
    raw = (query.get("since") or [None])[0]
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError(400, f"非法 since={raw!r}（必须是 ≥0 的整数）")
    if value < 0:
        raise ApiError(400, f"非法 since={value}（必须是 ≥0 的整数）")
    return value


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def route_api(cfg, method, path, query, body):
    if path == "/api/health":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, health(cfg)
    if path == "/api/packs":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, packs(cfg)
    if path == "/api/team":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return team_view(cfg)
    if path == "/api/tasks":
        if method == "GET":
            return 200, list_tasks(cfg)
        if method == "POST":
            return 201, create_task(cfg, body)
        raise ApiError(405, "method not allowed")

    match = TASK_ROUTE_RE.match(path)
    if not match:
        raise ApiError(404, f"未知端点: {path}")
    task_id = unquote(match.group(1))
    action = match.group(2) or ""
    if action == "":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, get_task(cfg, task_id)
    if action == "log":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, get_log(cfg, task_id, _int_param(query, "after", 0))
    if action == "events":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, get_events(cfg, task_id, _since_param(query))
    if action == "data":
        if method != "POST":
            raise ApiError(405, "method not allowed")
        return post_upload_data(cfg, task_id, body)
    if action == "gate":
        if method != "POST":
            raise ApiError(405, "method not allowed")
        return post_gate(cfg, task_id, body)
    if action == "delivery":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, delivery_view(cfg, task_id)
    if action == "run":
        if method != "POST":
            raise ApiError(405, "method not allowed")
        return post_run(cfg, task_id, body)
    if action == "sign":
        if method != "POST":
            raise ApiError(405, "method not allowed")
        return post_sign(cfg, task_id, body)
    if action == "finalize":
        if method != "POST":
            raise ApiError(405, "method not allowed")
        return post_finalize(cfg, task_id)
    raise ApiError(404, f"未知端点: {path}")


class BotHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bot-team-local/0.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_HEAD(self):  # noqa: N802
        self._handle("HEAD")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self):  # noqa: N802
        self._send_json(204, {})

    # -- 内部 -------------------------------------------------------------- #
    def _handle(self, method):
        cfg = self.server.cfg
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "/")
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if method not in ("GET", "POST"):
                    raise ApiError(405, "method not allowed")
                stream = str((query.get("stream") or [""])[0]).strip().lower()
                if path.startswith("/api/tasks/") and path.endswith("/events") and stream in ("1", "true", "yes"):
                    if method not in ("GET", "HEAD"):
                        raise ApiError(405, "method not allowed")
                    self._handle_sse(cfg, path, query)
                    return
                body = self._read_body() if method == "POST" else {}
                status, payload = route_api(cfg, method, path, query, body)
                self._send_json(status, payload)
            else:
                if method not in ("GET", "HEAD"):
                    raise ApiError(405, "method not allowed")
                self._send_static(cfg, path, head=(method == "HEAD"))
        except ApiError as exc:
            payload = {"ok": False, "error": exc.message}
            payload.update(exc.extra)
            self._send_json(exc.status, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            try:
                self._send_json(500, {"ok": False, "error": f"internal error: {exc}"})
            except Exception:  # noqa: BLE001
                pass

    def _handle_sse(self, cfg, path, query):
        """SSE 事件流：id: <seq> / data: <json> / 15s ": ping"；支持 since 续传。"""
        match = TASK_ROUTE_RE.match(path)
        if not match:
            raise ApiError(404, f"未知端点: {path}")
        task_id = unquote(match.group(1))
        tdir = task_dir(cfg, task_id)
        since = _since_param(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        last = since
        last_ping = time.time()
        try:
            # 首帧：注释行让客户端立刻确认连接已建立
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                for event in read_events(cfg, tdir / "run.log", last):
                    frame = ("id: {}\ndata: {}\n\n".format(
                        event["seq"], json.dumps(event, ensure_ascii=False)))
                    self.wfile.write(frame.encode("utf-8"))
                    last = event["seq"]
                self.wfile.flush()
                if time.time() - last_ping >= SSE_PING_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(SSE_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            log(f"sse closed {task_id}: last_seq={last}")

    def _read_body(self):
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "非法 Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"body 过大（上限 {MAX_BODY_BYTES} bytes）")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(400, f"非法 JSON body: {exc}")

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_static(self, cfg, path, head=False):
        ui = cfg.ui.resolve()
        if path in ("/", "/index.html"):
            rel = "index.html"
        elif path.startswith("/ui/"):
            rel = path[len("/ui/"):]
        else:
            raise ApiError(404, f"not found: {path}")
        rel = rel.replace("\\", "/").lstrip("/")
        target = (ui / rel).resolve()
        try:
            target.relative_to(ui)
        except ValueError:
            raise ApiError(404, f"not found: {path}")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise ApiError(404, f"not found: {path}")
        data = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(data)


class BotHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, cfg):
        self.cfg = cfg
        super().__init__(address, BotHandler)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="bot-team 本地服务（M1 §2.6 / M1.5 §2.5）")
    parser.add_argument("--host", default=os.environ.get("BOT_HOST") or DEFAULT_HOST,
                        help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("BOT_PORT") or DEFAULT_PORT),
                        help="端口（默认 8787）")
    parser.add_argument("--root", default=os.environ.get("BOT_ROOT") or str(DEFAULT_ROOT),
                        help="代码根 $BT（默认 server/ 的上一级）")
    parser.add_argument("--owner", default=os.environ.get("BOT_OWNER") or DEFAULT_OWNER,
                        help=f"工作台所有者/默认签字人（默认 {DEFAULT_OWNER}）")
    parser.add_argument("--allow-signers", default=DEFAULT_ALLOW_SIGNERS,
                        help=f"签字人/确认人白名单，逗号分隔（默认 {DEFAULT_ALLOW_SIGNERS}）")
    return parser.parse_args(argv)


def main(argv=None):
    _enable_utf8_stdio()
    args = parse_args(argv)
    cfg = Config(Path(args.root), owner=args.owner, allow_signers=args.allow_signers)
    if not cfg.ui.is_dir():
        log(f"[warn] ui/ 不存在: {cfg.ui}")
    if not (cfg.root / "tools").is_dir():
        log("[warn] tools/ 不存在：B1 后端未落盘时 /run 会返回 503")
    try:
        httpd = BotHTTPServer((args.host, args.port), cfg)
    except OSError as exc:
        log(f"监听 {args.host}:{args.port} 失败：{exc}（端口被占用？换 --port 8788 试试）")
        return 2
    log(f"bot-team local server: http://{args.host}:{args.port}/")
    log(f"root={cfg.root}")
    log(f"ui={cfg.ui} runs={cfg.runs}")
    log(f"python={find_python(cfg)}")
    log(f"owner={cfg.owner} allow_signers={','.join(cfg.allow_signers)}")
    log(f"health=http://{args.host}:{args.port}/api/health  （Ctrl+C 停止）")
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在停止…")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
