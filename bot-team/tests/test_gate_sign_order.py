# -*- coding: utf-8 -*-
"""securityGate → 签字 → 收口 的顺序契约回归（T0 形态跑通实测出的死局）。

背景（2026-10-20 实测）：
  `POST /gate confirm` 会回写 `report.json`（core/gate.py::_apply_to_report），
  报告字节一变 → 先前签下的指纹失效 → `POST /finalize` 报 `signature hash mismatch`。
  server/README.md 当时把顺序写成 sign → gate → finalize，照着做必然卡死。
  本文件锁死四件事：
    1) gate 未确认时 `/sign` 不阻断（200 + `gate_pending` 提示），`/finalize` fail-closed 409；
    2) 正确顺序 gate confirm → sign → finalize 能收口（decision.status=approved）；
    3) 已经签在前的历史任务，finalize 给出可读原因，重新 /sign 即可恢复；
    4) 静态资源在 /ui/ 下全部可达（否则真数据模式退化为静态演示）。

只依赖 stdlib；沙盒在 %TEMP%，不污染仓库。退出码 0 = 全绿。
"""
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VENV_PY = Path(r"D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe")
IGNORE_COPY = shutil.ignore_patterns("runs", "__pycache__", "*.pyc", ".git", ".pytest_cache")
SIGNER = "老板"
WORKFLOW_RESULT = "demo-result-2026-10-19.json"
DEMO_FILES = ("material_refill_T.json", "material_refill_T30.json")


def pick_python():
    if DEFAULT_VENV_PY.is_file():
        return str(DEFAULT_VENV_PY)
    return sys.executable


PY = pick_python()


class Failure(Exception):
    pass


def fail(reason: str):
    raise Failure(reason)


def read_text(path: Path, limit: int = 200000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError as exc:
        return f"<read error: {exc}>"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def try_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def tail(text: str, n: int = 800) -> str:
    return (text or "")[-n:].replace("\r", "")


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("BOT_SIGNERS", None)
    env.pop("BOT_SIGNER", None)
    return env


def free_port(prefer: int = 8791) -> int:
    for cand in (prefer, prefer + 1, prefer + 2, prefer + 3):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", cand))
            return cand
        except OSError:
            continue
        finally:
            sock.close()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http(method: str, url: str, body=None, timeout: int = 300):
    data = None
    ctype = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        ctype = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, raw


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向（用于断言 302 Location）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_no_redirect(url: str, timeout: int = 60):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, dict(exc.headers or {}), raw


class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="gate-order-"))
        self.repo = self.root / "repo"
        shutil.copytree(ROOT, self.repo, ignore=IGNORE_COPY)
        (self.repo / "runs").mkdir(parents=True, exist_ok=True)
        src = ROOT / "runs" / WORKFLOW_RESULT
        if not src.is_file():
            fail(f"缺少 {WORKFLOW_RESULT}（录制的 team-workflow 结果），无法构造不可逆报告")
        shutil.copy2(src, self.repo / "runs" / WORKFLOW_RESULT)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Server:
    def __init__(self, sb: Sandbox):
        self.sb = sb
        self.repo = sb.repo
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = sb.root / "server.log"
        self.proc = None
        self._log = None

    def __enter__(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [PY, "-B", str(self.repo / "server" / "app.py"), "--host", "127.0.0.1",
             "--port", str(self.port), "--root", str(self.repo),
             "--owner", SIGNER, "--allow-signers", f"{SIGNER},boss"],
            cwd=str(self.repo), stdout=self._log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=child_env(), creationflags=flags)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                fail(f"server 启动即退出 rc={self.proc.returncode}: {tail(read_text(self.log_path))}")
            try:
                status, _ = http("GET", self.base + "/api/health", timeout=2)
                if status == 200:
                    return self
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        fail(f"server 30s 未就绪: {tail(read_text(self.log_path))}")

    def __exit__(self, exc_type, exc, tb):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
        return False

    def post(self, path: str, body=None):
        return http("POST", self.base + path, body if body is not None else {})

    def get(self, path: str):
        return http("GET", self.base + path)


