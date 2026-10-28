# -*- coding: utf-8 -*-
"""全链路遥测（飞轮底座）：单一 append-only 账本 + 事件契约。

为什么单独一个文件而不是复用 run.log：
  run.log 是「单任务内」的叙事流（seq 连续、按任务目录存放），无法跨任务联表；
  飞轮要回答的是「哪个角色版本 / 哪类场景 / 哪种改动，让哪项指标变好了」，
  必须以 task_id、role、版本 hash 为键做跨任务聚合，故另立一份全局账本。

契约（与公司 AI 运营中枢 ai-hub-ingestion 对齐，但不依赖它在位）：
  - product_id = "bot_team"，事件名统一前缀 bot_team_
  - props 只允许标量（str/int/float/bool/None），嵌套对象一律拒绝
  - 不上报 prompt 原文、报告正文等大字段；用 hash 代替
  - 本地账本 = 事实源（离线可算、可回溯）；中枢 = 镜像（TRACK_ENDPOINT 未配置时静默 no-op）

账本路径：<bot-team>/.telemetry/flywheel.jsonl（本地、不入库、含业务数据）
"""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

from core.events import append_event, iter_events

PRODUCT_ID = "bot_team"
EVENT_PREFIX = PRODUCT_ID + "_"

LEDGER_DIRNAME = ".telemetry"
LEDGER_FILENAME = "flywheel.jsonl"

_SCALARS = (str, int, float, bool, type(None))

# 事件契约：name -> (required props, optional props, 一句话用途)
# 六个阶段与 docs 中的链路一一对应；新增事件必须同时改本表和聚合器。
EVENTS = {
    # ① 角色设置（因果起点：不知道版本就没法归因）
    "role_snapshot": (
        ("agent_id", "prompt_hash"),
        ("role", "persona_hash", "pack_version", "source", "model"),
        "角色/人设落盘快照，每次同步或重建记一条",
    ),
    "role_change": (
        ("agent_id", "field", "before_hash", "after_hash"),
        ("reason", "actor", "task_id"),
        "角色配置变更（含阈值/场景/提示词），飞轮的分组键",
    ),
    # ② 编排
    "task_created": (
        ("task_id", "scene_id"),
        ("line", "window_start", "window_end", "source", "agent_id"),
        "接单建任务，source=manual|discovered",
    ),
    "step_run": (
        ("task_id", "step", "role", "status", "duration_ms"),
        ("agent_id", "started_at", "ended_at", "tokens", "cost_cny", "error_kind",
         "rework_count"),
        "DAG 单步执行（s1..s6），时长/成本/返工挂在这里",
    ),
    "gate": (
        ("task_id", "gate_id", "verdict"),
        ("reason_code", "signer"),
        "门裁决 pass|reject，reason_code 用于失败归因",
    ),
    # ③ 报告
    "report_built": (
        ("task_id", "finding_count", "is_demo"),
        ("cannot_attribute_count", "degraded_pct", "report_hash", "elapsed_ms"),
        "报告产出形态：有效发现数 / 无归因数 / 降级档位",
    ),
    "sign": (
        ("task_id", "signer"),
        ("signature_hash",),
        "签字收口，采纳链路的硬证据",
    ),
    # ④ 推送
    "push_sent": (
        ("task_id", "agent_id", "ok"),
        ("channel_id", "message_id", "latency_ms", "error_kind", "resend"),
        "推送结果；ok=false 必须配 error_kind；resend=true 表示同任务重发（演示/补偿），计入分母不掩盖",
    ),
    # ⑤ 回信
    "reply": (
        ("task_id", "letter"),
        ("latency_s", "thread_id", "agent_id", "attr"),
        "用户回信 a（采纳）/ b（转人工）/ c（转分析）；attr=任务归属依据",
    ),
    "nudge": (
        ("task_id", "nudge_no"),
        ("days_since_delivery", "responded"),
        "催办；responded 用来算沉默率",
    ),
    # ⑥ 回看
    "review": (
        ("task_id", "hit"),
        ("expected", "actual", "days_after", "reviewed_at"),
        "T+N 实际结果对比，命中率的唯一来源",
    ),
    # ⑦ 账本治理
    "void": (
        ("task_id", "reason"),
        ("actor", "evidence"),
        "作废某任务的全部既有事件（测试/演示污染）：统计侧整单排除，历史行保留不删",
    ),
}

# 承重指标所需的键，聚合器据此报警「字段缺失」
REQUIRED_FOR_METRICS = ("task_id", "letter", "hit", "finding_count")


