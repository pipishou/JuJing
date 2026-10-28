#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IM 签字身份白名单：把「谁在 IM 里点了签字」落成可审计的 signer。

为什么要有这一层
----------------
签字的本质不是「点了按钮」，是「这个动作能追到某个具体的人，且这个人有资格」。
IM 里点按钮带过来的只有一个应用维度 ID（飞书 open_id / 企微 userid），它不是
姓名、也不会自己变成 signer。所以中间必须有一张**本地映射表**：

    {"signers": [{"id": "ou_xxx", "id_type": "open_id",
                  "name": "Mo", "signer": "老板", "chat_id": ""}]}

- `id`：IM 侧身份（飞书 open_id 是「应用维度」的，换应用就变，所以必须钉死在这里）
- `signer`：要写进签名文件的署名，必须在服务端 signer 白名单里（server --allow-signers）
- `chat_id`：可选；给了就要求消息必须来自这个群/会话，防「别人把卡片转给第三人」

失败口径（fail closed，这个不能商量）
------------------------------------
- 配置文件存在但 JSON 坏 → 抛 SignerConfigError，**不放行任何人**（不是退回旧常量）
- 配置文件存在但 `signers` 为空 → 拒绝所有人（这是「我要关掉 IM 签字」的表达）
- 配置文件不存在 → 退回历史 open_id 常量（兼容存量部署），并标 `source="legacy"`，
  调用方应当在日志里提示「白名单未配置」
- 命中但 `enabled: false` → 视为未授权

刻意不做的事：不缓存、不联网校验姓名。姓名/部门校验是「飞书通讯录权限」那条线
（当前应用 40004 no dept authority，权限未开），拿到权限后由上层补，不塞进这里。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BOT_TEAM = TOOLS_DIR.parent
DEFAULT_FILE = BOT_TEAM / "server" / "im_signers.json"

# 历史常量：桥应用 cli_aa536a0b3db89d29 视角下的 Mo（与 lark-cli 视角不是同一串）
LEGACY_OPEN_ID = "ou_daffa42fee5a30596dd3e5393b527e5e"
LEGACY_SIGNER = "老板"

ID_TYPES = ("open_id", "user_id", "union_id")


class SignerConfigError(RuntimeError):
    """白名单文件存在但不可用——必须让人看见，不能静默放行。"""


def config_path(path=None):
    if path:
        return Path(path)
    env = os.environ.get("BOT_IM_SIGNERS_FILE")
    return Path(env) if env else DEFAULT_FILE


def load(path=None):
    """读白名单。返回 (entries, source)。

    source ∈ {"file", "legacy"}；文件不存在时 entries 为空、source="legacy"。
    """
    p = config_path(path)
    if not p.exists():
        return [], "legacy"
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都不许放行
        raise SignerConfigError("IM 签字白名单不可解析: %s (%s)" % (p, exc)) from exc
    if isinstance(raw, list):
        raw = {"signers": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("signers"), list):
        raise SignerConfigError("IM 签字白名单格式错误：需要 {\"signers\": [...]}: %s" % p)

    entries = []
    for i, item in enumerate(raw["signers"]):
        if not isinstance(item, dict):
            raise SignerConfigError("signers[%d] 不是对象" % i)
        sid = str(item.get("id") or "").strip()
        if not sid:
            raise SignerConfigError("signers[%d] 缺 id" % i)
        id_type = str(item.get("id_type") or "open_id").strip()
        if id_type not in ID_TYPES:
            raise SignerConfigError("signers[%d].id_type 不支持: %s" % (i, id_type))
        signer = str(item.get("signer") or "").strip()
        if not signer:
            raise SignerConfigError("signers[%d] 缺 signer（要写进签名的署名）" % i)
        entries.append({
            "id": sid,
            "id_type": id_type,
            "name": str(item.get("name") or signer).strip(),
            "signer": signer,
            "chat_id": str(item.get("chat_id") or "").strip(),
            "enabled": bool(item.get("enabled", True)),
        })
    return entries, "file"


def resolve(sender_id, chat_id=None, path=None):
    """把 IM 侧身份换成 signer。未授权返回 None（调用方负责拒绝）。

    - 文件不存在 → 只认 legacy open_id
    - chat_id 校验：条目给了 chat_id 就必须一致；没给则不限（p2p 场景）
    """
    sid = str(sender_id or "").strip()
    if not sid:
        return None
    entries, source = load(path)
    if source == "legacy":
        if sid == LEGACY_OPEN_ID:
            return {"id": sid, "name": LEGACY_SIGNER, "signer": LEGACY_SIGNER,
                    "source": "legacy", "chat_id": str(chat_id or "")}
        return None
    for e in entries:
        if e["id"] != sid or not e["enabled"]:
            continue
        if e["chat_id"] and chat_id and e["chat_id"] != str(chat_id):
            continue
        if e["chat_id"] and not chat_id:
            continue  # 条目限定了会话，但消息没给会话 → 不放行
        return {"id": sid, "name": e["name"], "signer": e["signer"],
                "source": "file", "chat_id": str(chat_id or "")}
    return None


def describe(path=None):
    """给人看的白名单摘要（不打印完整 ID）。"""
    entries, source = load(path)
    if source == "legacy":
        return {"source": "legacy", "count": 0,
                "hint": "白名单未配置，退回历史 open_id（%s）" % mask(LEGACY_OPEN_ID)}
    return {"source": "file", "count": len(entries),
            "signers": [{"id": mask(e["id"]), "name": e["name"],
                         "signer": e["signer"], "enabled": e["enabled"]}
                        for e in entries]}


def mask(ident):
    s = str(ident or "")
    return s[:6] + "…" + s[-4:] if len(s) > 12 else s