def make_task(srv: Server, tag: str) -> str:
    """建单 → 上传 demo 数据 → 回放录制的 workflow 结果 → 跑到 assembled。返回 task_id。"""
    status, raw = srv.post("/api/tasks", {
        "goal": "定位 B 线补料率异常根因，给出可执行建议并可签字交付",
        "context": "B 线 2026-10-05~10-11 补料率均值 6.02（T30 基线 5.01，+5.79σ）",
        "target": "补料率回落到基线 2σ 带 [4.66, 5.36] 内",
        "line": "B",
        "runner": "external",
    })
    if status != 201:
        fail(f"[{tag}] 建单失败 HTTP {status}: {tail(raw)}")
    task_id = (try_json(raw) or {}).get("task_id") or ""
    if not task_id:
        fail(f"[{tag}] 建单未返回 task_id: {tail(raw)}")

    files = []
    for name in DEMO_FILES:
        src = srv.repo / "demo" / name
        files.append({"name": name, "content": src.read_text(encoding="utf-8"), "encoding": "text"})
    status, raw = srv.post(f"/api/tasks/{task_id}/data", {"files": files})
    if status != 200:
        fail(f"[{tag}] 上传数据失败 HTTP {status}: {tail(raw)}")

    shutil.copy2(srv.repo / "runs" / WORKFLOW_RESULT, srv.repo / "runs" / task_id / "result.json")
    status, raw = srv.post(f"/api/tasks/{task_id}/run", {"runner": "external"})
    if status not in (200, 202):
        fail(f"[{tag}] 触发运行失败 HTTP {status}: {tail(raw)}")

    deadline = time.time() + 240
    state = ""
    while time.time() < deadline:
        status, raw = srv.get(f"/api/tasks/{task_id}")
        doc = try_json(raw) or {}
        state = str(((doc.get("status") or {}).get("state")) or "")
        if state in ("assembled", "awaiting_sign", "signed", "failed"):
            break
        time.sleep(1.0)
    if state != "assembled":
        fail(f"[{tag}] 运行未达 assembled（state={state!r}）: {tail(raw)}")

    report = load_json(srv.repo / "runs" / task_id / "report.json")
    if report.get("is_demo") is not False:
        fail(f"[{tag}] 报告 is_demo={report.get('is_demo')!r}，本回归要求真实报告（非 --demo）")
    return task_id


def case_sign_advisory_then_finalize_fails(srv: Server) -> str:
    """1) gate 未确认 → /sign 不阻断（200 + gate_pending 提示），/finalize fail-closed 409。

    A6 契约：含未确认 securityGate 的真实报告允许签字；A3 负例：pending gate → finalize 非 0。
    因此死路不在签字入口被堵死，而是在收口处 fail-closed，并在签字时就给出顺序提示。
    """
    task_id = make_task(srv, "guard")
    status, raw = srv.post(f"/api/tasks/{task_id}/sign", {"signer": SIGNER})
    if status != 200:
        fail(f"gate 未确认时 /sign 应放行（HTTP {status}）: {tail(raw)}")
    obj = try_json(raw) or {}
    if obj.get("gate_pending") is not True:
        fail(f"/sign 响应缺 gate_pending=true: {tail(raw)}")
    if not any(token in str(obj.get("warning") or "") for token in ("securityGate", "不可逆", "重新签字")):
        fail(f"/sign warning 未指向顺序问题: {tail(raw)}")

    status, raw = srv.post(f"/api/tasks/{task_id}/finalize")
    if status != 409:
        fail(f"gate 未确认时 finalize 未 fail-closed（HTTP {status}）: {tail(raw)}")
    if not any(token in raw for token in ("securityGate", "security_gate", "安全门", "不可逆")):
        fail(f"finalize 拒收原因未指向 securityGate/不可逆: {tail(raw)}")
    return f"gate 未确认 → /sign 200 + gate_pending 提示 → /finalize 409 fail-closed（{task_id}）"


def case_correct_order_finalizes(srv: Server) -> str:
    """2) gate confirm → sign → finalize → approved。"""
    task_id = make_task(srv, "order")
    status, raw = srv.post(f"/api/tasks/{task_id}/gate",
                           {"action": "confirm", "confirmer": SIGNER,
                            "comment": "回归用例：人工确认不可逆动作"})
    if status not in (200, 201):
        fail(f"/gate confirm 失败 HTTP {status}: {tail(raw)}")

    status, raw = srv.post(f"/api/tasks/{task_id}/sign", {"signer": SIGNER})
    if status != 200:
        fail(f"gate 已确认后 /sign 仍失败 HTTP {status}: {tail(raw)}")

    status, raw = srv.post(f"/api/tasks/{task_id}/finalize")
    if status != 200:
        fail(f"正确顺序 finalize 未放行 HTTP {status}: {tail(raw)}")
    obj = try_json(raw) or {}
    if obj.get("status") != "approved":
        fail(f"finalize 后 decision.status={obj.get('status')!r}（期望 approved）")
    if obj.get("signer") != SIGNER:
        fail(f"finalize 后 signer={obj.get('signer')!r}（期望 {SIGNER!r}）")

    report_path = srv.repo / "runs" / task_id / "report.json"
    actual = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if obj.get("signature_sha256") != actual:
        fail(f"signature_sha256={obj.get('signature_sha256')!r} != report.json 整文件 sha256={actual!r}")
    return f"gate→sign→finalize → approved（{task_id}）"


