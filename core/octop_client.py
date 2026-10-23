# -*- coding: utf-8 -*-
"""Octop 程序化客户端（stdlib only）：login / 找或建解析 agent / WS 对话轮。

口径（G1 批次，L0 路线 A「骑 Octop」）：
  - 认证：POST /api/auth/login {username,password} -> {access_token}；
    之后 /api/* 带 Authorization: Bearer <token>；WS 用 query token=<token>。
  - 解析 agent：独立轻量 agent（非 RC98TE 业务人格），按 name 幂等查找，
    不存在才创建。服务端按 agent 复用同一 thread（2026-10-23 实测：
    同 agent 跨调用走同一 thread_id，历史累积）——「每次调用全新 thread」
    的旧口径不成立；解析契约由 system prompt＋单轮消息自足保证。
  - 凭据：只从环境变量 OCTOP_USERNAME / OCTOP_PASSWORD 读，代码不存凭据。
  - 失败显式：任何一步失败抛 OctopClientError，上层转 status=llm_unavailable，
    禁编造（全链路方案 §3 A0 失败分支）。
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from core import mini_ws

DEFAULT_BASE = "http://127.0.0.1:8088"
PARSER_AGENT_NAME = "intent-parser"
PARSER_SYSTEM_PROMPT = (
    "你是意图解析器，不是业务助手。输入是【用户问题】和【场景清单】。"
    "只输出一个 JSON 对象，禁止输出任何其他文字。规则："
    "1) scene_id 必须从场景清单里原样选一个，清单里没有匹配就输出 {\"scene_not_matched\": true}；"
    "2) 实体（产线/对象）必须从用户问题里提取，问题没说就输出 null，禁止猜；"
    "3) 时间窗只能是 T7 / T30 之一（问题明确说「最近N天/本周/上周」等短窗用 T7，"
    "说「近一个月/30天」用 T30，什么都没说默认 T7）；"
    "4) 问题含糊到无法选场景或无法定位实体时，输出 "
    "{\"ambiguous\": true, \"clarify\": \"一句追问\"}。"
    "输出格式：{\"scene_id\": str|null, \"entities\": {\"production_line\": str|null}, "
    "\"time_window\": \"T7\"|\"T30\", \"ambiguous\": bool, \"scene_not_matched\": bool, "
    "\"clarify\": str|null}"
)


class OctopClientError(RuntimeError):
    """登录失败 / agent 操作失败 / WS 轮次失败。"""


def _http_json(url, payload=None, token=None, method=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")[:300]
        except OSError:
            detail = ""
        raise OctopClientError(f"HTTP {exc.code} {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OctopClientError(f"连不上 Octop（{url}）: {exc.reason}") from exc


def login(base, username, password):
    """POST /api/auth/login -> access_token（免 JWT 端点，deps.py 白名单）。"""
    _, doc = _http_json(base.rstrip("/") + "/api/auth/login",
                        payload={"username": username, "password": password})
    token = str((doc or {}).get("access_token") or "").strip()
    if not token:
        raise OctopClientError("login 成功但响应缺 access_token")
    return token


def find_agent(base, token, name):
    """按 name 找 agent；返回 row 或 None。"""
    _, doc = _http_json(base.rstrip("/") + "/api/agents", token=token)
    rows = doc if isinstance(doc, list) else (doc or {}).get("items") or (doc or {}).get("agents") or []
    for row in rows:
        if isinstance(row, dict) and str(row.get("name") or "") == name:
            return row
    return None


def ensure_parser_agent(base, token, name=PARSER_AGENT_NAME):
    """找 name 同名 agent；没有就建极简解析 agent（只带 name/system_prompt/description）。"""
    row = find_agent(base, token, name)
    if row is not None:
        return row
    _, row = _http_json(
        base.rstrip("/") + "/api/agents",
        payload={
            "name": name,
            "description": "L0 意图解析器（bot-team 自动创建）：用户问题+场景清单 -> strict JSON 意图卡",
            "system_prompt": PARSER_SYSTEM_PROMPT,
        },
        token=token,
    )
    agent_id = str((row or {}).get("agent_id") or "").strip()
    if not agent_id:
        raise OctopClientError("创建解析 agent 成功但响应缺 agent_id")
    return row


def credentials_from_env():
    """读 OCTOP_USERNAME/OCTOP_PASSWORD；缺哪个报哪个（错误信息里给设置示例）。"""
    username = (os.environ.get("OCTOP_USERNAME") or "").strip()
    password = os.environ.get("OCTOP_PASSWORD") or ""
    missing = [k for k, v in (("OCTOP_USERNAME", username), ("OCTOP_PASSWORD", password)) if not v]
    if missing:
        raise OctopClientError(
            "缺少 Octop 凭据环境变量: " + ", ".join(missing)
            + "（setup 时的管理员账号；PowerShell: $env:OCTOP_USERNAME='...' 等）")
    return username, password


def ws_chat_path(base, agent_id, token):
    """构造 WS 路径（query token，ws.py 的认证方式）。"""
    return f"/api/agents/{quote(agent_id, safe='')}/chat/ws?token={quote(token, safe='')}"


def ws_turn(base, agent_id, token, text, timeout=180.0):
    """一轮对话：WS 连上 -> user_turn -> 收帧到 done -> 返回 (frames, assistant_text)。"""
    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    try:
        return mini_ws.turn_once(host, port, ws_chat_path(base, agent_id, token), text, timeout=timeout)
    except mini_ws.WSError as exc:
        raise OctopClientError(f"WS 轮次失败: {exc}") from exc


def parse_question(base, question, catalog_text, timeout=180.0):
    """L0 主调用：确保解析 agent -> WS 一轮 -> 返回 assistant 文本（期望 strict JSON）。

    catalog_text：场景清单的紧凑 JSON 串（core.intent.build_catalog_text 产出）。
    任何网络/协议失败抛 OctopClientError；返回的文本可能不是合法 JSON，
    由 core.intent.parse_llm_json 兜。
    """
    username, password = credentials_from_env()
    token = login(base, username, password)
    row = ensure_parser_agent(base, token)
    agent_id = str(row.get("agent_id") or "")
    prompt = (
        "【场景清单】\n" + catalog_text + "\n\n"
        "【用户问题】\n" + question + "\n\n"
        "只输出一个 JSON 对象。"
    )
    return ws_turn(base, agent_id, token, prompt, timeout=timeout)
