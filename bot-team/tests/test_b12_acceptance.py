#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B11 独立验收测试（A13–A16）——Checker 侧黑盒验收。

覆盖（规格来源：Lead 派单消息，仓库内无独立 B11 规格文件）：
  A13 tests/web_api_smoke.py 在无 pandas 环境 exit 0（运行时 sys.modules 无 pandas）。
  A14 tools/ingest_task_data.py 对 CSV 夹具与 openpyxl 生成的 .xlsx 各跑一次：
      T/T30 JSON schema 正确、日期归一、缺列 rc=2 单行 JSON、无 traceback；
      tools/ 与 server/ 零 "import pandas" 命中。
  A15 起随机端口服务 → 建任务 → POST /api/tasks/<id>/data multipart 上传
      table/field_map/line/t_start/t_end/base_start/base_end（line 缺省回落
      task.json/intake.line，两者都空才 400）→ t_records>0、runs/<id>/data 两 JSON、
      sha256 与文件一致、data_refs 进 task.json；
      fixtures workflow-result 走 external runner → 报告 is_demo=false、
      data_refs sha 校验通过 → /sign → /finalize 合法链；
      负例：损坏 CSV / 超大文件 / 越界窗口 / 非法 task_id / line 缺省回落 全拒。
  A16 版本 v0.12.0 在 README.md、SKILL.md、ui/README.md 三处一致；
      README 有 M1.5 能力段且 task_server/workbench 标注 legacy；
      tests/validate_structure.py PASS。

运行方式：
  python -B tests/test_b12_acceptance.py                 # 跑 A13–A16
  python -B tests/test_b12_acceptance.py --case A15      # 单跑一个
  python -B tests/test_b12_acceptance.py --regression    # 附带全量回归
  python -B tests/test_b12_acceptance.py --list          # 列出用例
约定：每个用例独立 %TEMP% 沙盒；子进程一律 python -B；只读产品代码，不写仓库 runs/。
输出：逐条 "CASE A<n> PASS/FAIL: <原因>"，末尾 "RESULT: GREEN|RED"，退出码 0/1。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与环境
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # bot-team
FIX = HERE / "fixtures" / "b12"


def _default_venv_py() -> Path:
    """子进程解释器：仓库同级 venv（bot-2/venv）→ 仓内 venv → 当前解释器。

    不写死某台机器的绝对路径；确需指定时用环境变量 BOT_PY（优先）。
    """
    for rel in ("venv/Scripts/python.exe", ".venv/Scripts/python.exe",
                "venv/bin/python", ".venv/bin/python"):
        cand = ROOT.parent / rel
        if cand.is_file():
            return cand
    return Path(sys.executable)


DEFAULT_VENV_PY = _default_venv_py()


def _pick_py() -> str:
    for key in ("BOT_PY", "B11_PY", "M15_PY", "M1_PY"):
        val = os.environ.get(key)
        if val and Path(val).is_file():
            return val
    if DEFAULT_VENV_PY.is_file():
        return str(DEFAULT_VENV_PY)
    return sys.executable


PY = _pick_py()

IGNORE_COPY = shutil.ignore_patterns("runs", "__pycache__", "*.pyc", ".git", ".pytest_cache")

DEMO_HASH_PREFIX = {
    "demo/material_refill_T.json": "0048fa0aad30",
    "demo/material_refill_T30.json": "da985bce35a0",
    "runs/demo-report-2026-10-19.json": "086c2a51af05",
    "runs/demo-result-2026-10-19.json": "55eab00f4c42",
}

SIGNER = "老板"

CSV_HEADERS = [
    "日期", "产线", "补料率", "产量", "模具状态", "保养记录", "班组",
    "原料批次", "环境温湿度", "换模记录", "停机记录", "信息员",
]


# ---------------------------------------------------------------------------
# 失败分类：实现未就绪 / 测试自身问题 / 断言失败
# ---------------------------------------------------------------------------