def case_stale_signature_recovers(srv: Server) -> str:
    """3) 签在前的历史死局：finalize 给可读原因，重新 /sign 后可收口。"""
    task_id = make_task(srv, "stale")
    report_path = srv.repo / "runs" / task_id / "report.json"
    # 绕过 API 守卫（CLI 直签），模拟「先签字后确认门」的历史状态
    sig_path = srv.repo / "runs" / task_id / "signature.json"
    proc = subprocess.run([PY, "-B", str(srv.repo / "tools" / "sign_delivery.py"),
                           str(report_path), "--signer", SIGNER, "--out", str(sig_path)],
                          cwd=str(srv.repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=child_env(), timeout=120)
    if proc.returncode != 0:
        fail(f"CLI 签字失败 rc={proc.returncode}: {tail(proc.stderr or proc.stdout)}")

    # 签字刚落下、报告尚未被 gate 改动 → 指纹有效
    status, raw = srv.get(f"/api/tasks/{task_id}")
    detail = try_json(raw) or {}
    if detail.get("signature_stale") is not False:
        fail(f"gate 确认前 signature_stale 应为 false（实际 {detail.get('signature_stale')!r}）: {tail(raw)}")

    status, raw = srv.post(f"/api/tasks/{task_id}/gate",
                           {"action": "confirm", "confirmer": SIGNER,
                            "comment": "回归用例：签在前的人工确认"})
    if status not in (200, 201):
        fail(f"/gate confirm 失败 HTTP {status}: {tail(raw)}")

    # gate 确认回写 report.json → 旧指纹失效，API 必须如实暴露（否则 UI 会禁用「重新签字」→ 无解）
    status, raw = srv.get(f"/api/tasks/{task_id}")
    detail = try_json(raw) or {}
    if detail.get("signature_stale") is not True:
        fail(f"gate 确认后 signature_stale 应为 true（实际 {detail.get('signature_stale')!r}）: {tail(raw)}")

    status, raw = srv.post(f"/api/tasks/{task_id}/finalize")
    if status != 409:
        fail(f"签字在前的死局 finalize 未 409（HTTP {status}）: {tail(raw)}")
    if "hash mismatch" not in raw:
        fail(f"finalize 原因未含 hash mismatch: {tail(raw)}")
    if not any(token in raw for token in ("sign", "签字", "重新")):
        fail(f"finalize 原因未给出恢复动作（重新签字）: {tail(raw)}")

    status, raw = srv.post(f"/api/tasks/{task_id}/sign", {"signer": SIGNER})
    if status != 200:
        fail(f"重新 /sign 失败 HTTP {status}: {tail(raw)}")
    status, raw = srv.get(f"/api/tasks/{task_id}")
    detail = try_json(raw) or {}
    if detail.get("signature_stale") is not False:
        fail(f"重新签字后 signature_stale 应回到 false（实际 {detail.get('signature_stale')!r}）: {tail(raw)}")
    status, raw = srv.post(f"/api/tasks/{task_id}/finalize")
    if status != 200:
        fail(f"重新签字后 finalize 仍未放行 HTTP {status}: {tail(raw)}")
    return f"签字在前 → 指纹失效被如实暴露 → finalize 409 可读 → 重新签字可恢复（{task_id}）"


def case_static_assets_resolvable(srv: Server) -> str:
    """4) 静态资源必须在 /ui/ 下可解析：真数据模式不能因为 JS 404 而退化成静态演示。

    实测缺陷（2026-10-20 浏览器验证）：ui/index.html 用相对路径引同级 JS，
    在 `/` 下解析成 `/api-adapter.js` → 404 → `window.APIAdapter` 缺失 →
    页面只剩静态演示，真数据（任务栏 / 审批门 / 签字）全断。
    """
    status, headers, _ = http_no_redirect(srv.base + "/")
    if status != 302:
        fail(f"GET / 应 302 到 /ui/index.html（实际 HTTP {status}）")
    location = str(headers.get("Location") or "")
    if location != "/ui/index.html":
        fail(f"GET / 的 Location 应为 /ui/index.html（实际 {location!r}）")

    status, raw = srv.get("/ui/index.html")
    if status != 200:
        fail(f"GET /ui/index.html 应 200（实际 {status}）")
    html = raw or ""

    # 抽出 index.html 里引用的相对资源，逐一按 /ui/<src> 验证可达
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    srcs += re.findall(r'<link[^>]+href="([^"]+)"', html)
    relatives = [s for s in srcs if not s.startswith(("/", "http://", "https://", "#"))]
    if not relatives:
        fail("index.html 未解析出任何相对资源引用，断言可能失效")
    for rel in relatives:
        status, _ = srv.get("/ui/" + rel.lstrip("./"))
        if status != 200:
            fail(f"相对资源 /ui/{rel} 不可达（HTTP {status}）→ 真数据模式会退化为静态演示")
    return f"/ 302 → /ui/index.html；{len(relatives)} 个相对资源全部可达（{', '.join(relatives)}）"


def main() -> int:
    sb = Sandbox()
    problems = []
    try:
        with Server(sb) as srv:
            for fn in (case_sign_advisory_then_finalize_fails, case_correct_order_finalizes,
                       case_stale_signature_recovers, case_static_assets_resolvable):
                try:
                    print("CASE", fn.__name__, "PASS:", fn(srv))
                except Failure as exc:
                    problems.append(f"{fn.__name__}: {exc}")
                    print("CASE", fn.__name__, "FAIL:", exc)
    finally:
        sb.cleanup()
    if problems:
        print("RESULT: RED")
        for item in problems:
            print(" -", item)
        return 1
    print("RESULT: GREEN（4/4）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
