#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot-team 本地服务（M1 构建规格 §2.6，纯 Python stdlib，无第三方依赖）。

启动（推荐 venv python 绝对路径）：
    "D:\\work\\产品类\\个人产品\\bot-2\\venv\\Scripts\\python.exe" -B server/app.py
    ... --port 8787 --root "D:\\work\\产品类\\个人产品\\bot-2\\JuJing\\bot-team"

端点（M1 spec §2.6 + M1.5 spec §2.5）：
    GET  /                                    302 → /ui/index.html（相对路径的同级 JS 必须在 /ui/ 下才解析得到）
    GET  /ui/<path>                           ui/ 静态资源
    GET  /api/health                          {ok, pack, version, owner, allow_signers, ...}
    GET  /api/packs                           已装行业包列表
    GET  /api/team                            core.team.load_team：employees/crews/shelf/roster/pack
    GET  /api/scenes                          场景种子（core.scene.load_seeds；未就绪回退 packs/<pack>/scenes/*.json）
    POST /api/scenes/derive                   body {prompt?,metric_id?,data_ref?} -> preset / discovered / user_prompt(501)
    GET  /api/tasks                           [{task_id,title,state,updated_at,...}]（扫描 runs/）
    POST /api/tasks                           body=intake JSON，可含 data_files[{name,content[,encoding]}]
    GET  /api/tasks/<id>                      {task,report,status,loop,signature,gate,scene,outcome,outcome_pending,task_id}
    GET  /api/tasks/<id>/delivery             交付卡字段投影（finding/scorecard/roi/decision_log/signature/outcome）
    GET  /api/tasks/<id>/outcome              T+7 回填：{ok,outcome_pending,outcome,decision_log_entry}
    POST /api/tasks/<id>/outcome              body {observed_outcome,actual_action?,override_reason?,action_cost?,increment_value?}
                                              finalized 任务回填（幂等覆盖，写 runs/<id>/outcome.json，签字字节不变；
    GET  /api/tasks/<id>/log?after=<n>        旧增量端点（保留兼容，读 run.log）
    GET  /api/tasks/<id>/events?since=<n>     事件增量 [{seq,ts,phase,role,agent,kind,line}]
    GET  /api/tasks/<id>/events?since=<n>&stream=1
                                              SSE（id: <seq> / data: <json> / 15s ": ping"，断线续传）
    POST /api/tasks/<id>/data                 multipart/JSON 上传表格转 T/T30，或 {files:[...]} 追加数据文件
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
- 表格转 T/T30（B9，POST /api/tasks/<id>/data 的 table 模式）：multipart 或 JSON 上传
  table（.csv/.xlsx/.xls，≤8MB）+ 可选 field_map（YAML，≤64KB）+ line + 四个 ISO 日期；
  server 不 import pandas，子进程调用 tools/ingest_task_data.py（超时 120s），成功后把
  material_refill_T.json / material_refill_T30.json 记入 task.json 的 data_files/data_refs
  （sha256+size 走 core.provenance）。失败统一 4xx/500 单行可读原因，不吐 traceback。

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
from email.parser import BytesParser
from email.policy import default as email_policy
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
TASK_ROUTE_RE = re.compile(r"^/api/tasks/([^/]+)(?:/([a-z][a-z-]*))?$")
SIX_DIMS = ("data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency")
DEFAULT_OWNER = "老板"
DEFAULT_ALLOW_SIGNERS = "老板,boss"
DEMO_RESULT = "demo-result-2026-10-19.json"
MAX_UPLOAD_FILE = 8 * 1024 * 1024          # 单文件 8MB
MAX_UPLOAD_TOTAL = 24 * 1024 * 1024        # 总量 24MB
MAX_BODY_BYTES = 36 * 1024 * 1024          # 24MB 数据 base64 膨胀 4/3 + JSON 余量
MAX_TABLE_BYTES = 8 * 1024 * 1024          # 上传表格（csv/xlsx/xls）单文件 8MB
MAX_FIELD_MAP_BYTES = 64 * 1024            # 字段映射 YAML 文本 64KB
ALLOWED_TABLE_SUFFIXES = (".csv", ".xlsx", ".xls")
INGEST_TIMEOUT_SECONDS = 120               # 表格转 T/T30 子进程超时
INGEST_FILES = ("material_refill_T.json", "material_refill_T30.json")
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


def replace_with_retry(tmp, path, attempts=10, delay=0.05):
    """os.replace 在 Windows 上会因目标文件被并发读句柄占用而报 WinError 5。

    读句柄（另一个线程的 read_json / 另一个进程的 status 轮询）与原子替换撞上时，
    目标文件不带 FILE_SHARE_DELETE，替换被系统拒绝。重试即可稳定落盘。
    """
    last = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp, path)


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
        replace_with_retry(tmp, path)
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


def safe_task_id(cfg, raw):
    """任务号安全校验：优先 core.paths.safe_task_id（spec §2.4），缺失时用 TASK_ID_RE。"""
    text = "" if raw is None else str(raw).strip()
    module = core_paths_module(cfg)
    if module is not None:
        try:
            return str(module.safe_task_id(text))
        except Exception as exc:  # noqa: BLE001 - 统一转 400
            raise ApiError(400, f"非法 task_id: {text!r}（{exc}）")
    if not TASK_ID_RE.match(text):
        raise ApiError(400, f"非法 task_id: {text!r}")
    return text


def _iso_date(value, field):
    """校验 ISO 日期（YYYY-MM-DD；也接受 ISO 时间戳的前 10 位日期部分）。"""
    text = "" if value is None else str(value).strip()
    if not text:
        raise ApiError(400, f"{field} 必填（ISO 日期，例如 2026-10-05）")
    candidate = text[:10] if len(text) >= 10 else text
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, f"{field} 不是合法 ISO 日期: {text!r}（示例 2026-10-05）")
    return parsed.strftime("%Y-%m-%d")


def _first_line(text, limit=300):
    for raw in str(text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line:
            return line[:limit]
    return ""


def _first_json_object(text):
    """从子进程输出中提取第一个完整 JSON 对象（忽略前缀日志行）。"""
    src = str(text or "")
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(src):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(src[idx:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _basename(raw, label):
    """取文件名 basename（浏览器/JSON 可能带路径，禁止借路径穿越）。"""
    text = str(raw or "").replace("\\", "/").strip()
    name = text.rsplit("/", 1)[-1].strip()
    if not name:
        raise ApiError(400, f"{label} 缺少文件名")
    return name


def _upload_payload(item, label):
    """解析上传项 → (原始文件名, 文件字节)。

    支持三种形态：multipart 解析结果 {name, content(bytes)}、
    JSON {name/filename, content: str, encoding: base64|text}、纯 base64 字符串。
    """
    if isinstance(item, dict):
        raw_name = ""
        for key in ("name", "filename", "path", "file", "table_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                raw_name = value.strip()
                break
        content = item.get("content")
        if isinstance(content, (bytes, bytearray)):
            data = bytes(content)
        elif isinstance(content, str):
            encoding = str(item.get("encoding") or ("base64" if item.get("base64") else "text")).strip().lower()
            if encoding in ("base64", "b64"):
                try:
                    data = base64.b64decode(content, validate=True)
                except (ValueError, TypeError):
                    raise ApiError(400, f"{label} base64 解码失败")
            elif encoding in ("text", "utf-8", "utf8", "raw"):
                data = content.encode("utf-8")
            else:
                raise ApiError(400, f"{label} encoding 只支持 base64/text，收到 {encoding!r}")
        else:
            raise ApiError(400, f"{label} content 必须是文件字节或字符串")
        return raw_name, data
    if isinstance(item, str) and item.strip():
        try:
            return "", base64.b64decode(item.strip(), validate=True)
        except (ValueError, TypeError):
            raise ApiError(400, f"{label} 字符串必须是 base64；或改用 JSON 对象 {{name,content,encoding}}")
    raise ApiError(400, f"{label} 缺失或类型不对")


def _table_upload(cfg, body):
    """解析并校验 table 上传项 → (safe_relative 文件名, 字节)。"""
    name, data = _upload_payload(body.get("table"), "table")
    if not name:
        fallback = body.get("table_name") or body.get("filename") or ""
        if isinstance(fallback, str) and fallback.strip():
            name = _basename(fallback, "table")
    if not name:
        raise ApiError(400, "table 需要文件名（.csv/.xlsx/.xls）：multipart 的 filename 或 JSON 的 name/table_name")
    name = _basename(name, "table")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_TABLE_SUFFIXES:
        raise ApiError(400, f"table 只支持 {'/'.join(ALLOWED_TABLE_SUFFIXES)}，收到 {name!r}")
    if not data:
        raise ApiError(400, f"table 文件为空: {name!r}")
    if len(data) > MAX_TABLE_BYTES:
        raise ApiError(400, f"table 超过 {MAX_TABLE_BYTES // (1024 * 1024)}MB 限制（实际 {len(data)} bytes）")
    return safe_relative(cfg, name), data


def _field_map_upload(cfg, body):
    """解析并校验 field_map（YAML 文本或文件）→ (safe_relative 文件名, 字节) 或 (None, None)。"""
    item = body.get("field_map")
    if item in (None, ""):
        return None, None
    if isinstance(item, str):
        data = item.encode("utf-8")
        name = "field-map.yaml"
    else:
        name, data = _upload_payload(item, "field_map")
        if name:
            suffix = Path(_basename(name, "field_map")).suffix.lower()
            if suffix not in (".yaml", ".yml"):
                raise ApiError(400, f"field_map 只支持 .yaml/.yml，收到 {name!r}")
        # 固定落盘名，避免多次上传堆叠
        name = "field-map.yaml"
    if not data or not data.strip():
        raise ApiError(400, "field_map 为空")
    if len(data) > MAX_FIELD_MAP_BYTES:
        raise ApiError(400, f"field_map 超过 {MAX_FIELD_MAP_BYTES // 1024}KB 限制（实际 {len(data)} bytes）")
    return safe_relative(cfg, name), data


_UPLOAD_TABLE_KEYS = ("table", "csv", "file", "input_csv", "data_csv")
_UPLOAD_FIELD_MAP_KEYS = ("field_map", "field-map", "fieldmap", "mapping", "mappings")


def _window_dates(value, label):
    """解析窗口参数（JSON 文本 / 对象 / `a~b` 文本）→ (start, end)；缺失返回 ("", "")。"""
    if value in (None, ""):
        return "", ""
    if isinstance(value, dict):
        start = value.get("start") or value.get("from") or value.get("t_start")
        end = value.get("end") or value.get("to") or value.get("t_end")
        return str(start or "").strip(), str(end or "").strip()
    text = str(value).strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ApiError(400, f"{label} 不是合法 JSON 对象: {text[:80]!r}")
        if not isinstance(parsed, dict):
            raise ApiError(400, f"{label} 必须是对象（start/end）: {text[:80]!r}")
        return _window_dates(parsed, label)
    if "~" in text:
        start, end = text.split("~", 1)
        return start.strip(), end.strip()
    raise ApiError(400, f"{label} 格式无法识别：传 JSON 对象 {{start,end}} 或 `start~end`")


def _normalize_upload_body(body):
    """把上游 workbench/UI 的多种上传字段名归一成 M1.5 契约。

    兼容：table ← csv/file/input_csv/data_csv；field_map ← field-map/mapping；
    t_start/t_end ← start/end 或 window{start,end}；base_start/base_end ← baseline_window{...}。
    只做别名映射（canonical 字段优先）；没有表格字段的 body 原样返回（走旧 files 路径）。
    """
    if not isinstance(body, dict):
        return body
    out = dict(body)

    def _first(keys):
        for key in keys:
            value = out.get(key)
            if value not in (None, ""):
                return value
        return None

    table = _first(_UPLOAD_TABLE_KEYS)
    if table is None:
        return out
    out["table"] = table
    field_map = _first(_UPLOAD_FIELD_MAP_KEYS)
    if field_map is not None:
        out["field_map"] = field_map
    if out.get("line") in (None, ""):
        for key in ("production_line", "产线"):
            if out.get(key) not in (None, ""):
                out["line"] = out[key]
                break
    t_start, t_end = _window_dates(out.get("window"), "window")
    if out.get("t_start") in (None, ""):
        out["t_start"] = t_start or out.get("start")
    if out.get("t_end") in (None, ""):
        out["t_end"] = t_end or out.get("end")
    base_start, base_end = _window_dates(
        _first(("baseline_window", "base_window", "baseline")), "baseline_window")
    if out.get("base_start") in (None, ""):
        out["base_start"] = base_start
    if out.get("base_end") in (None, ""):
        out["base_end"] = base_end
    return out


def _write_under(cfg, base_dir, rel_name, data):
    """在 base_dir 下安全落盘（防穿越 + 原子写），返回绝对 Path。"""
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    rel = safe_relative(cfg, rel_name)
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ApiError(400, f"上传文件路径越界: {rel_name!r}")
    if target.exists() and target.is_dir():
        raise ApiError(400, f"上传文件路径是目录: {rel_name!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(target, data)
    return target


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


class RawBody:
    """route_api 的非 JSON 响应体（导出/打印端点用）：原样字节 + Content-Type。"""

    def __init__(self, body, content_type="text/html; charset=utf-8"):
        self.body = str(body if body is not None else "")
        self.content_type = str(content_type or "text/html; charset=utf-8")


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


def signature_stale(tdir):
    """当前 signature.json 的指纹是否已与 report.json 现字节脱钩。

    securityGate 人工确认会回写 report.json（core/gate.py::_apply_to_report），
    报告字节一变 → 先前签下的指纹失效 → finalize 报 signature hash mismatch。
    此处如实暴露「签已失效」，供 UI 放开「重新签字」，避免用户卡在无解状态。
    """
    sig = signature_doc(tdir)
    if not isinstance(sig, dict):
        return False
    signed = str(sig.get("report_sha256") or "")
    if not signed:
        return False
    report_file = Path(tdir) / "report.json"
    if not report_file.is_file():
        return False
    try:
        return sha256_file(report_file) != signed
    except OSError:
        return False


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
        # 显式任务号必须符合 core.paths.safe_task_id 契约（T-YYYYMMDD-xxx），
        # 不做宽松兜底：避免「T-2026」之类半成品 ID 或路径穿越写进 runs/。
        explicit = safe_task_id(cfg, explicit)
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


def _json_record_count(path):
    doc = read_json(path)
    records = doc.get("records") if isinstance(doc, dict) else None
    return len(records) if isinstance(records, list) else 0


def ingest_table(cfg, task_id, body):
    """表格模式：上传 .csv/.xlsx/.xls → ingest_task_data.py 子进程转 T/T30 → 记账。

    不 import pandas（venv 无 pandas）：转换全部交给 tools/ingest_task_data.py。
    """
    task_id = safe_task_id(cfg, task_id)
    tdir = task_dir(cfg, task_id)
    status = read_status(tdir)
    if str(status.get("state") or "") == "running":
        raise ApiError(409, "任务正在运行，暂不能替换取数文件（等运行结束再上传）")
    rel_table, table_bytes = _table_upload(cfg, body)
    dates = {
        "t_start": _iso_date(body.get("t_start"), "t_start"),
        "t_end": _iso_date(body.get("t_end"), "t_end"),
        "base_start": _iso_date(body.get("base_start"), "base_start"),
        "base_end": _iso_date(body.get("base_end"), "base_end"),
    }
    if dates["t_start"] > dates["t_end"]:
        raise ApiError(400, f"T 窗口起点晚于终点：{dates['t_start']} > {dates['t_end']}")
    if dates["base_start"] > dates["base_end"]:
        raise ApiError(400, f"T30 基线窗口起点晚于终点：{dates['base_start']} > {dates['base_end']}")
    task_doc = read_json(Path(tdir) / "task.json") or {}
    if not isinstance(task_doc, dict):
        task_doc = {}
    intake_doc = task_doc.get("intake")
    intake_doc = intake_doc if isinstance(intake_doc, dict) else {}
    # 上传时未显式给 line 时沿用任务自身产线（先 task.json 顶层，再 intake）：
    # 表格里可能同时含多条产线，不指定产线会混入其他产线造成重复日期。
    line = body.get("line")
    if line is None or str(line).strip() == "":
        line = task_doc.get("line") or intake_doc.get("line") or ""
    line = str(line).strip()
    if not line:
        raise ApiError(400, "line 必填：上传时传 line，或在创建任务时指定产线")
    if len(line) > 64 or any(ord(ch) < 0x20 for ch in line):
        raise ApiError(400, "line 非法（不允许控制字符，最长 64 字符）")
    data_dir = Path(tdir) / "data"
    table_path = _write_under(cfg, data_dir, rel_table, table_bytes)
    rel_field_map, field_map_bytes = _field_map_upload(cfg, body)
    field_map_path = None
    if rel_field_map:
        field_map_path = _write_under(cfg, data_dir, rel_field_map, field_map_bytes)
    ingest_tool = Path(cfg.root) / "tools" / "ingest_task_data.py"
    if not ingest_tool.is_file():
        raise ApiError(500, f"缺少取数工具: {ingest_tool}")
    cmd = [find_python(cfg), "-B", str(ingest_tool),
           "--input", str(table_path), "--line", line,
           "--t-start", dates["t_start"], "--t-end", dates["t_end"],
           "--base-start", dates["base_start"], "--base-end", dates["base_end"],
           "--outdir", str(data_dir)]
    if field_map_path:
        cmd += ["--field-map", str(field_map_path)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cfg.root), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=INGEST_TIMEOUT_SECONDS, env=child_env(),
        )
    except subprocess.TimeoutExpired:
        raise ApiError(500, f"表格转 T/T30 超时（{INGEST_TIMEOUT_SECONDS}s）：ingest_task_data.py 未在限时内完成")
    except OSError as exc:
        raise ApiError(500, f"表格转 T/T30 子进程启动失败：{exc}")
    doc = _first_json_object(proc.stdout or "") or _first_json_object(proc.stderr or "")
    if proc.returncode != 0:
        message = ""
        if isinstance(doc, dict):
            message = str(doc.get("error") or doc.get("message") or "").strip()
        message = message or _first_line(proc.stderr) or _first_line(proc.stdout) or "ingest_task_data 返回非 0"
        # 数据级错误（缺列/窗口空/文件损坏）→ 400；工具/环境错误 → 500
        internal = not isinstance(doc, dict) or doc.get("ok") is not False
        if isinstance(doc, dict) and str(doc.get("missing") or "") == "pandas":
            internal = True
        raise ApiError(500 if internal else 400, f"表格转 T/T30 失败：{_first_line(message, 400)}")
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        raise ApiError(500, f"ingest_task_data 输出无法解析：{_first_line(proc.stdout) or '空输出'}")
    missing = [name for name in INGEST_FILES if not (data_dir / name).is_file()]
    if missing:
        raise ApiError(500, "ingest_task_data 未产出预期文件：" + ", ".join(missing))
    t_paths = [data_dir / name for name in INGEST_FILES]
    try:
        t_records = int(doc.get("t_records"))
        t30_records = int(doc.get("t30_records"))
    except (TypeError, ValueError):
        t_records = _json_record_count(t_paths[0])
        t30_records = _json_record_count(t_paths[1])
    provenance = import_core(cfg, "provenance")
    try:
        refs = provenance.refs_record([str(path) for path in t_paths], str(cfg.root))
    except Exception as exc:  # noqa: BLE001 - 文档/审计记录失败不掩盖主流程
        raise ApiError(500, f"计算数据文件 sha256 失败（core.provenance）：{exc}")
    new_paths = {str(ref.get("path")) for ref in refs}
    with cfg.task_lock(task_id):
        task = read_json(Path(tdir) / "task.json") or {}
        if not isinstance(task, dict):
            task = {}
        data_files = [str(entry) for entry in (task.get("data_files") or []) if isinstance(entry, str)]
        for ref in refs:
            rel = str(ref.get("path"))
            if rel not in data_files:
                data_files.append(rel)
        manifest = [entry for entry in (task.get("data_manifest") or []) if isinstance(entry, dict)]
        manifest = [entry for entry in manifest if entry.get("path") not in new_paths]
        for ref in refs:
            manifest.append({
                "name": Path(str(ref.get("path"))).name,
                "path": str(ref.get("path")),
                "sha256": ref.get("sha256"),
                "size": ref.get("size"),
            })
        old_refs = [entry for entry in (task.get("data_refs") or []) if isinstance(entry, dict)]
        merged_refs = [entry for entry in old_refs if entry.get("path") not in new_paths] + list(refs)
        root = Path(cfg.root).resolve()
        task["data_files"] = data_files
        task["data_manifest"] = manifest
        task["data_refs"] = merged_refs
        task["table_upload"] = {
            "name": rel_table,
            "path": table_path.resolve().relative_to(root).as_posix(),
            "sha256": sha256_bytes(table_bytes),
            "size": len(table_bytes),
            "line": line,
            **dates,
        }
        if field_map_path:
            task["field_map"] = field_map_path.resolve().relative_to(root).as_posix()
        task["updated_at"] = now_iso()
        write_json_atomic(Path(tdir) / "task.json", task)
        status_task = read_status(tdir).get("task")
        status_task = dict(status_task) if isinstance(status_task, dict) else {}
        status_task.update({"data_ready": True, "data_files": data_files,
                            "data_refs": merged_refs, "data_manifest": manifest})
        write_status(tdir, task=status_task, data_ready=True)
        status = read_status(tdir)
    return 200, {
        "ok": True,
        "task_id": task_id,
        "t_records": t_records,
        "t30_records": t30_records,
        "files": refs,
        "data_files": data_files,
        "data_refs": merged_refs,
        "data_ready": True,
        "table": task.get("table_upload"),
        "field_map": task.get("field_map"),
        "status": status,
        "identity": cfg.identity(),
    }


def post_upload_data(cfg, task_id, body):
    """POST /api/tasks/<id>/data：表格转 T/T30（table 模式）或追加 JSON 数据文件（兼容）。"""
    tdir = task_dir(cfg, task_id)
    if not isinstance(body, dict):
        raise ApiError(400, "body 必须是 JSON 对象或 multipart/form-data 表单")
    body = _normalize_upload_body(body)
    if body.get("table") not in (None, ""):
        return ingest_table(cfg, task_id, body)
    raw = body.get("files")
    if raw is None:
        raw = body.get("data_files")
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "body.files 必须是非空数组（{name,content[,encoding]}）；上传表格请用 multipart 的 table 字段")
    uploads, _kept = _upload_items(cfg, {"data_files": raw}, "data_files")
    if not uploads:
        raise ApiError(400, "body.files 内没有可落盘的上传项")
    files, data_files = apply_uploads(cfg, tdir, task_id, uploads)
    status = read_status(tdir)
    return 200, {"ok": True, "task_id": task_id, "files": files,
                 "data_files": data_files, "data_ready": True,
                 "status": status, "identity": cfg.identity()}


# --------------------------------------------------------------------------- #
# B17：T+7 回填（POST /api/tasks/<id>/outcome，设计文档 §3.3 / §4 批次 B17）
#   - 只接受 finalized 任务（status.state=signed 或 loop/decision.status=approved）
#   - 幂等覆盖：重复提交覆盖 outcome.json，history 保留最近 20 次
#   - 落盘 runs/<id>/outcome.json（原子写）；run.log 追加 kind=status / phase=followup 事件
#   - 已签字 report.json 字节不动（保签字哈希与 A6）；回填字段通过 decision_log_view
#     合并进 GET /api/tasks/<id> 与 /delivery 的 decision_log 视图
# --------------------------------------------------------------------------- #
OUTCOME_FILE = "outcome.json"
OUTCOME_FIELDS = ("observed_outcome", "actual_action", "override_reason",
                  "action_cost", "increment_value", "outcome_window")
_OUTCOME_HISTORY_MAX = 20


def outcome_path(tdir):
    return Path(tdir) / OUTCOME_FILE


def read_outcome(tdir):
    doc = read_json(outcome_path(tdir))
    return doc if isinstance(doc, dict) else None


def task_finalized(tdir):
    """finalized 判定：status.state=signed 优先，其次 loop/decision.json status=approved。"""
    status = read_status(tdir)
    if str(status.get("state") or "") == "signed":
        return True
    decision = (read_loop(tdir) or {}).get("decision")
    return isinstance(decision, dict) and str(decision.get("status") or "") == "approved"


def decision_log_view(report, outcome=None):
    """report.decision_log_entry + T+7 回填（有值才覆盖）→ 合并视图；不改 report 文件。

    B17：回填字段全部可选、有值才写，不写 null 占位。合并视图只用于 POST /outcome 的响应回显；
    GET（任务详情/交付卡）仍返回 report.decision_log_entry 原值，回填单独放 outcome 字段。
    """
    entry = dict((report or {}).get("decision_log_entry") or {})
    if isinstance(outcome, dict):
        for key in OUTCOME_FIELDS:
            value = outcome.get(key)
            if value in (None, "", [], {}):
                continue
            entry[key] = value
    return entry


def append_followup_event(cfg, tdir, outcome, outcome_sha256=""):
    """outcome 提交 → run.log 追一条 kind='status' / phase='followup' 审计事件（core.events）。"""
    module = import_core(cfg, "events")
    meta = {"task_id": Path(tdir).name,
            "report_sha256": outcome.get("report_sha256"),
            "observed_outcome": outcome.get("observed_outcome")}
    if outcome_sha256:
        meta["outcome_sha256"] = outcome_sha256
    return module.append_event(str(Path(tdir) / "run.log"), {
        "phase": "followup",
        "role": "host",
        "kind": "status",
        "line": "T+7 回填：observed_outcome 已记入 outcome.json（签字 report 字节不变）",
        "meta": meta,
    })


def post_outcome(cfg, task_id, body):
    """POST /api/tasks/<id>/outcome：T+7 实测回填（幂等覆盖，重复提交 200）。"""
    tdir = task_dir(cfg, task_id)
    report_file = tdir / "report.json"
    report = read_json(report_file)
    if not isinstance(report, dict):
        raise ApiError(409, f"report.json 不存在: {task_id}（先 POST /api/tasks/{task_id}/run）")
    if not task_finalized(tdir):
        raise ApiError(409, f"任务尚未 finalized（先 POST /api/tasks/{task_id}/finalize）: {task_id}")
    contract = import_core(cfg, "report_contract")
    validator = getattr(contract, "validate_outcome", None)
    if not callable(validator):
        raise ApiError(500, "core.report_contract.validate_outcome 不可用（B17 契约缺失）")
    try:
        clean, errors = validator(body if isinstance(body, dict) else {})
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 校验器异常按 500 暴露，不吐 traceback
        raise ApiError(500, f"outcome 校验失败: {exc}")
    if errors:
        raise ApiError(400, "；".join(str(item) for item in errors))
    report_sha = sha256_file(report_file)
    previous = read_outcome(tdir)
    history = list(previous.get("history") or []) if previous else []
    if previous:
        snapshot = {}
        for key in ("submitted_at", "report_sha256", "sha256") + OUTCOME_FIELDS:
            value = previous.get(key)
            if value not in (None, "", [], {}):
                snapshot[key] = value
        history.append(snapshot)
        history = history[-_OUTCOME_HISTORY_MAX:]
    now = now_iso()
    record = {
        "ok": True,
        "task_id": task_id,
        "submitted_at": now,
        "submitted_by": cfg.owner,
        "report_sha256": report_sha,
        "decision_id": (report.get("decision_log_entry") or {}).get("id"),
        "decision_log_entry": decision_log_view(report, clean),
    }
    for key in OUTCOME_FIELDS:
        if key in clean:
            record[key] = clean[key]
    if history:
        record["history"] = history
    data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    write_bytes_atomic(outcome_path(tdir), data)
    sha256 = sha256_bytes(data)
    with cfg.task_lock(task_id):
        write_status(tdir, step="outcome", outcome_submitted_at=now,
                     outcome_sha256=sha256, outcome_report_sha256=report_sha)
    event_seq = None
    try:
        event_seq = append_followup_event(cfg, tdir, record, sha256)
    except Exception as exc:  # noqa: BLE001 - 审计事件失败不回滚回填（handoff 记录）
        log(f"outcome 事件落 run.log 失败: {exc}")
    record["sha256"] = sha256
    if event_seq is not None:
        record["event_seq"] = event_seq
    record["identity"] = cfg.identity()
    return 200, record


def get_outcome(cfg, task_id):
    """GET /api/tasks/<id>/outcome：回填记录 + 报告原 decision_log_entry（未回填时 outcome=null）。"""
    tdir = task_dir(cfg, task_id)
    report = read_json(tdir / "report.json")
    if not isinstance(report, dict):
        raise ApiError(404, f"report.json 不存在: {task_id}")
    outcome = read_outcome(tdir)
    return {
        "ok": True,
        "task_id": task_id,
        "outcome_pending": outcome is None,
        "outcome": outcome,
        "decision_log_entry": report.get("decision_log_entry") or {},
    }


# --------------------------------------------------------------------------- #
# R4：GET /api/decisions —— 跨任务决策日志时间线（P3 决策日志页）
#   事实源：runs/*/report.json（机器真值）+ runs/*/outcome.json（T+7 回填）。
#   不新建事实源：合并视图复用 decision_log_view()，回填读盘复用 read_outcome()。
#   筛选维度（设计文档 §3.2 P3）：场景 / 实体 / 结论 / 档位 / 是否覆盖（人工改判），
#   另加「是否已回填」——P3 的主字段是「实际结果（T+7 回填状态）」，
#   没有这一维就筛不出「待回看」的卡片。
# --------------------------------------------------------------------------- #
DECISION_DEFAULT_LIMIT = 100
DECISION_MAX_LIMIT = 500


def _decision_entry(cfg, tdir):
    """时间线一条：报告决策 + T+7 回填（只读，不改写任何落盘文件）。"""
    report = read_json(tdir / "report.json")
    if not isinstance(report, dict):
        return None
    dle = report.get("decision_log_entry")
    if not isinstance(dle, dict) or not str(dle.get("id") or "").strip():
        return None
    task = read_json(tdir / "task.json")
    task = task if isinstance(task, dict) else {}
    raw_outcome = read_outcome(tdir)
    outcome = raw_outcome if isinstance(raw_outcome, dict) else {}
    merged = decision_log_view(report, outcome)
    scorecard = report.get("scorecard") if isinstance(report.get("scorecard"), dict) else {}
    findings = report.get("finding") if isinstance(report.get("finding"), list) else []
    parts = []
    for row in findings[:2]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("factor") or row.get("name") or "").strip()
        if not name:
            continue
        conf = row.get("confidence")
        parts.append(name + (f"（置信度 {conf}）" if conf not in (None, "") else ""))
    rec = report.get("recommendation") if isinstance(report.get("recommendation"), list) else []
    actions = [str(x.get("action") or "").strip()
               for x in rec if isinstance(x, dict) and str(x.get("action") or "").strip()]
    scope = task.get("entity_scope")
    if isinstance(scope, dict):
        entity = str(scope.get("name") or scope.get("id") or "")
    elif scope not in (None, ""):
        entity = str(scope)
    else:
        entity = str(task.get("line") or "")
    scene_id = str(task.get("scene_id") or "")
    if not scene_id:
        ref = task.get("scene_ref")
        if isinstance(ref, dict):
            scene_id = str(ref.get("scene_id") or "")
    signature = signature_doc(tdir) or {}
    # 回填字段只取 outcome.json 里的实际值（不写 null 占位）；outcome_window 缺时
    # 退回报告里规划的窗口，好让「待回看」的卡也能显示回看期。
    outcome_fields = {}
    for key in ("observed_outcome", "actual_action", "override_reason",
                "action_cost", "increment_value", "outcome_window"):
        value = outcome.get(key)
        if value in (None, "", [], {}):
            continue
        outcome_fields[key] = value
    if "outcome_window" not in outcome_fields and merged.get("outcome_window"):
        outcome_fields["outcome_window"] = merged["outcome_window"]
    return {
        "task_id": str(task.get("task_id") or report.get("task_id") or Path(tdir).name),
        "decision_id": str(dle.get("id") or ""),
        "decided_at": str(dle.get("date") or report.get("generated_at") or ""),
        "signed_at": str(signature.get("signed_at") or ""),
        "actor": str(dle.get("actor") or ""),
        "scene_id": scene_id,
        "entity": entity,
        "goal": str(report.get("goal") or ""),
        "context": str(dle.get("context") or ""),
        "expected_result": str(dle.get("expected_result") or ""),
        "conclusion": " / ".join(parts),
        "finding_count": len(findings),
        "cannot_attribute_count": len(report.get("cannot_attribute") or []),
        "actions": actions[:3],
        "verdict": str(scorecard.get("verdict") or ""),
        "verdict_label": str(scorecard.get("label") or ""),
        "score_total": scorecard.get("total"),
        "signed": bool(signature.get("report_sha256")),
        "signer": str(signature.get("signer") or ""),
        "report_sha256": str(signature.get("report_sha256") or sha256_file(tdir / "report.json")),
        "is_demo": bool(report.get("is_demo")),
        "outcome_pending": raw_outcome is None,
        # 人话状态标记：设计文档 §3.5 U4「未回填卡片标记『待回看』」——机判口径。
        "outcome_status": "待回看" if raw_outcome is None else "已回填",
        "overridden": bool(str(outcome_fields.get("override_reason") or "").strip()),
        "outcome": outcome_fields,
        "data_ref_count": len(dle.get("data_refs") or []),
    }


def decisions_view(cfg, query):
    """GET /api/decisions：跨任务决策时间线 + 筛选 + 候选值（facets）。"""
    items = []
    if cfg.runs.is_dir():
        for child in cfg.runs.iterdir():
            if not child.is_dir():
                continue
            entry = _decision_entry(cfg, child)
            if entry:
                items.append(entry)
    items.sort(key=lambda row: (str(row.get("decided_at") or ""),
                                str(row.get("signed_at") or ""),
                                str(row.get("task_id") or "")), reverse=True)
    scenes, entities, verdicts = [], [], []
    for row in items:
        for bucket, value in ((scenes, row.get("scene_id")),
                              (entities, row.get("entity")),
                              (verdicts, row.get("verdict"))):
            value = str(value or "")
            if value and value not in bucket:
                bucket.append(value)
    scene = _str_param(query, "scene")
    entity = _str_param(query, "entity")
    verdict = _str_param(query, "verdict")
    conclusion = _str_param(query, "conclusion").lower()
    overridden = _str_param(query, "overridden")
    filled = _str_param(query, "filled")

    def keep(row):
        if scene and str(row.get("scene_id") or "") != scene:
            return False
        if entity and entity.lower() not in str(row.get("entity") or "").lower():
            return False
        if verdict and str(row.get("verdict") or "") != verdict:
            return False
        if conclusion and conclusion not in str(row.get("conclusion") or "").lower():
            return False
        if overridden in ("0", "1") and bool(row.get("overridden")) != (overridden == "1"):
            return False
        if filled == "1" and row.get("outcome_pending"):
            return False
        if filled == "0" and not row.get("outcome_pending"):
            return False
        return True

    filtered = [row for row in items if keep(row)]
    limit = _int_param(query, "limit", DECISION_DEFAULT_LIMIT)
    limit = max(1, min(limit, DECISION_MAX_LIMIT))
    return {
        "ok": True,
        "count": len(filtered[:limit]),
        "total": len(items),
        "matched": len(filtered),
        "generated_at": now_iso(),
        "decisions": filtered[:limit],
        "facets": {
            "scenes": sorted(scenes),
            "entities": sorted(entities),
            "verdicts": sorted(verdicts),
        },
    }


def reconcile_sign_state(cfg, tdir):
    """按产物推导签字终态（与 run_task_thread「按产物推导终态」同一兜底血脉）。

    适用：直接跑 CLI 收口（finalize_delivery.py 早于状态同步补丁）绕过 server，
    签名/决策卡均已落盘但 status.json 停在 awaiting_sign。判据对齐 check_finalize
    的只读子集：签名指纹与现报告一致 + signer 非空 + task_id 一致 +
    存在 approved 决策卡（loop/ 优先，其次 run 根）。满足即把状态推导为
    signed/finalized（写 status.json，带 reconciled 标记）；任何一条不满足都不动。
    """
    status = read_status(tdir)
    if str(status.get("state") or "") == "signed":
        return status
    report = read_json(tdir / "report.json")
    if not isinstance(report, dict):
        return status
    sig = signature_doc(tdir)
    if not isinstance(sig, dict) or not sig.get("report_sha256") or not str(sig.get("signer") or "").strip():
        return status
    report_file = tdir / "report.json"
    try:
        if sha256_file(report_file) != sig.get("report_sha256"):
            return status
    except OSError:
        return status
    if sig.get("task_id") and report.get("task_id") and sig.get("task_id") != report.get("task_id"):
        return status
    approved = None
    for dpath in (tdir / "loop" / "decision.json", tdir / "decision.json"):
        dec = read_json(dpath)
        if isinstance(dec, dict) and str(dec.get("status") or "") == "approved":
            if dec.get("task_id") and report.get("task_id") and dec.get("task_id") != report.get("task_id"):
                continue
            approved = dec
            break
    if approved is None:
        return status
    with cfg.task_lock(tdir.name):
        return write_status(tdir, state="signed", step="finalized",
                            decision_status=approved.get("status"),
                            approval=approved.get("approval"), reconciled=True)


def get_task(cfg, task_id):
    tdir = task_dir(cfg, task_id)
    reconcile_sign_state(cfg, tdir)
    gate = read_json(tdir / GATE_FILE)
    scene_doc = read_json(tdir / "scene.json") if (tdir / "scene.json").is_file() else None
    report = read_json(tdir / "report.json")
    outcome = read_outcome(tdir)
    return {
        "task": read_json(tdir / "task.json"),
        "report": report,
        "status": read_status(tdir),
        "loop": read_loop(tdir),
        "signature": signature_doc(tdir),
        # 签字指纹是否已与报告现字节脱钩（gate 确认回写报告后为 true → 需重新签字）
        "signature_stale": signature_stale(tdir),
        "gate": gate if isinstance(gate, dict) else None,
        # B18：场景实例（runs/<id>/scene.json，含 current_value/因素快照/评分），无则 null
        "scene": scene_doc if isinstance(scene_doc, dict) else None,
        # B17：T+7 回填记录（未回填时 outcome=null）；报告本身仍是唯一事实源
        "outcome": outcome,
        "outcome_pending": outcome is None,
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
    outcome = read_outcome(tdir)
    card = _decision_card(cfg, tdir)
    # B19 裁定：降级不是失败，但降级态呈现层不出建议（report.json 保持机器真值不动）
    degradation = ((card.get("context") or {}).get("degradation")
                   if isinstance(card, dict) else None)
    if degradation:
        recommendation = []
        first = {}
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
        # B17：decision_log 保持报告原值（report 是唯一事实源）；T+7 回填单独暴露 outcome
        "decision_log": report.get("decision_log_entry") or {},
        "outcome": outcome,
        "outcome_pending": outcome is None,
        "signature": signature,
        "signature_sha256": signature.get("report_sha256"),
        # B16：P2 决策卡（DEC §3.3）——report 的呈现层视图，不新增事实源
        "decision_card": card,
        # 降级态：只给原因与缺失项，不给建议
        "degradation": degradation,
    }


def _decision_card(cfg, tdir):
    """组装 DEC 决策卡（core.decision_card.build_for_run）；不可用时返回 None 不阻断交付视图。"""
    try:
        module = import_core(cfg, "decision_card")
    except ApiError:
        return None
    builder = getattr(module, "build_for_run", None)
    if not callable(builder):
        return None
    try:
        return builder(tdir, pack=_pack_doc(cfg), root=cfg.root)
    except Exception as exc:  # noqa: BLE001 - 决策卡是增强项，失败不拖垮交付视图
        log(f"decision card build failed: {exc}")
        return None


def delivery_card_html(cfg, task_id):
    """GET /api/tasks/<id>/delivery-card：可打印/可转发的单页 HTML 决策卡。"""
    tdir = task_dir(cfg, task_id)
    if not (tdir / "report.json").is_file():
        raise ApiError(404, f"report.json 不存在: {task_id}（先 POST /api/tasks/{task_id}/run）")
    card = _decision_card(cfg, tdir)
    if not isinstance(card, dict):
        raise ApiError(500, "决策卡无法组装（core.decision_card 不可用）")
    try:
        module = import_core(cfg, "decision_card")
        html = module.render_html(card)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError(500, f"决策卡渲染失败: {exc}")
    return RawBody(html, "text/html; charset=utf-8")


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
# 经营决策台：场景引擎（B18，设计文档 §4.5 / §4.5.1–§4.5.3）
#
# 契约：GET /api/scenes -> {ok, scenes:[SceneSpec...], pack_id, generated_at, contract_errors}
#       POST /api/scenes/derive -> 三通道 preset / discovered / user_prompt
#       runs/<id>/scene.json 存在时，GET /api/tasks/<id> 透出 scene 字段
#
# 纪律：前端只认 SceneSpec，不认具体场景；此处不写任何场景业务常量。
#       core.scene 未落盘时回退读 packs/<pack>/scenes/*.json（preset 种子，属数据非代码），
#       并用下方 validate_scene_spec 副本做 §4.5.3 校验。
# TODO(B18): core.scene.load_seeds / core.scene.validate / core.scene.discover 落盘后，
#       把 _load_scene_seeds 的 fallback 分支与 _validate_scene_spec 副本删除，改为直接调用 core。
# --------------------------------------------------------------------------- #
SCENE_SPEC_VERSION = "scene.v1"
SCENE_ORIGINS = ("preset", "discovered", "user_prompt")
SCENE_CONTROL_METHODS = ("mean_2sigma", "target", "cusum")
SCENE_MAX_PROMPT = 2000


def _scene_contract_error(code, reason):
    return {"code": code, "reason": reason}


def _pack_doc(cfg):
    doc = read_json(cfg.root / "packs" / "manufacturing" / "pack.json")
    return doc if isinstance(doc, dict) else {}


def _validate_scene_spec(spec, pack):
    """SceneSpec v1 校验器副本（§4.5.3 六类拒绝；TODO 与 core.scene.validate 合并）。

    规则 1（metric 必须存在于 data_manifest）需要 dat_manifest 才能判定，这里仅做结构校验，
    由 derive/scene 实例写入方保证；规则 2/3 的因子/动作封闭世界依赖 pack 声明，
    pack 未声明 factors/actions 时只校验可判定部分（不静默放行不可逆动作）。
    """
    errors = []
    if not isinstance(spec, dict):
        return ["spec 必须是 JSON 对象"]
    version = spec.get("spec_version")
    if version != SCENE_SPEC_VERSION:
        errors.append(f"未知 spec_version：{version!r}（只接受 {SCENE_SPEC_VERSION}，前端不做兼容猜测）")
    scene_id = spec.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id.strip():
        errors.append("scene_id 必须是非空字符串")
    origin = spec.get("origin")
    if origin is not None and origin not in SCENE_ORIGINS:
        errors.append(f"origin 非法：{origin!r}（允许 {list(SCENE_ORIGINS)}）")
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title 必须是非空字符串")
    metric = spec.get("metric")
    if not isinstance(metric, dict):
        errors.append("metric 必须是对象")
    else:
        metric_id = metric.get("id")
        if not isinstance(metric_id, str) or not metric_id.strip():
            errors.append("metric.id 缺失")
        if metric.get("unit") is not None and not isinstance(metric.get("unit"), str):
            errors.append("metric.unit 必须是字符串")
        control = metric.get("control_line")
        if not isinstance(control, dict) or control.get("method") not in SCENE_CONTROL_METHODS:
            errors.append(f"metric.control_line.method 必须在白名单 {list(SCENE_CONTROL_METHODS)}")
        elif not isinstance(control.get("params"), dict):
            errors.append("metric.control_line.params 必须是对象（阈值只能来自 pack/params）")
    scope = spec.get("entity_scope")
    if scope is not None:
        if not isinstance(scope, dict) or not isinstance(scope.get("values"), list):
            errors.append("entity_scope 必须是 {field, values[]}")
    anchors = pack.get("roi_anchors") if isinstance(pack.get("roi_anchors"), dict) else {}
    irreversible = {str(item) for item in (pack.get("irreversible_actions") or [])}
    keywords = [str(item) for item in (pack.get("irreversible_keywords") or [])]
    actions = spec.get("actions")
    if actions is not None:
        if not isinstance(actions, list):
            errors.append("actions 必须是数组")
        else:
            for index, action in enumerate(actions):
                if not isinstance(action, dict) or not str(action.get("id") or "").strip():
                    errors.append(f"actions[{index}] 缺 id")
                    continue
                key = str(action.get("cost_anchor_key") or action.get("id"))
                if anchors and key not in anchors:
                    errors.append(f"actions[{index}].cost_anchor_key={key!r} 不在 pack.roi_anchors {sorted(anchors)}")
                if action.get("reversible") is False:
                    action_id = str(action.get("id"))
                    if not irreversible:
                        errors.append(f"actions[{index}].id={action_id!r} 声明不可逆但 pack.irreversible_actions 为空")
                    elif action_id not in irreversible and not any(word and word in action_id for word in keywords):
                        errors.append(f"actions[{index}].id={action_id!r} 不可逆但未登记在 pack.irreversible_actions")
    requirements = spec.get("data_requirements")
    if requirements is not None:
        if not isinstance(requirements, list):
            errors.append("data_requirements 必须是数组")
        else:
            for index, item in enumerate(requirements):
                if not isinstance(item, dict) or not str(item.get("field") or "").strip():
                    errors.append(f"data_requirements[{index}] 缺 field")
    scorecard = spec.get("scorecard")
    if scorecard is not None:
        if not isinstance(scorecard, dict):
            errors.append("scorecard 必须是对象")
        elif scorecard.get("scale") not in (None, 100):
            errors.append("scorecard.scale 只支持 100（B16 已统一 100 分制）")
    generated_at = spec.get("generated_at")
    if generated_at is not None and not isinstance(generated_at, str):
        errors.append("generated_at 必须是 ISO8601 字符串")
    return errors


def _seed_specs_from_files(cfg):
    """preset 种子回退读取：packs/*/scenes/*.json，统一返回 (specs, errors)。"""
    specs, errors = [], []
    pack = _pack_doc(cfg)
    packs_dir = cfg.root / "packs"
    if not packs_dir.is_dir():
        return specs, errors
    for path in sorted(packs_dir.glob("*/scenes/*.json")):
        doc = read_json(path)
        candidates = []
        if isinstance(doc, list):
            candidates = [item for item in doc if isinstance(item, dict)]
        elif isinstance(doc, dict):
            if isinstance(doc.get("spec"), dict):
                candidates = [doc["spec"]]
            elif isinstance(doc.get("scene"), dict):
                candidates = [doc["scene"]]
            elif doc.get("spec_version"):
                candidates = [doc]
            elif isinstance(doc.get("scenes"), list):
                candidates = [item for item in doc["scenes"] if isinstance(item, dict)]
        if not candidates:
            errors.append({"code": "scene_contract", "reason": f"种子无法解析为 SceneSpec: {path}"})
            continue
        for spec in candidates:
            problems = _validate_scene_spec(spec, pack)
            if problems:
                errors.append({"code": "scene_invalid", "file": str(path),
                               "scene_id": spec.get("scene_id"), "errors": problems})
            else:
                specs.append(spec)
    return specs, errors


def _load_scene_seeds(cfg):
    """优先 core.scene.load_seeds；未就绪时回退种子文件。返回 (seeds, errors, rejected)。"""
    pack = _pack_doc(cfg)
    try:
        module = import_core(cfg, "scene")
    except ApiError:
        module = None
    errors, rejected = [], []
    if module is not None:
        loader = getattr(module, "load_seeds", None)
        raw = None
        if not callable(loader):
            errors.append(_scene_contract_error("scene_contract", "core.scene 缺 load_seeds（契约 §4.5.1）"))
        else:
            last_error = None
            for args in ((pack,), (str(cfg.root),), ()):
                try:
                    raw = loader(*args)
                    last_error = None
                    break
                except TypeError as exc:  # 参数个数不符，换下一种调用方式
                    last_error = exc
                except Exception as exc:  # noqa: BLE001
                    errors.append(_scene_contract_error("scene_load_error", f"core.scene.load_seeds 失败：{exc}"))
                    last_error = None
                    break
            if raw is None and last_error is not None:
                errors.append(_scene_contract_error(
                    "scene_contract", f"core.scene.load_seeds 签名不符合 load_seeds(pack)/load_seeds(root)：{last_error}"))
        if raw is not None:
            if isinstance(raw, dict):
                rejected = [item for item in (raw.get("rejected") or []) if isinstance(item, (dict, str))]
                raw = raw.get("scenes")
                if raw is None:
                    raw = []
            if not isinstance(raw, list):
                errors.append(_scene_contract_error(
                    "scene_contract", "core.scene.load_seeds 必须返回 SceneSpec 列表或 {scenes:[...]}"))
                return [], errors, rejected
            seeds = []
            validator = getattr(module, "validate", None)
            for item in raw:
                if not isinstance(item, dict):
                    errors.append(_scene_contract_error("scene_contract", "load_seeds 列表内存在非对象条目"))
                    continue
                problems = _validate_scene_spec(item, pack)
                if callable(validator):
                    try:
                        reasons = validator(item, pack) or []
                    except TypeError:
                        try:
                            reasons = validator(item, pack, None) or []
                        except Exception:  # noqa: BLE001
                            reasons = []
                    except Exception:  # noqa: BLE001
                        reasons = []
                    if reasons:
                        problems = list(problems) + [str(reason) for reason in reasons]
                if problems:
                    errors.append({"code": "scene_invalid", "scene_id": item.get("scene_id"), "errors": problems})
                    rejected.append({"scene_id": item.get("scene_id"), "file": item.get("_path"), "reasons": problems})
                else:
                    seeds.append(item)
            return seeds, errors, rejected
    seeds, file_errors = _seed_specs_from_files(cfg)
    for item in file_errors:
        errors.append(item)
        if isinstance(item, dict) and item.get("code") == "scene_invalid":
            rejected.append({"scene_id": item.get("scene_id"), "file": item.get("file"), "reasons": item.get("errors")})
    if module is None:
        errors.insert(0, _scene_contract_error(
            "core_scene_missing",
            "core.scene 未就绪（core-builder 并行中）：回退读取 packs/<pack>/scenes/*.json 并用 §4.5.3 校验副本；" 
            "TODO 与 core.scene.load_seeds/validate 合并，不硬编码假场景"))
    return seeds, errors, rejected


def _scene_latest(cfg, spec):
    """P0 场景卡的「最近决策/当前值」：优先按 task.scene_id 命中，其次按 metric 名/id；无匹配则 null。"""
    scene_id = str(spec.get("scene_id") or "").strip()
    metric = spec.get("metric") if isinstance(spec.get("metric"), dict) else {}
    names = {str(metric.get("name") or "").strip(), str(metric.get("id") or "").strip()}
    names.discard("")
    latest = None
    for row in list_tasks(cfg):
        task = read_json(cfg.runs / row["task_id"] / "task.json")
        if not isinstance(task, dict):
            continue
        if scene_id and str(task.get("scene_id") or "").strip() == scene_id:
            latest = row
            break
        raw_metric = str(task.get("metric") or "").strip()
        if not raw_metric:
            continue
        if raw_metric in names or any(name and (name in raw_metric or raw_metric in name) for name in names):
            latest = row
            break
    if latest is None:
        return None
    tdir = cfg.runs / latest["task_id"]
    report = read_json(tdir / "report.json") if (tdir / "report.json").is_file() else {}
    scene_doc = read_json(tdir / "scene.json") if (tdir / "scene.json").is_file() else {}
    if not isinstance(report, dict):
        report = {}
    if not isinstance(scene_doc, dict):
        scene_doc = {}
    closure = {}
    closure_path = tdir / "loop" / "closure.json"
    if closure_path.is_file():
        closure = read_json(closure_path)
        if not isinstance(closure, dict):
            closure = {}
    decision = report.get("decision_log_entry") if isinstance(report.get("decision_log_entry"), dict) else {}
    scorecard = report.get("scorecard") if isinstance(report.get("scorecard"), dict) else {}
    instance_metric = scene_doc.get("metric") if isinstance(scene_doc.get("metric"), dict) else {}
    finding = report.get("finding") if isinstance(report.get("finding"), list) else []
    confidence = None
    if finding and isinstance(finding[0], dict):
        confidence = finding[0].get("confidence")
    recommendations = report.get("recommendation") if isinstance(report.get("recommendation"), list) else []
    advice = None
    if recommendations and isinstance(recommendations[0], dict):
        first = recommendations[0]
        advice = {
            "action": first.get("action") or first.get("name"),
            "irreversibility": first.get("irreversibility"),
            "roi": first.get("roi"),
        }
    cannot = report.get("cannot_attribute") if isinstance(report.get("cannot_attribute"), list) else []
    next_step = decision.get("next_step") or decision.get("verification_plan")
    review_due = None
    decision_path = tdir / "loop" / "decision.json"
    if decision_path.is_file():
        loop_decision = read_json(decision_path)
        if isinstance(loop_decision, dict):
            review_due = loop_decision.get("review_due")
    return {
        "task_id": latest["task_id"],
        "title": latest["title"],
        "state": latest["state"],
        "updated_at": latest["updated_at"],
        "is_demo": latest["is_demo"],
        "has_report": bool(latest["has_report"]),
        "decision_id": decision.get("id"),
        "verdict": scorecard.get("verdict"),
        "total": scorecard.get("total"),
        "current_value": scene_doc.get("current_value", instance_metric.get("current_value")),
        "control_line": scene_doc.get("control_line") or instance_metric.get("control_line"),
        "scene_instance": bool(scene_doc),
        # U1：P0 五秒内可读「建议动作 + 置信度」；U4：T+7 回看
        "advice": advice,
        "confidence": confidence,
        "cannot_count": len(cannot),
        "next_step": next_step,
        "review_due": review_due,
        "review_status": closure.get("verdict") or closure.get("status") or ("verified" if closure else "unverified"),
        "actual_outcome": closure.get("actual_outcome") or closure.get("actual_result"),
        "increment_value": closure.get("increment_value"),
    }


def scenes_view(cfg, include_rejected=False):
    pack = _pack_doc(cfg)
    seeds, errors, rejected = _load_scene_seeds(cfg)
    scenes = []
    for spec in seeds:
        item = dict(spec)
        item["latest"] = _scene_latest(cfg, spec)
        scenes.append(item)
    payload = {
        "ok": True,
        "scenes": scenes,
        "pack_id": pack.get("pack_id") or "manufacturing",
        "pack_version": pack.get("version"),
        "spec_version": SCENE_SPEC_VERSION,
        "generated_at": now_iso(),
        "contract_errors": errors,
    }
    if include_rejected:
        payload["rejected"] = rejected
    return payload


def _find_preset(cfg, key):
    if not key:
        return None
    seeds, _errors, _rejected = _load_scene_seeds(cfg)
    key = str(key).strip()
    for spec in seeds:
        metric = spec.get("metric") if isinstance(spec.get("metric"), dict) else {}
        if key in (str(spec.get("scene_id")), str(metric.get("id")), str(metric.get("name"))):
            return spec
    return None


def _derive_validate(cfg, spec):
    """派生派生-校验模式（Lead 契约 m01016）：core.scene.validate 六类拒绝。

    spec_version 未知 / 缺必填 → 400 单行 JSON；其余拒绝 → 200 {ok:false, cannot_derive:[人话原因]}。
    """
    pack = _pack_doc(cfg)
    spec_version = spec.get("spec_version")
    if spec_version != SCENE_SPEC_VERSION:
        raise ApiError(
            400,
            f"spec_version 未知：{spec_version!r}（只支持 {SCENE_SPEC_VERSION}）")
    required = ("scene_id", "pack_id", "origin", "title", "question", "metric")
    missing = []
    for key in required:
        value = spec.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    if missing:
        raise ApiError(400, "spec 缺必填字段：" + "、".join(missing))
    if not isinstance(spec.get("metric"), dict) or not str(spec["metric"].get("id") or "").strip():
        raise ApiError(400, "spec.metric 必须是含 id 的对象")
    try:
        module = import_core(cfg, "scene")
    except ApiError:
        raise ApiError(501, "core.scene 未就绪：/api/scenes/derive 校验模式暂不可用（core-builder 并行中）")
    validator = getattr(module, "validate", None)
    if not callable(validator):
        raise ApiError(501, "core.scene 缺 validate（契约 §4.5.3）")
    try:
        reasons = validator(spec, pack) or []
    except TypeError:
        try:
            reasons = validator(spec, pack, None) or []
        except Exception as exc:  # noqa: BLE001
            reasons = [f"core.scene.validate 执行失败：{exc}"]
    except Exception as exc:  # noqa: BLE001
        reasons = [f"core.scene.validate 执行失败：{exc}"]
    reasons = [str(reason) for reason in reasons]
    if reasons:
        return {
            "ok": False,
            "channel": "validate",
            "spec_version": SCENE_SPEC_VERSION,
            "cannot_derive": reasons,
            "rejected": [{"scene_id": spec.get("scene_id"), "reasons": reasons}],
        }
    return {"ok": True, "channel": "validate", "valid": True, "scene": spec, "spec_version": SCENE_SPEC_VERSION}


def _scene_data_ref_rel(cfg, data_ref):
    """把 data_ref 归一为仓库相对路径（可多段），逐段过 core.paths.safe_relative 白名单。

    返回 (rel, error_reason)；error_reason 非 None 时调用方不得继续读文件。
    """
    raw = str(data_ref or "").strip()
    if not raw:
        return None, "discovered 通道需要 data_ref（数据文件的仓库相对路径），当前未提供"
    if ("\\" in raw or raw.startswith("/") or raw.startswith("~")
            or "://" in raw or re.match(r"^[A-Za-z]:", raw)):
        return None, f"data_ref 非法或越界：{raw}"
    parts = [part for part in raw.split("/") if part]
    if not parts:
        return None, f"data_ref 非法或越界：{raw}"
    for part in parts:
        if part in (".", ".."):
            return None, f"data_ref 非法或越界：{raw}"
        try:
            safe_relative(cfg, part)
        except ApiError:
            return None, f"data_ref 非法或越界：{raw}"
    return "/".join(parts), None


def _scene_read_data_file(path):
    """读一个数据文件 -> (meta, records)；文件缺失/JSON 非法/records 非数组 -> (None, None)。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(doc, dict):
        return None, None
    records = doc.get("records")
    if not isinstance(records, list) or not records:
        return None, None
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    return meta, records


def _scene_baseline_sibling(rel_path):
    """基线兄弟文件：同目录同名前缀 + "_T30.json"（material_refill_T.json → material_refill_T30.json）。

    不存在返回 None（当期窗口有没有基线由数据说话，见 _scene_data_records 注释）。
    """
    rel = str(rel_path or "")
    name = rel.rsplit("/", 1)[-1]
    stem = name[:-5] if name.lower().endswith(".json") else name
    base = stem[:-2] if stem.endswith("_T") else stem
    sibling = base + "_T30.json"
    if sibling == name:
        return None
    return (rel.rsplit("/", 1)[0] + "/" + sibling) if "/" in rel else sibling


def _scene_data_records(cfg, data_ref):
    """按 data_ref 读当期 + 基线数据，返回 (manifest, records, error_reason)。

    - data_ref 是仓库相对路径，逐段过 core.paths.safe_relative 白名单；
    - 基线兄弟文件 = 同目录同名前缀 + "_T30.json"；
    - **基线 records 前置、当期 records 后置**：core.scene.discover 取末两条当 latest/previous，
      所以当期必须排在最后，基线只参与 μ/σ 计算；
    - 口径不可省：只拼当期 T（7 条）→ μ=6.0186 σ=0.7346，μ+2σ=7.4878 > latest 7.2 → 判不出异常；
      拼上 T30 基线（n=37）→ μ=5.2014 σ=0.5307，μ+2σ=6.2628 < 7.2 → 命中 over_2sigma。
    """
    rel, error = _scene_data_ref_rel(cfg, data_ref)
    if error:
        return None, None, error
    root = Path(cfg.root).resolve()
    current_path = (root / rel).resolve()
    try:
        current_path.relative_to(root)
    except ValueError:
        return None, None, f"data_ref 非法或越界：{rel}"
    if not current_path.is_file():
        return None, None, f"data_ref 指向的文件不存在：{rel}"
    current_meta, current_records = _scene_read_data_file(current_path)
    if current_records is None:
        return None, None, f"data_ref 文件不是合法数据文件：{rel}"
    records = list(current_records)
    sources = [rel]
    baseline_rel = _scene_baseline_sibling(rel)
    if baseline_rel:
        baseline_path = (root / baseline_rel).resolve()
        try:
            baseline_path.relative_to(root)
        except ValueError:
            baseline_path = None
        if baseline_path is not None and baseline_path.is_file():
            baseline_meta, baseline_records = _scene_read_data_file(baseline_path)
            if baseline_records:
                records = list(baseline_records) + records
                sources.insert(0, baseline_rel)
    fields = set()
    contract = (current_meta or {}).get("field_contract")
    if isinstance(contract, list):
        fields.update(str(item) for item in contract)
    manifest = {
        "fields": sorted(fields),
        "sources": sources,
        "current": rel,
        "baseline": sources[0] if len(sources) > 1 else None,
    }
    return manifest, records, None


def derive_scene(cfg, body):
    """双模式派生（Lead 契约 m01016 + 文档 §4.5.2）：
    - body 含 spec → 校验模式（core.scene.validate）；
    - 否则生成模式：preset 命中种子直接回；discovered 走 core.scene.discover；user_prompt 由 M2 DSH 执行器承接。"""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ApiError(400, "body 必须是 JSON 对象")
    if "spec" in body:
        spec = body.get("spec")
        if not isinstance(spec, dict):
            raise ApiError(400, "body.spec 必须是 SceneSpec 对象（校验模式）")
        return _derive_validate(cfg, spec)
    prompt = body.get("prompt")
    metric_id = body.get("metric_id")
    data_ref = body.get("data_ref")
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
        raise ApiError(400, "body.prompt 必须是非空字符串（≤2000 字）")
    if isinstance(prompt, str) and len(prompt) > SCENE_MAX_PROMPT:
        raise ApiError(400, f"body.prompt 超过 {SCENE_MAX_PROMPT} 字上限")
    if metric_id is not None and not isinstance(metric_id, str):
        raise ApiError(400, "body.metric_id 必须是字符串")
    if data_ref is not None and not isinstance(data_ref, str):
        raise ApiError(400, "body.data_ref 必须是字符串")
    if not any([(prompt and prompt.strip()), (metric_id and metric_id.strip()), (data_ref and data_ref.strip())]):
        raise ApiError(400, "body 需至少包含 prompt / metric_id / data_ref 之一")

    if prompt and prompt.strip():
        # 文档 §4.5.2：T0 server 内不调 LLM，user_prompt 通道由 DSH workflow 的 s0_scene 承接。
        raise ApiError(501, "user_prompt 通道由 M2 DSH 执行器（team-workflow s0_scene）承接，当前 server 不调 LLM（§4.5.2）")

    preset = _find_preset(cfg, metric_id)
    if preset is not None and not (data_ref and data_ref.strip()):
        return {
            "ok": True,
            "channel": "preset",
            "scene": preset,
            "spec_version": SCENE_SPEC_VERSION,
            "source": "packs/<pack>/scenes/*.json 或 core.scene.load_seeds",
        }
    if not (data_ref and data_ref.strip()):
        return {
            "ok": False,
            "channel": "discovered",
            "cannot_derive": [
                f"指标 {metric_id or '(未指定)'} 没有匹配的 preset 种子；"
                "discovered 通道需要 data_ref（数据文件的仓库相对路径）"
            ],
        }
    try:
        module = import_core(cfg, "scene")
    except ApiError:
        module = None
    discover = getattr(module, "discover", None) if module is not None else None
    if not callable(discover):
        raise ApiError(501, "discovered 通道需 core.scene.discover（core-builder 尚未落盘；§4.5.2）")
    manifest, records, error = _scene_data_records(cfg, data_ref)
    if error:
        return {"ok": False, "channel": "discovered", "cannot_derive": [error]}
    pack = _pack_doc(cfg)
    try:
        # core.scene.discover(data_manifest, records, pack) —— 签名是硬契约，不做试探调用
        raw = discover(manifest, records, pack)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "channel": "discovered",
            "cannot_derive": [f"core.scene.discover 执行失败：{exc}"],
        }
    candidates = []
    if isinstance(raw, dict):
        if isinstance(raw.get("scene"), dict):
            candidates = [raw["scene"]]
        elif isinstance(raw.get("scenes"), list):
            candidates = [item for item in raw["scenes"] if isinstance(item, dict)]
        elif raw.get("spec_version"):
            candidates = [raw]
        else:
            candidates = []
    elif isinstance(raw, list):
        candidates = [item for item in raw if isinstance(item, dict)]
    if not candidates:
        return {
            "ok": False,
            "channel": "discovered",
            "cannot_derive": ["core.scene.discover 未返回可用 SceneSpec（可能当前数据无 μ±2σ 异常）"],
        }
    valid, rejected = [], []
    for spec in candidates:
        problems = _validate_scene_spec(spec, pack)
        if problems:
            rejected.append({"scene_id": spec.get("scene_id"), "errors": problems})
        else:
            valid.append(spec)
    if not valid:
        return {
            "ok": False,
            "channel": "discovered",
            "cannot_derive": ["派生结果未通过 SceneSpec 校验（§4.5.3），前端回退最近合法 preset"],
            "rejected": rejected,
        }
    return {
        "ok": True,
        "channel": "discovered",
        "scene": _pick_discovered_scene(valid, metric_id),
        "candidates": valid,
        "rejected": rejected,
        "spec_version": SCENE_SPEC_VERSION,
    }


def _pick_discovered_scene(valid, metric_id):
    """生成模式：调用方点名 metric_id 时优先返回该指标的草稿。"""
    key = str(metric_id or "").strip()
    if not key:
        return valid[0]
    for spec in valid:
        metric = spec.get("metric") if isinstance(spec.get("metric"), dict) else {}
        if str(metric.get("id") or "") == key:
            return spec
    return valid[0]


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


def _scene_data_files_from_task(task):
    """从 task.json.data_manifest 找当期(*_T.json) / 基线(*_T30.json)数据文件（相对 cfg.root）。"""
    current = baseline = None
    for entry in (task.get("data_manifest") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        path = str(entry.get("path") or "")
        if not path:
            continue
        if name.endswith("_T30.json") or path.endswith("_T30.json"):
            baseline = path
        elif name.endswith("_T.json") or path.endswith("_T.json"):
            current = path
    return current, baseline


def _scene_task_records(cfg, task):
    """读当期/基线数据文件 -> (baseline_records, current_records, current_path, baseline_path)。"""
    current_rel, baseline_rel = _scene_data_files_from_task(task)
    root = Path(cfg.root).resolve()

    def _read(rel):
        if not rel:
            return None, []
        path = Path(rel)
        if not path.is_absolute():
            path = root / rel
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return None, []
        meta, records = _scene_read_data_file(path)
        return str(path), (records or [])

    current_path, current_records = _read(current_rel)
    baseline_path, baseline_records = _read(baseline_rel)
    return baseline_records, current_records, current_path, baseline_path


def _write_scene_instance(cfg, tdir, task):
    """run 收口后写 runs/<id>/scene.json（场景实例：μ±2σ 控制线 + 趋势 + 因素三态）。

    spec 来源优先级：task.json.scene_id → preset 种子 → discovered 草稿 → 跳过。
    写方只放 server（tools/run_task.py 不写，避免双写撞 os.replace WinError 5）。
    任何失败都不阻断 run：原因写进 task.json.scene_note。
    """
    task_id = Path(tdir).name
    note = ""
    spec = None
    pack = _pack_doc(cfg)
    baseline_records, current_records, current_path, baseline_path = _scene_task_records(cfg, task)
    try:
        if not current_records:
            note = "缺少当期数据文件（task.json.data_manifest 里没有 *_T.json），跳过场景实例落盘"
        else:
            scene_id = str(task.get("scene_id") or "").strip()
            if scene_id:
                spec = _find_preset(cfg, scene_id)
                if spec is None:
                    note = f"task.json.scene_id={scene_id} 未命中 preset 种子，尝试 discovered"
            if spec is None and not note:
                spec = _find_preset(cfg, task.get("metric") or task.get("metric_id"))
            if spec is None:
                module = import_core(cfg, "scene")
                discover = getattr(module, "discover", None)
                if not callable(discover):
                    note = "core.scene.discover 不可用，跳过场景实例落盘"
                else:
                    manifest = {"fields": sorted({
                        str(key) for row in (baseline_records + current_records)
                        if isinstance(row, dict) for key in row.keys()
                    })}
                    drafts = discover(manifest, list(baseline_records) + list(current_records), pack) or []
                    drafts = [item for item in drafts if isinstance(item, dict)]
                    spec = drafts[0] if drafts else None
                    if spec is None:
                        note = "discovered 未产出场景草稿（数据无 μ±2σ 异常），跳过场景实例落盘"
            if spec is not None:
                module = import_core(cfg, "scene")
                builder = getattr(module, "build_instance", None)
                if not callable(builder):
                    note = "core.scene.build_instance 不可用，跳过场景实例落盘"
                else:
                    instance = builder(spec, pack, baseline_records, current_records,
                                       metric_id=(spec.get("metric") or {}).get("id"))
                    instance["source"] = {
                        "current_path": current_path,
                        "baseline_path": baseline_path,
                    }
                    written = module.write_task_scene(tdir, instance)
                    with cfg.task_lock(task_id):
                        task_doc = read_json(Path(tdir) / "task.json") or {}
                        if not isinstance(task_doc, dict):
                            task_doc = {}
                        task_doc["scene_ref"] = {
                            "path": written.get("path"),
                            "sha256": written.get("sha256"),
                            "size": written.get("size"),
                            "scene_id": instance.get("scene_id"),
                        }
                        task_doc["scene_id"] = instance.get("scene_id")
                        task_doc.pop("scene_note", None)
                        write_json_atomic(Path(tdir) / "task.json", task_doc)
                    log(f"scene instance written {task_id}: {instance.get('scene_id')}")
                    return True
    except Exception as exc:  # noqa: BLE001 - 场景落盘失败不阻断 run
        note = f"场景实例落盘失败：{exc}"
        log(f"scene instance failed {task_id}: {exc}")
    if note:
        with cfg.task_lock(task_id):
            task_doc = read_json(Path(tdir) / "task.json") or {}
            if not isinstance(task_doc, dict):
                task_doc = {}
            task_doc["scene_note"] = note
            write_json_atomic(Path(tdir) / "task.json", task_doc)
    return False


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
        if code == 0:
            # 场景实例落盘（server 是唯一写方；失败不阻断 run）
            _write_scene_instance(cfg, tdir, task)
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
    # 顺序提示（不阻断）：securityGate 确认会回写 report.json（core/gate.py::_apply_to_report），
    # 报告字节一变 → 先前签下的指纹失效（finalize 报 signature hash mismatch）。
    # 正确顺序 gate confirm → sign → finalize；此处只提示，不拦签字
    # （A6 契约：含未确认 securityGate 的真实报告允许签字，finalize 才按 fail-closed 拒收）。
    gate_pending = False
    if isinstance(report_doc, dict) and report_doc.get("is_demo") is not True:
        gates_mod = import_core(cfg, "gates")
        if gates_mod.report_claims_security_gate(report_doc):
            gate_doc = import_core(cfg, "gate").read(tdir) or {}
            gate_pending = str(gate_doc.get("status") or "") != "confirmed"
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
    payload = {"ok": True, "sha256": sha256, "signer": sig.get("signer") or signer,
               "signed_at": sig.get("signed_at"), "path": str(out),
               "signature_file": str(out), "identity": cfg.identity()}
    if gate_pending:
        # 只提示、不阻断：A6 契约允许「含未确认 securityGate 的真实报告」先签字，
        # fail-closed 发生在 /finalize（core.gates.check_finalize）。
        payload["gate_pending"] = True
        payload["warning"] = ("报告含不可逆动作，securityGate 尚未人工确认："
                              "确认门会回写 report.json 使本次指纹失效，届时需重新签字再收口")
    return 200, payload


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
        if "hash mismatch" in message:
            message += ("（报告在签字后被变更——securityGate 确认会回写 report.json，"
                        "使旧指纹失效；请重新 POST /api/tasks/%s/sign 再收口）" % task_id)
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


def _str_param(query, name, default=""):
    raw = (query.get(name) or [None])[0]
    if raw is None:
        return default
    return str(raw).strip()


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
    if path == "/api/scenes":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        include_rejected = str((query.get("include_rejected") or [""])[0]).strip().lower() in ("1", "true", "yes", "on")
        return 200, scenes_view(cfg, include_rejected)
    if path == "/api/scenes/derive":
        if method != "POST":
            raise ApiError(405, "method not allowed")
        return 200, derive_scene(cfg, body)
    if path == "/api/tasks":
        if method == "GET":
            return 200, list_tasks(cfg)
        if method == "POST":
            return 201, create_task(cfg, body)
        raise ApiError(405, "method not allowed")
    if path == "/api/decisions":
        # R4：跨任务决策日志时间线（只读，不落盘）
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, decisions_view(cfg, query)

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
    if action == "delivery-card":
        if method != "GET":
            raise ApiError(405, "method not allowed")
        return 200, delivery_card_html(cfg, task_id)
    if action == "outcome":
        if method == "GET":
            return 200, get_outcome(cfg, task_id)
        if method == "POST":
            return post_outcome(cfg, task_id, body)
        raise ApiError(405, "method not allowed")
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
                ctype = str(self.headers.get("Content-Type") or "")
                is_multipart = ("multipart/form-data" in ctype.lower()
                                and path.endswith("/data"))
                if method == "POST" and is_multipart:
                    body = self._read_multipart(ctype)
                else:
                    body = self._read_body() if method == "POST" else {}
                status, payload = route_api(cfg, method, path, query, body)
                if isinstance(payload, RawBody):
                    self._send_raw(status, payload.body.encode("utf-8"), payload.content_type)
                else:
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

    def _read_multipart(self, ctype):
        """解析 multipart/form-data（纯 stdlib email 解析器，不装 python-multipart）。

        文件字段 → {"name": filename, "content": bytes}；文本字段 → str（UTF-8）。
        """
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "非法 Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"body 过大（上限 {MAX_BODY_BYTES} bytes）")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            raise ApiError(400, "空 multipart body")
        head = (b"Content-Type: " + ctype.encode("latin-1", "replace")
                + b"\r\nMIME-Version: 1.0\r\n\r\n")
        try:
            message = BytesParser(policy=email_policy).parsebytes(head + raw)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(400, f"multipart 解析失败: {exc}")
        if not message.is_multipart():
            raise ApiError(400, "multipart 解析失败（请求不是 multipart 消息）")
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename:
                fields[str(name).strip()] = {"name": filename, "content": payload}
            else:
                charset = part.get_content_charset() or "utf-8"
                try:
                    fields[str(name).strip()] = payload.decode(charset)
                except (UnicodeDecodeError, LookupError):
                    raise ApiError(400, f"multipart 文本字段 {name} 解码失败（{charset}）")
        return fields

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

    def _send_raw(self, status, data: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_static(self, cfg, path, head=False):
        ui = cfg.ui.resolve()
        if path in ("/", "/index.html"):
            # ui/index.html 用相对路径引 team-data.js / api-adapter.js 等同级资源：
            # 在 / 下解析成 /team-data.js → 404，页面只剩静态演示、真数据全断。
            # 重定向到 /ui/index.html 让相对路径落到 /ui/ 下（file:// 直接打开磁盘文件仍照常）。
            self.send_response(302)
            self.send_header("Location", "/ui/index.html")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path.startswith("/ui/"):
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