def ledger_path(root=None):
    """账本绝对路径。<root> 缺省读 BOT_LEDGER_ROOT，再缺省为本文件上两级（bot-team 根）。

    为什么认环境变量：测试/冒烟脚本会把 runs 根指到临时目录，但账本原来硬绑仓库根，
    结果「跑测试」= 往真实账本里灌合成事件（2026-10-27 实证 3 个任务 33 行）。
    隔离必须同时隔离账本，否则指标被测试数据污染且无法归因。
    """
    if root:
        base = Path(root)
    else:
        base = Path(os.environ.get("BOT_LEDGER_ROOT") or Path(__file__).resolve().parent.parent)
    return base / LEDGER_DIRNAME / LEDGER_FILENAME


def _sha(text, size=16):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:size]


def text_hash(text):
    """给 prompt/persona/报告正文算指纹；只存指纹，不存正文。"""
    return _sha(text, 16)


def file_hash(path):
    """文件指纹；读不到返回空串（不抛，埋点不能因文件缺失而中断业务）。"""
    try:
        return _sha(Path(path).read_bytes(), 16)
    except OSError:
        return ""


def _validate(name, props):
    if name not in EVENTS:
        raise ValueError("未注册事件 %r；可用：%s" % (name, ", ".join(sorted(EVENTS))))
    required, optional, _ = EVENTS[name]
    # 空串视同缺失：否则 role="" 会被 events._normalize 静默补成 host，指标失真且查不到
    missing = [k for k in required
               if props.get(k) is None
               or (isinstance(props.get(k), str) and not props[k].strip())]
    if missing:
        raise ValueError("事件 %s 缺少必需字段：%s" % (name, ", ".join(missing)))
    unknown = [k for k in props if k not in required and k not in optional]
    if unknown:
        raise ValueError("事件 %s 含未声明字段：%s（先改 core/telemetry.EVENTS）"
                         % (name, ", ".join(unknown)))
    bad = [k for k, v in props.items() if not isinstance(v, _SCALARS)]
    if bad:
        raise ValueError("事件 %s 的字段必须为标量，以下为嵌套：%s" % (name, ", ".join(bad)))


def emit(name, props, root=None, mirror=True):
    """写一条遥测事件，返回 seq。校验失败抛 ValueError（宁可在埋点处爆，不要脏数据）。"""
    props = dict(props or {})
    _validate(name, props)
    record = {
        "product_id": PRODUCT_ID,
        "event": EVENT_PREFIX + name,
        "name": name,
    }
    record.update(props)
    seq = append_event(str(ledger_path(root)), record)
    if mirror:
        _mirror(record)
    return seq


def _mirror(record):
    """上报中枢：未配置 TRACK_ENDPOINT 时静默返回。失败一律静默（埋点不能影响业务）。"""
    endpoint = os.environ.get("TRACK_ENDPOINT")
    if not endpoint:
        return False
    secret = os.environ.get("TRACK_SECRET") or ""
    body = json.dumps(record, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if secret:
        req.add_header("X-Track-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        # 契约：真实落库判据是 accepted===true，不是 HTTP 2xx
        return bool(payload.get("accepted") is True)
    except Exception:  # noqa: BLE001 — 镜像失败不影响本地账本
        return False


def read(root=None, since=0):
    return iter_events(str(ledger_path(root)), since=since)


def replay(root=None, endpoint=None, secret=None):
    """把本地账本全量重放到中枢（配好 TRACK_ENDPOINT 后一次性补齐历史）。"""
    endpoint = endpoint or os.environ.get("TRACK_ENDPOINT")
    if not endpoint:
        raise RuntimeError("未配置 TRACK_ENDPOINT / 未传 endpoint")
    rows = read(root)
    ok = 0
    for row in rows:
        payload = {k: v for k, v in row.items() if k not in ("seq",)}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if secret or os.environ.get("TRACK_SECRET"):
            req.add_header("X-Track-Secret", secret or os.environ["TRACK_SECRET"])
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                if json.loads(resp.read().decode("utf-8", "replace") or "{}").get("accepted") is True:
                    ok += 1
        except Exception:  # noqa: BLE001
            continue
    return {"total": len(rows), "accepted": ok}


if __name__ == "__main__":
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        seq = emit("task_created", {"task_id": "T-DEMO-1", "scene_id": "scene-refill",
                                    "source": "manual"}, root=tmp, mirror=False)
        print("seq =", seq)
        try:
            emit("task_created", {"task_id": "T-DEMO-2", "scene_id": "x",
                                  "meta": {"nested": 1}}, root=tmp, mirror=False)
        except ValueError as exc:
            print("nested rejected:", exc)
        print(json.dumps(read(tmp), ensure_ascii=False, indent=2))
    sys.exit(0)