class CaseFailure(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def not_ready(msg: str) -> None:
    raise CaseFailure("not_ready", msg)


def test_issue(msg: str) -> None:
    raise CaseFailure("test_issue", msg)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise CaseFailure("assert", msg)


PREFIX = {
    "not_ready": "实现未就绪: ",
    "test_issue": "测试自身问题: ",
    "assert": "断言失败: ",
}


# ---------------------------------------------------------------------------
# 子进程 / 文件工具
# ---------------------------------------------------------------------------


class Res:
    def __init__(self, rc: int, out: str, err: str):
        self.rc = rc
        self.out = out
        self.err = err

    @property
    def text(self) -> str:
        return (self.out or "") + ("\n" if self.out and self.err else "") + (self.err or "")

    def tail(self, n: int = 1200) -> str:
        t = self.text.strip()
        return t[-n:]


def _utf8_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    for key in ("BOT_PACK", "BOT_SIGNERS", "BOT_SIGNER_ALLOWLIST", "BOT_PY", "BOT_ROOT",
                "BOT_HOST", "BOT_PORT", "PYTHONPATH"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run_py(args: list, cwd: Path | None = None, timeout: int = 180,
           env_extra: dict | None = None, stdin_data: bytes | None = None) -> Res:
    cmd = [PY, "-B"] + [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, env=_utf8_env(env_extra),
            input=stdin_data, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CaseFailure("assert", f"命令超时（{timeout}s）：{' '.join(cmd[-4:])}")
    return Res(proc.returncode, proc.stdout.decode("utf-8", "replace"),
               proc.stderr.decode("utf-8", "replace"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def load_json(path: Path):
    return json.loads(read_text(path))


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def try_json(text: str):
    """尽力从输出里解析 JSON（对象或数组），失败返回 None。"""
    if not text:
        return None
    for ch in "{[":
        start = text.find(ch)
        if start < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj
        except Exception:
            continue
    return None


def last_json_line(text: str):
    """从输出中找最后一行可解析为 JSON 的行，返回 (obj, line)。"""
    for line in reversed([l.strip() for l in (text or "").splitlines()]):
        if not line:
            continue
        try:
            return json.loads(line), line
        except Exception:
            continue
    return None, ""


# ---------------------------------------------------------------------------
# 沙盒
# ---------------------------------------------------------------------------


class Sandbox:
    """把仓库复制到 %TEMP% 后在其中跑测试，绝不写仓库 runs/。"""

    def __init__(self, case: str):
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix=f"b11-accept-{case}-"))
        self.repo = self.root / "repo"
        shutil.copytree(ROOT, self.repo, ignore=IGNORE_COPY)
        runs = self.repo / "runs"
        runs.mkdir(exist_ok=True)
        # 红线 demo 文件必须随沙盒存在（回归用例/validate_report 依赖）
        for src in sorted((ROOT / "runs").glob("*.json")):
            if src.name.startswith("."):
                continue
            shutil.copy2(src, runs / src.name)
        self._tmp_seq = 0

    def tmp_dir(self, name: str) -> Path:
        self._tmp_seq += 1
        path = self.root / f"tmp-{name}-{self._tmp_seq}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def rmtree(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# HTTP / 本地服务
# ---------------------------------------------------------------------------


class LocalServer:
    def __init__(self, sb: Sandbox, owner: str = SIGNER, allow: str = "老板,boss"):
        self.sb = sb
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = sb.root / "server.log"
        self.proc = None
        self._start(owner, allow)

    def _start(self, owner: str, allow: str) -> None:
        app = self.sb.repo / "server" / "app.py"
        check(app.is_file(), f"缺少 server/app.py：{app}")
        log = open(self.log_path, "wb")
        self._log_fh = log
        cmd = [PY, "-B", str(app), "--host", "127.0.0.1", "--port", str(self.port),
               "--root", str(self.sb.repo), "--owner", owner, "--allow-signers", allow]
        self.proc = subprocess.Popen(cmd, cwd=str(self.sb.repo), env=_utf8_env(),
                                     stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise CaseFailure(
                    "not_ready",
                    f"server/app.py 启动即退出 rc={self.proc.returncode}：" + self.log_tail(800),
                )
            st, body, _ = self.get_raw("/api/health", timeout=2)
            if st == 200:
                return
            time.sleep(0.3)
        raise CaseFailure("not_ready", "server/app.py 40s 内未就绪：" + self.log_tail(800))

    def log_tail(self, n: int = 1200) -> str:
        try:
            return read_text(self.log_path)[-n:]
        except Exception:
            return "<no log>"

    # --- HTTP 原语 -------------------------------------------------------
    def request(self, method: str, path: str, data: bytes | None = None,
                headers: dict | None = None, timeout: int = 60):
        url = self.base + path
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b"", dict(exc.headers or {})
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise CaseFailure("not_ready", f"HTTP 连接失败 {method} {path}: {exc}")

    def get_raw(self, path: str, timeout: int = 30):
        st, body, hdrs = self.request("GET", path, timeout=timeout)
        return st, body, hdrs

    def get_json(self, path: str, timeout: int = 30):
        st, body, _ = self.request("GET", path, timeout=timeout)
        text = body.decode("utf-8", "replace")
        return st, try_json(text), text

    def post_json(self, path: str, obj, timeout: int = 120):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        st, body, _ = self.request("POST", path, data=data,
                                   headers={"Content-Type": "application/json; charset=utf-8"},
                                   timeout=timeout)
        text = body.decode("utf-8", "replace")
        return st, try_json(text), text

    def post_multipart(self, path: str, parts: list, timeout: int = 180):
        data, ctype = build_multipart(parts)
        st, body, _ = self.request("POST", path, data=data,
                                   headers={"Content-Type": ctype}, timeout=timeout)
        text = body.decode("utf-8", "replace")
        return st, try_json(text), text

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self._log_fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_multipart(parts: list) -> tuple:
    """parts: [{name, value(bytes|str), filename?, ctype?}] → (body, content_type)"""
    boundary = "----B11Boundary" + hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
    chunks = []
    for part in parts:
        name = part["name"]
        value = part["value"]
        if isinstance(value, str):
            value = value.encode("utf-8")
        filename = part.get("filename")
        ctype = part.get("ctype") or "application/octet-stream"
        if filename is not None:
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                          f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8"))
            chunks.append(value)
            chunks.append(b"\r\n")
        else:
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                          f'\r\n\r\n'.encode("utf-8"))
            chunks.append(value)
            chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _json_body(body):
    return body if isinstance(body, (dict, list)) else {}


def extract_state(body) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("state",):
        val = body.get(key)
        if isinstance(val, str):
            return val
    status = body.get("status")
    if isinstance(status, dict):
        val = status.get("state")
        if isinstance(val, str):
            return val
    elif isinstance(status, str):
        return status
    task = body.get("task")
    if isinstance(task, dict):
        val = task.get("state")
        if isinstance(val, str):
            return val
    return ""


def wait_state(srv: LocalServer, task_id: str, targets: set, timeout: float = 120.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        st, obj, text = srv.get_json(f"/api/tasks/{task_id}")
        if st == 200:
            last = extract_state(obj)
            if last in targets:
                return last
        time.sleep(0.5)
    return last or "<unknown>"


def report_timeout(srv: LocalServer, task_id: str, timeout: float = 120.0) -> str:
    return wait_state(srv, task_id, {"assembled", "awaiting_sign", "signed", "failed"}, timeout)


# ---------------------------------------------------------------------------
# A13：web_api_smoke.py 无 pandas exit 0
# ---------------------------------------------------------------------------


def case_a13(_sb: Sandbox) -> str:
    sb = _sb
    script = sb.repo / "tests" / "web_api_smoke.py"
    check(script.is_file(), f"缺少 tests/web_api_smoke.py：{script}")

    pandas_probe = run_py(["-c", "import pandas"], cwd=sb.repo, timeout=60)
    if pandas_probe.rc == 0:
        test_issue("环境里仍能 import pandas，A13 的『无 pandas 环境』前提不成立")

    wrapper = r"""
import json, runpy, sys, traceback
rc = 0
try:
    runpy.run_path(r"tests/web_api_smoke.py", run_name="__main__")
except SystemExit as exc:
    code = exc.code
    rc = 0 if code in (None, 0) else (code if isinstance(code, int) else 1)
except Exception:
    traceback.print_exc()
    rc = 1
print("B11_WRAPPER " + json.dumps({"rc": rc, "pandas": "pandas" in sys.modules}))
"""
    res = run_py(["-c", wrapper], cwd=sb.repo, timeout=240)
    marker = None
    for line in res.out.splitlines():
        if line.startswith("B11_WRAPPER "):
            try:
                marker = json.loads(line[len("B11_WRAPPER "):])
            except Exception:
                marker = None
    if marker is None:
        test_issue(
            "web_api_smoke 包装执行未输出版本标记（脚本可能提前 sys.exit 于 import 阶段）："
            + res.tail(1500)
        )
    combined = res.text
    check("Traceback (most recent call last)" not in combined,
          "web_api_smoke 运行抛 traceback：\n" + res.tail(2000))
    check("No module named 'pandas'" not in combined and 'No module named "pandas"' not in combined,
          "web_api_smoke 仍依赖 pandas：" + res.tail(1500))
    check(marker.get("rc") == 0, f"web_api_smoke 退出码 {marker.get('rc')}（期望 0）：" + res.tail(2000))
    check(marker.get("pandas") is False,
          "web_api_smoke 运行后 sys.modules 出现 pandas（间接依赖未清干净）")
    return "web_api_smoke.py rc=0，运行时 sys.modules 无 pandas"

# ---------------------------------------------------------------------------
# A14：ingest_task_data.py CSV / xlsx / 缺列 / 零 pandas 命中
# ---------------------------------------------------------------------------


def _ingest_args(input_path: Path, outdir: Path) -> list:
    return [
        "tools/ingest_task_data.py",
        "--input", str(input_path),
        "--line", "B",
        "--t-start", "2026-10-05", "--t-end", "2026-10-11",
        "--base-start", "2026-09-05", "--base-end", "2026-10-04",
        "--outdir", str(outdir),
        "--field-map", str(FIX / "field-map.yaml"),
    ]


def _dataset_docs(outdir: Path) -> list:
    docs = []
    for path in sorted(outdir.glob("*.json")):
        try:
            doc = load_json(path)
        except Exception:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("records"), list):
            doc["_file"] = path.name
            docs.append(doc)
    return docs


def _check_dataset_pair(res: Res, outdir: Path, label: str) -> str:
    check(res.rc == 0, f"{label}: ingest 退出码 {res.rc}（期望 0）：" + res.tail(1500))
    check("Traceback (most recent call last)" not in res.text,
          f"{label}: ingest 抛 traceback：" + res.tail(2000))
    docs = _dataset_docs(outdir)
    check(len(docs) == 2,
          f"{label}: 期望 outdir 下 2 个数据 JSON（T/T30），实得 {[d['_file'] for d in docs]}")

    def _n(doc):
        return len(doc.get("records") or [])

    docs.sort(key=_n)
    t_doc, t30_doc = docs[0], docs[1]
    check(_n(t_doc) == 7, f"{label}: T 窗口应 7 条记录，实得 {_n(t_doc)}（{t_doc['_file']}）")
    check(_n(t30_doc) == 30,
          f"{label}: T30 窗口应 30 条记录，实得 {_n(t30_doc)}（{t30_doc['_file']}）")

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for doc, tag in ((t_doc, "T"), (t30_doc, "T30")):
        meta = doc.get("meta")
        check(isinstance(meta, dict) and meta, f"{label}: {tag} JSON 缺 meta 对象")
        keys = set(meta)
        check(bool(keys & {"dataset", "purpose", "production_line", "line", "field_contract"}),
              f"{label}: {tag} meta 字段过少：{sorted(keys)}")
        records = doc.get("records") or []
        for rec in records:
            check(isinstance(rec, dict), f"{label}: {tag} records 元素不是对象：{type(rec)}")
            date = rec.get("date")
            check(isinstance(date, str) and bool(date_re.match(date)),
                  f"{label}: {tag} 日期未归一为 YYYY-MM-DD：{date!r}")
            line = rec.get("production_line") or rec.get("line")
            check(line == "B", f"{label}: {tag} 产线过滤错误：{line!r}（期望 B）")
            check(rec.get("refill_rate") is not None,
                  f"{label}: {tag} 记录缺 refill_rate 字段")
    t_records = t_doc["records"]
    check(t_records[0]["date"] == "2026-10-05" and t_records[-1]["date"] == "2026-10-11",
          f"{label}: T 窗口日期范围错误 {t_records[0]['date']}~{t_records[-1]['date']}")
    check(t30_doc["records"][0]["date"] == "2026-09-05"
          and t30_doc["records"][-1]["date"] == "2026-10-04",
          f"{label}: T30 窗口日期范围错误 "
          f"{t30_doc['records'][0]['date']}~{t30_doc['records'][-1]['date']}")
    first = float(t_records[0]["refill_rate"])
    last = float(t_records[-1]["refill_rate"])
    check(abs(first - 5.10) < 1e-6 and abs(last - 7.20) < 1e-6,
          f"{label}: T 段补料率取值异常 {first}~{last}（期望 5.10~7.20）")
    return f"{t_doc['_file']}(7) + {t30_doc['_file']}(30)"


def _make_xlsx(path: Path) -> None:
    try:
        from openpyxl import Workbook  # type: ignore
    except Exception as exc:  # noqa: BLE001
        test_issue(f"venv 缺 openpyxl，无法生成 .xlsx 夹具：{exc}")
    from datetime import date, timedelta  # noqa: PLC0415

    wb = Workbook()
    ws = wb.active
    ws.title = "补料率"
    ws.append(CSV_HEADERS)
    start = date(2026, 9, 5)
    for i in range(37):
        day = start + timedelta(days=i)
        t_refill = [5.10, 5.28, 5.54, 5.90, 6.26, 6.85, 7.20][i - 30] if i >= 30 else round(5.00 + 0.004 * i, 2)
        ws.append([day, "B", t_refill, 1000 + i, "normal", "[]", "甲班",
                   "RM-2026043", "{}", "[]", "晨会", "李强"])
        ws.append([day, "C", 4.80, 900 + i, "normal", "[]", "乙班",
                   "RM-2026043", "{}", "[]", "晨会", "王芳"])
    wb.save(str(path))


def case_a14(sb: Sandbox) -> str:
    check((sb.repo / "tools" / "ingest_task_data.py").is_file(),
          "缺少 tools/ingest_task_data.py")
    check((FIX / "task.csv").is_file(), f"缺夹具 {FIX / 'task.csv'}")
    check((FIX / "field-map.yaml").is_file(), f"缺夹具 {FIX / 'field-map.yaml'}")

    # 1) CSV
    out_csv = sb.tmp_dir("ingest-csv")
    res = run_py(_ingest_args(FIX / "task.csv", out_csv), cwd=sb.repo, timeout=180)
    if "ModuleNotFoundError" in res.text and "pandas" in res.text:
        not_ready("ingest_task_data.py 仍 import pandas（B8 未完成）：" + res.tail(800))
    if "unrecognized arguments" in res.text:
        test_issue("ingest_task_data.py 参数契约与派单不一致：" + res.tail(800))
    evidence = ["CSV " + _check_dataset_pair(res, out_csv, "CSV")]

    # 2) XLSX（openpyxl 现场生成，日期为 datetime 单元格）
    xlsx = sb.root / "task-b11.xlsx"
    _make_xlsx(xlsx)
    out_xlsx = sb.tmp_dir("ingest-xlsx")
    res_x = run_py(_ingest_args(xlsx, out_xlsx), cwd=sb.repo, timeout=180)
    if "ModuleNotFoundError" in res_x.text and "pandas" in res_x.text:
        not_ready("ingest_task_data.py 仍 import pandas（B8 未完成）：" + res_x.tail(800))
    evidence.append("XLSX " + _check_dataset_pair(res_x, out_xlsx, "XLSX"))

    # 3) 缺列 → rc=2、单行 JSON、无 traceback
    out_bad = sb.tmp_dir("ingest-bad")
    bad = run_py(_ingest_args(FIX / "bad_missing_column.csv", out_bad), cwd=sb.repo, timeout=120)
    check(bad.rc == 2, f"缺列 CSV 期望 rc=2，实得 rc={bad.rc}：" + bad.tail(1200))
    check("Traceback (most recent call last)" not in bad.text,
          "缺列 CSV 输出 traceback：" + bad.tail(1500))
    lines = [l for l in bad.text.splitlines() if l.strip()]
    json_lines = []
    for line in lines:
        try:
            json_lines.append(json.loads(line))
        except Exception:
            continue
    check(len(json_lines) == 1,
          "缺列 CSV 期望『单行 JSON』错误输出，实得 JSON 行数="
          f"{len(json_lines)}，输出：{bad.tail(1200)}")
    err_doc = json_lines[0]
    check(isinstance(err_doc, dict) and err_doc.get("ok") is False,
          f"缺列 CSV 错误 JSON 不含 ok:false：{err_doc}")
    err_text = json.dumps(err_doc, ensure_ascii=False)
    check(any(k in err_text for k in ("缺列", "缺少", "missing", "column", "必填", "required")),
          f"缺列 CSV 错误 JSON 未说明缺列原因：{err_text[:400]}")
    evidence.append("缺列 rc=2 单行 JSON")

    # 4) tools/ 与 server/ 零 "import pandas"
    hits = []
    for base in ("tools", "server"):
        for path in sorted((sb.repo / base).rglob("*.py")):
            text = read_text(path)
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("import pandas") or stripped.startswith("from pandas"):
                    hits.append(f"{path.relative_to(sb.repo)}:{lineno}")
    check(not hits, "tools/ 或 server/ 仍存在 pandas 导入：" + ", ".join(hits))
    evidence.append("tools/server import pandas 命中 0")
    return "；".join(evidence)


# ---------------------------------------------------------------------------
# A15：multipart 上传 → external runner → 真实报告 → sign/finalize
# ---------------------------------------------------------------------------


def _task_id_of(body) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("task_id", "id"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
    task = body.get("task")
    if isinstance(task, dict):
        for key in ("task_id", "id"):
            val = task.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _create_task(srv: LocalServer, payload: dict) -> tuple:
    st, obj, text = srv.post_json("/api/tasks", payload)
    return st, obj, text


def _data_files(task_dir: Path) -> list:
    data_dir = task_dir / "data"
    if not data_dir.is_dir():
        return []
    return sorted(p for p in data_dir.glob("*.json") if p.is_file())


def _task_refs(task_dir: Path) -> dict:
    """task.json 里登记的 data_files / data_refs（name → {sha256,size,path}）。"""
    task_doc = {}
    task_json = task_dir / "task.json"
    if task_json.is_file():
        try:
            task_doc = load_json(task_json)
        except Exception:
            task_doc = {}
    raw = task_doc.get("data_refs") or task_doc.get("data_files") or []
    out = {}
    if isinstance(raw, dict):
        raw = list(raw.values())
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out[Path(item).name] = {"path": item}
        elif isinstance(item, dict):
            path = item.get("path") or item.get("name") or item.get("file")
            if isinstance(path, str) and path:
                out[Path(path).name] = item
    return out


def _verify_report_refs(repo: Path, report: dict) -> list:
    refs = report.get("data_refs")
    check(isinstance(refs, list) and refs, f"report.data_refs 为空或非列表：{refs!r}")
    verified = []
    for ref in refs:
        check(isinstance(ref, dict),
              f"report.data_refs 元素不是对象（real 模式必须带 sha256）：{ref!r}")
        path = ref.get("path")
        sha = ref.get("sha256")
        check(isinstance(path, str) and path, f"data_ref 缺 path：{ref!r}")
        check(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha or ""),
              f"data_ref 缺合法 sha256：{ref!r}")
        target = Path(path)
        if not target.is_absolute():
            target = repo / path
        check(target.is_file(), f"data_ref 指向文件不存在：{path}")
        actual = sha256_file(target)
        check(actual == sha, f"data_ref sha256 不符：{path} 声明 {sha[:12]} 实算 {actual[:12]}")
        verified.append(f"{Path(path).name}:{sha[:12]}")
    return verified


def case_a15(sb: Sandbox) -> str:
    check((sb.repo / "server" / "app.py").is_file(), "缺少 server/app.py")
    check((sb.repo / "tools" / "run_task.py").is_file(), "缺少 tools/run_task.py")

    with LocalServer(sb) as srv:
        # 1) 建任务（可逆建议类 intake；字段取派单口径的超集）
        payload = {
            "line": "B",
            "title": "B 线补料率 7 日爬升归因（B11 验收）",
            "goal": "判断 B 线补料率 7 日爬升是否由模具磨损/保养不足引起，并给出可逆处置",
            "plant": "一号车间",
            "metric": "补料率",
            "window": {"start": "2026-10-05", "end": "2026-10-11"},
            "baseline_window": {"start": "2026-09-05", "end": "2026-10-04"},
            "t_start": "2026-10-05", "t_end": "2026-10-11",
            "base_start": "2026-09-05", "base_end": "2026-10-04",
            "context": "B 线补料率 7 日从 5.10% 爬升到 7.20%（B11 验收）",
            "expected": "T+7 补料率回落至 ≤5.3%",
            "data_owner": "业务负责人",
        }
        st, body, text = _create_task(srv, payload)
        tid = _task_id_of(body)
        if st not in (200, 201):
            # 契约兼容：退回最小 intake 再试一次（不同字段命名下的合理变体）
            minimal = {"line": "B", "window": {"start": "2026-10-05", "end": "2026-10-11"},
                       "baseline_window": {"start": "2026-09-05", "end": "2026-10-04"}}
            st2, body2, text2 = _create_task(srv, minimal)
            st, body, text, tid = st2, body2, text2, _task_id_of(body2)
        check(st in (200, 201), f"POST /api/tasks 期望 2xx，实得 {st}：{text[:400]}")
        check(bool(tid), f"POST /api/tasks 未返回 task_id：{text[:400]}")
        check(bool(re.fullmatch(r"T-\d{8}-[A-Za-z0-9_\-]+", tid)),
              f"服务发号格式异常：{tid!r}")
        task_dir = sb.repo / "runs" / tid
        check(task_dir.is_dir(), f"任务目录未创建：runs/{tid}")
        check((task_dir / "task.json").is_file(), f"缺 runs/{tid}/task.json")

        # 2) multipart 上传 CSV + 字段映射 + 窗口（已确认契约：POST /api/tasks/<id>/data）
        #    部件：table（CSV 文件）、field_map（yaml 文件）、line、t_start/t_end/base_start/base_end
        csv_bytes = (FIX / "task.csv").read_bytes()
        fm_bytes = (FIX / "field-map.yaml").read_bytes()

        def _parts(csv_data: bytes = None, fm_data: bytes = None, with_line: bool = True,
                   line: str = "B",
                   t_start: str = "2026-10-05", t_end: str = "2026-10-11",
                   base_start: str = "2026-09-05", base_end: str = "2026-10-04") -> list:
            parts = [
                {"name": "table", "filename": "task.csv",
                 "value": csv_bytes if csv_data is None else csv_data, "ctype": "text/csv"},
                {"name": "field_map", "filename": "field-map.yaml",
                 "value": fm_bytes if fm_data is None else fm_data,
                 "ctype": "application/x-yaml"},
            ]
            if with_line:
                parts.append({"name": "line", "value": line})
            parts.extend([
                {"name": "t_start", "value": t_start},
                {"name": "t_end", "value": t_end},
                {"name": "base_start", "value": base_start},
                {"name": "base_end", "value": base_end},
            ])
            return parts

        upload_path = f"/api/tasks/{tid}/data"
        st, up_body, up_text = srv.post_multipart(upload_path, _parts(), timeout=240)
        if st == 404:
            not_ready(f"server 尚无 POST /api/tasks/<id>/data 上传端点（B9 未完成）：{up_text[:300]}")
        check(st in (200, 201) and not (isinstance(up_body, dict) and up_body.get("ok") is False),
              f"POST {upload_path} 期望 2xx，实得 {st}：{up_text[:600]}")
        up_body = up_body or {}
        t_records = up_body.get("t_records")
        if t_records is None:
            t_records = up_body.get("records")

        data_files = _data_files(task_dir)
        check(len(data_files) == 2,
              f"runs/{tid}/data 期望 2 个 JSON（T/T30），实得 {[p.name for p in data_files]}")
        if t_records is not None:
            check(int(t_records) > 0, f"上传响应 t_records={t_records}（期望 >0）")
        else:
            docs = [load_json(p) for p in data_files]
            counts = [len(d.get("records") or []) for d in docs if isinstance(d, dict)]
            check(sum(counts) > 0, f"上传后数据 JSON records 全空：{counts}")

        # data_refs 进 task.json 且 sha 与文件一致
        refs = _task_refs(task_dir)
        check(bool(refs), f"task.json 未登记 data_refs/data_files：{task_dir}/task.json")
        for path in data_files:
            entry = refs.get(path.name)
            check(entry is not None,
                  f"task.json data_refs 未收录 {path.name}（已收录 {sorted(refs)}）")
            sha = entry.get("sha256")
            check(isinstance(sha, str) and sha == sha256_file(path),
                  f"task.json 中 {path.name} 的 sha256 与文件不一致")
        evidence = [f"上传端点 POST {upload_path}",
                    f"data={[p.name for p in data_files]} sha 一致"]

        # 3) external runner：fixtures workflow-result → result.json
        result_path = task_dir / "result.json"
        shutil.copy2(FIX / "workflow_result_real.json", result_path)
        st, run_body, run_text = srv.post_json(f"/api/tasks/{tid}/run", {"runner": "external"})
        if st in (404, 501):
            not_ready(f"server 尚无 external runner（POST /run 返回 {st}，B9 未完成）：{run_text[:300]}")
        if st == 400 and "runner" in run_text.lower():
            not_ready(f"server 不识别 runner=external：{run_text[:300]}")
        check(st in (200, 202), f"POST /run {{runner:external}} 期望 2xx，实得 {st}：{run_text[:400]}")
        state = report_timeout(srv, tid, timeout=150)
        check(state != "failed", f"external runner 状态 failed："
              + srv.log_tail(600))
        check(state in ("assembled", "awaiting_sign", "signed"),
              f"external runner 后状态未到 assembled（实得 {state!r}）")
        report_path = task_dir / "report.json"
        check(report_path.is_file(), f"缺 runs/{tid}/report.json")
        report = load_json(report_path)
        check(report.get("is_demo") is False,
              f"external runner 报告 is_demo={report.get('is_demo')!r}（期望 False，fixture 不得冒充真实）")
        check(report.get("task_id") == tid, f"报告 task_id={report.get('task_id')!r} 与任务不一致")
        verified = _verify_report_refs(sb.repo, report)
        dle = report.get("decision_log_entry") or {}
        if isinstance(dle, dict) and dle.get("data_refs"):
            try:
                _verify_report_refs(sb.repo, {"data_refs": dle["data_refs"]})
            except CaseFailure as exc:
                raise CaseFailure("assert", "decision_log_entry.data_refs 溯源失败：" + exc.message)
        evidence.append(f"report is_demo=False，data_refs sha 通过 {verified}")

        # validate_report --require-real（独立 CLI 复核）
        val = run_py(["tests/validate_report.py", str(report_path), "--require-real"],
                     cwd=sb.repo, timeout=120)
        check(val.rc == 0 and "RESULT: PASS" in val.text,
              "validate_report --require-real 未通过：" + val.tail(1500))
        evidence.append("validate_report --require-real PASS")

        # 4) /sign → sha256 == 整文件哈希
        st, sign_body, sign_text = srv.post_json(f"/api/tasks/{tid}/sign", {"signer": SIGNER})
        check(st == 200, f"POST /sign 期望 200，实得 {st}：{sign_text[:400]}")
        sign_doc = sign_body or {}
        sha = sign_doc.get("sha256") or sign_doc.get("report_sha256")
        actual_sha = sha256_file(report_path)
        check(sha == actual_sha,
              f"/sign 返回 sha256={sha!r} 与 report.json 整文件哈希 {actual_sha} 不一致")
        evidence.append(f"/sign sha256={actual_sha[:12]}")

        # 5) /finalize → approved / approval.by 老板
        st, fin_body, fin_text = srv.post_json(f"/api/tasks/{tid}/finalize", {})
        check(st == 200, f"POST /finalize 期望 200 approved，实得 {st}：{fin_text[:500]}")
        decision_path = task_dir / "loop" / "decision.json"
        check(decision_path.is_file(), f"缺 runs/{tid}/loop/decision.json")
        decision = load_json(decision_path)
        check(decision.get("status") == "approved",
              f"decision.status={decision.get('status')!r}（期望 approved）")
        approval = decision.get("approval") or {}
        check(approval.get("by") == SIGNER,
              f"decision.approval.by={approval.get('by')!r}（期望 {SIGNER}）")
        evidence.append("finalize approved / approval.by=老板")

        # 6) 负例：损坏 CSV / 超大 / 越界窗口 / 非法 task_id / line 缺省回落
        problems = []

        def _new_task_for_neg(name: str) -> str:
            st_c, body_c, text_c = _create_task(srv, {"line": "B",
                                                     "window": {"start": "2026-10-05", "end": "2026-10-11"},
                                                     "baseline_window": {"start": "2026-09-05",
                                                                         "end": "2026-10-04"}})
            if st_c not in (200, 201) or not _task_id_of(body_c):
                raise CaseFailure("assert", f"负例 {name} 无法建任务：{st_c} {text_c[:300]}")
            return _task_id_of(body_c)

        def _neg_corrupt() -> None:
            tid2 = _new_task_for_neg("corrupt")
            corrupt = b"\xff\xfe\x00\x01date,line\n\xff\xfe\xfd\x00" * 8
            st_c, obj_c, text_c = srv.post_multipart(
                f"/api/tasks/{tid2}/data", _parts(csv_data=corrupt))
            check(400 <= st_c < 500, f"损坏 CSV 期望 4xx，实得 {st_c}：{text_c[:300]}")
            d = sb.repo / "runs" / tid2 / "data"
            check(not (d.is_dir() and any(d.glob("*.json"))),
                  f"损坏 CSV 被拒后仍落盘：{list(d.glob('*.json'))}")

        def _neg_oversize() -> None:
            tid2 = _new_task_for_neg("oversize")
            head = ("日期,产线,补料率,产量,模具状态,保养记录,班组,原料批次,环境温湿度,换模记录,停机记录,信息员\n").encode("utf-8")
            row = "2026-10-05,B,5.2,1000,normal,[],甲班,RM-1,{},[],晨会,李强\n".encode("utf-8")
            big = head + row * ((9 * 1024 * 1024) // len(row) + 10)
            check(len(big) > 8 * 1024 * 1024, "超大夹具构造失败")
            st_c, obj_c, text_c = srv.post_multipart(
                f"/api/tasks/{tid2}/data", _parts(csv_data=big), timeout=240)
            check(400 <= st_c < 500, f"超大文件（>8MB）期望 4xx，实得 {st_c}：{text_c[:300]}")
            d = sb.repo / "runs" / tid2 / "data"
            check(not (d.is_dir() and any(d.glob("*.json"))),
                  f"超大文件被拒后仍落盘：{list(d.glob('*.json'))}")

        def _neg_window() -> None:
            tid2 = _new_task_for_neg("window")
            for t_start, t_end, tag in (("2026-10-11", "2026-10-05", "end<start"),
                                        ("2030-01-01", "2030-01-10", "2030 越界")):
                st_c, obj_c, text_c = srv.post_multipart(
                    f"/api/tasks/{tid2}/data", _parts(t_start=t_start, t_end=t_end))
                if 200 <= st_c < 300:
                    st_r, run_obj, run_txt = srv.post_json(
                        f"/api/tasks/{tid2}/run", {"runner": "external"})
                    state2 = report_timeout(srv, tid2, timeout=45)
                    check(state2 == "failed" or 400 <= st_r < 500,
                          f"越界窗口（{tag}）未被拒：上传 {st_c}、run {st_r}、状态 {state2!r}")
                else:
                    check(400 <= st_c < 500,
                          f"越界窗口（{tag}）期望 4xx，实得 {st_c}：{text_c[:300]}")

        def _neg_task_id() -> None:
            for bad_id in ("../../evil", "T-2026", "a"):
                st_c, obj_c, text_c = _create_task(srv, {"task_id": bad_id, "line": "B"})
                check(400 <= st_c < 500,
                      f"非法 task_id {bad_id!r} 期望 4xx，实得 {st_c}：{text_c[:300]}")
            evil = (sb.repo / ".." / "evil")
            check(not evil.exists(), f"路径遍历写出了越权目录：{evil}")
            for name in (sb.repo / "runs").iterdir():
                if name.is_dir():
                    check(bool(re.fullmatch(r"T-\d{8}-[A-Za-z0-9_\-]+", name.name)),
                          f"runs/ 下出现非法任务目录：{name.name}")
            st_g, _, _ = srv.get_raw("/api/tasks/..%2f..%2fevil")
            check(400 <= st_g < 500, f"非法 task_id GET 期望 4xx，实得 {st_g}")

        def _neg_line_fallback() -> None:
            # 契约：line 缺省时服务端回落 task.json/intake.line；两者都空才 400
            st_t, body_t, text_t = _create_task(srv, {
                "line": "B",
                "window": {"start": "2026-10-05", "end": "2026-10-11"},
                "baseline_window": {"start": "2026-09-05", "end": "2026-10-04"},
            })
            check(st_t in (200, 201) and bool(_task_id_of(body_t)),
                  f"line 缺省回落用例建任务失败：{st_t} {text_t[:300]}")
            tid_b = _task_id_of(body_t)
            st_u, body_u, text_u = srv.post_multipart(
                f"/api/tasks/{tid_b}/data", _parts(with_line=False), timeout=240)
            check(200 <= st_u < 300,
                  f"task.json 有 line 时上传不带 line 应回落成功（实得 {st_u}）：{text_u[:400]}")

            st_n, body_n, text_n = _create_task(srv, {
                "window": {"start": "2026-10-05", "end": "2026-10-11"},
                "baseline_window": {"start": "2026-09-05", "end": "2026-10-04"},
            })
            if st_n not in (200, 201):
                # 建任务即强制 line，视为等价 fail-closed，可接受（记录不判红）
                evidence.append(f"line 缺省回落[{st_n}] 建任务即拒缺 line")
                return
            tid_n = _task_id_of(body_n)
            st_u2, body_u2, text_u2 = srv.post_multipart(
                f"/api/tasks/{tid_n}/data", _parts(with_line=False), timeout=240)
            check(400 <= st_u2 < 500,
                  f"task.json/intake 均无 line 时上传应 400，实得 {st_u2}：{text_u2[:300]}")

        for name, fn in (("损坏 CSV", _neg_corrupt), ("超大文件", _neg_oversize),
                         ("越界窗口", _neg_window), ("非法 task_id", _neg_task_id),
                         ("line 缺省回落", _neg_line_fallback)):
            try:
                fn()
                evidence.append(f"负例[{name}] 已拒")
            except CaseFailure as exc:
                problems.append(f"负例[{name}] {exc.kind}: {exc.message}")
        if problems:
            raise CaseFailure("assert", "；".join(problems))
        return "；".join(evidence)


# ---------------------------------------------------------------------------
# A16：版本三处一致 + README M1.5/legacy + validate_structure
# ---------------------------------------------------------------------------


def _legacy_near(lines: list, idx: int, span: int = 2) -> bool:
    window = " ".join(lines[max(0, idx - span): idx + span + 1]).lower()
    return any(tok in window for tok in ("legacy", "旧", "弃用", "deprecated"))


def case_a16(sb: Sandbox) -> str:
    readme_path = sb.repo / "README.md"
    skill_path = sb.repo / "SKILL.md"
    ui_readme_path = sb.repo / "ui" / "README.md"
    for path in (readme_path, skill_path, ui_readme_path):
        check(path.is_file(), f"缺文件：{path}")
    readme = read_text(readme_path)
    skill = read_text(skill_path)
    ui_readme = read_text(ui_readme_path)

    check("v0.12.0" in readme, "README.md 无 v0.12.0")
    check("v0.12.0" in skill, "SKILL.md 无 v0.12.0")
    check("v0.12.0" in ui_readme, "ui/README.md 无 v0.12.0")

    check("M1.5" in readme, "README.md 无 M1.5 能力段")
    check(any(k in readme for k in ("安全门", "securityGate", "gate.json")),
          "README.md 的 M1.5 能力段未提及安全门/securityGate/gate.json")

    lines = readme.splitlines()
    for token, label in (("task_server", "task_server.py"), ("workbench", "workbench.html")):
        idxs = [i for i, line in enumerate(lines) if token in line]
        check(bool(idxs), f"README.md 未提及 {label}（无法核对 legacy 标注）")
        check(any(_legacy_near(lines, i) for i in idxs),
              f"README.md 中 {label} 未标注 legacy/旧/弃用/deprecated（±2 行内）")

    res = run_py(["tests/validate_structure.py"], cwd=sb.repo, timeout=180)
    check(res.rc == 0, f"validate_structure 退出码 {res.rc}（期望 0）：" + res.tail(1500))
    check("RESULT: PASS" in res.text,
          "validate_structure 未输出 RESULT: PASS：" + res.tail(1500))
    return "v0.12.0 三处一致；README 含 M1.5 安全门段；task_server/workbench 标 legacy；validate_structure PASS"


# ---------------------------------------------------------------------------
# 回归（--regression）：既有套件 + demo 四哈希
# ---------------------------------------------------------------------------

REGRESSION_SCRIPTS = [
    ("m1 A1–A6", ["tests/test_m1_acceptance.py", "--no-keep-failed"]),
    ("m15 A7–A12", ["tests/test_m15_acceptance.py", "--no-keep-failed"]),
    ("m1_smoke", ["tests/m1_smoke.py"]),
    ("test_core_m15", ["tests/test_core_m15.py"]),
    ("validate_structure", ["tests/validate_structure.py"]),
    ("validate_report demo", ["tests/validate_report.py", "runs/demo-report-2026-10-19.json"]),
]


def run_regression(sb: Sandbox) -> list:
    problems = []
    for rel, prefix in DEMO_HASH_PREFIX.items():
        path = sb.repo / rel
        if not path.is_file():
            problems.append(f"缺回归红线文件 {rel}")
            continue
        actual = sha256_file(path)
        if not actual.startswith(prefix):
            problems.append(f"demo 哈希变化：{rel} 期望 {prefix}… 实算 {actual[:12]}…")
    for name, args in REGRESSION_SCRIPTS:
        if not (sb.repo / args[0]).is_file():
            problems.append(f"回归脚本缺失：{args[0]}")
            continue
        res = run_py(args, cwd=sb.repo, timeout=900)
        if res.rc != 0:
            problems.append(f"回归 {name} 退出码 {res.rc}：{res.tail(800)}")
        elif "RESULT: FAIL" in res.text:
            problems.append(f"回归 {name} 输出 RESULT: FAIL：{res.tail(800)}")
    return problems


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

CASES = {
    "A13": ("web_api_smoke 无 pandas exit 0", case_a13),
    "A14": ("ingest_task_data CSV/xlsx/缺列/零 pandas", case_a14),
    "A15": ("multipart 上传 → external runner → 真报告 → sign/finalize", case_a15),
    "A16": ("v0.12.0 三处一致 + M1.5 legacy 标注 + validate_structure", case_a16),
}


def _summary_line(name: str, ok: bool, detail: str) -> str:
    return f"{name}: {'PASS' if ok else 'FAIL'} — {detail}"


def main(argv: list) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="B11 独立验收测试（A13–A16）")
    parser.add_argument("--case", default="", help="只跑指定用例（逗号分隔，如 A13,A15）")
    parser.add_argument("--list", action="store_true", help="列出用例")
    parser.add_argument("--keep", action="store_true", help="保留全部沙盒")
    parser.add_argument("--keep-failed", action="store_true", help="保留失败用例的沙盒")
    parser.add_argument("--regression", action="store_true", help="附带跑既有套件回归")
    parser.add_argument("--timeout", type=int, default=900, help="单用例总超时（秒，预留）")
    args = parser.parse_args(argv)

    if args.list:
        for key, (desc, _fn) in CASES.items():
            print(f"{key}: {desc}")
        return 0

    sel = [c.strip().upper() for c in args.case.split(",") if c.strip()] or list(CASES)
    for name in sel:
        if name not in CASES:
            print(f"未知用例 {name}；可选 {list(CASES)}", file=sys.stderr)
            return 2

    results = {}
    overall_ok = True
    for name in sel:
        desc, fn = CASES[name]
        sb = Sandbox(name)
        ok, detail = False, ""
        try:
            detail = fn(sb)
            ok = True
        except CaseFailure as exc:
            detail = PREFIX.get(exc.kind, "") + exc.message
        except Exception as exc:  # noqa: BLE001
            detail = "断言失败: 未捕获异常 " + repr(exc) + "\n" + traceback.format_exc()[-1200:]
        results[name] = (ok, detail)
        print(f"CASE {name} {'PASS' if ok else 'FAIL'}: {detail}")
        print(f"  ({desc})")
        keep = args.keep or (args.keep_failed and not ok)
        if keep:
            print(f"  SANDBOX (kept): {sb.root}")
        else:
            sb.rmtree()
        if not ok:
            overall_ok = False
        sys.stdout.flush()

    if args.regression:
        sb = Sandbox("REG")
        try:
            problems = run_regression(sb)
        except CaseFailure as exc:
            problems = [PREFIX.get(exc.kind, "") + exc.message]
        if problems:
            overall_ok = False
            print("CASE REG FAIL: " + "；".join(problems))
        else:
            print("CASE REG PASS: 既有套件 + demo 四哈希全绿")
        if args.keep or (args.keep_failed and problems):
            print(f"  SANDBOX (kept): {sb.root}")
        else:
            sb.rmtree()

    print("")
    print("SUMMARY: " + " | ".join(_summary_line(k, *v) for k, v in results.items()))
    print("RESULT: " + ("GREEN" if overall_ok else "RED"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
