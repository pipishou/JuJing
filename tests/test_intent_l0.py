#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G1 L0/L1 自测（纯 stdlib，不依赖 pytest、不依赖 Octop 在线）。

覆盖：
  1. load_catalog / build_catalog_text：种子 -> 场景目录 -> 解析器 prompt 清单
  2. parse_llm_json：裸 JSON / 带前后缀 / 垃圾 / 多对象取首个 / 空输入
  3. resolve_intent 全分支：ok / ambiguous（显式+缺省追问）/ scene_not_matched
     （显式未命中、未知场景、未注册数据源）/ 实体缺失定向追问 / 时间窗追问 /
     parse_failed
  4. check_a0：ok 缺四字段违规、软状态须带人话原因、未知状态违规
  5. mini_ws：客户端帧掩码编码 / 收帧（文本、ping 自动 pong、close、分片重组）/
     extract_assistant_text 聚合
  6. A1 正例（--skip-llm 子进程全链）：exit 0 + intent.json ok + scene.json 产出
     + current_value 非 null + control_line.mean 非空 + sha256 一致 + 零降级
  7. A1 负例（子进程）：--line Z 数据中不存在 -> exit 5 禁编造，不出场景卡
  8. A1 降级（库级）：variantC 纸面契约齐全但记录全 null -> degradation 非空
在线 Octop 用例不在本文件（需 OCTOP_USERNAME/OCTOP_PASSWORD 凭据，单独 smoke）。
运行：venv python -B tests/test_intent_l0.py
"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import fsutil, intent as intent_mod, pack as pack_mod  # noqa: E402
from core import scene as scene_mod  # noqa: E402
from core import mini_ws  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append(name)
        print(f"[FAIL] {name}  <- {detail}")
    else:
        print(f"[OK]   {name}")


def _run_cli(extra_args, outdir):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-B", str(ROOT / "tools" / "intent_turn.py"),
         "--question", "B线补料率最近一周怎么样", "--outdir", str(outdir)] + extra_args,
        capture_output=True, encoding="utf-8", env=env)


def case_catalog(pack):
    catalog = intent_mod.load_catalog(pack)
    hit = [c for c in catalog if c["scene_id"] == "scene-refill-rate-first"]
    check("catalog 含首发场景", len(hit) == 1, f"catalog={catalog}")
    check("catalog 带指标名", bool(hit) and hit[0]["metric_name"] == "补料率",
          f"hit={hit}")
    text = intent_mod.build_catalog_text(catalog)
    parsed = json.loads(text)
    check("catalog 文本是紧凑 JSON 数组", isinstance(parsed, list) and parsed, text[:120])
    return catalog


def case_parse_llm_json():
    check("parse_llm_json 裸对象", intent_mod.parse_llm_json('{"a":1}') == {"a": 1})
    check("parse_llm_json 带前后缀",
          intent_mod.parse_llm_json('解析结果：{"a":1} 请查收') == {"a": 1})
    check("parse_llm_json 垃圾输入", intent_mod.parse_llm_json("我觉得是模具问题") is None)
    check("parse_llm_json 多对象取首个",
          intent_mod.parse_llm_json('{"a":1} 尾随 {"b":2}') == {"a": 1})
    check("parse_llm_json 空输入", intent_mod.parse_llm_json("") is None)
    check("parse_llm_json None 输入", intent_mod.parse_llm_json(None) is None)


def case_resolve_intent(catalog):
    ok_doc = json.dumps({"scene_id": "scene-refill-rate-first",
                         "entities": {"production_line": "B"},
                         "time_window": "T7"}, ensure_ascii=False)
    intent = intent_mod.resolve_intent("B线补料率最近一周怎么样", ok_doc, catalog)
    check("resolve ok 状态", intent["status"] == intent_mod.STATUS_OK, str(intent))
    check("resolve ok 实体", intent["entities"] == {"production_line": "B"})
    check("resolve ok 时间窗", intent["time_window"] == {"kind": "T7", "grain": "daily"})
    check("resolve ok 数据源指向 demo",
          intent["data_source"] == {"dataset": "material_refill",
                                    "current": "demo/material_refill_T.json",
                                    "baseline": "demo/material_refill_T30.json"})
    check("resolve ok 通过 A0", intent_mod.check_a0(intent) == [])

    intent = intent_mod.resolve_intent("q", '{"ambiguous":true,"clarify":"哪个产线？"}', catalog)
    check("resolve ambiguous 显式追问",
          intent["status"] == intent_mod.STATUS_AMBIGUOUS
          and intent["clarify_question"] == "哪个产线？"
          and intent["scene_candidates"], str(intent))

    intent = intent_mod.resolve_intent("q", '{"ambiguous":true}', catalog)
    check("resolve ambiguous 缺省追问",
          intent["clarify_question"] == "请说明要查的场景和对象。", str(intent))

    intent = intent_mod.resolve_intent("q", '{"scene_not_matched":true}', catalog)
    check("resolve 显式未命中",
          intent["status"] == intent_mod.STATUS_SCENE_NOT_MATCHED
          and len(intent["options"]) == len(catalog), str(intent))

    intent = intent_mod.resolve_intent(
        "q", '{"scene_id":"scene-nope","entities":{"production_line":"B"},"time_window":"T7"}',
        catalog)
    check("resolve 未知场景未命中",
          intent["status"] == intent_mod.STATUS_SCENE_NOT_MATCHED, str(intent))

    intent = intent_mod.resolve_intent(
        "q", '{"scene_id":"scene-refill-rate-first","time_window":"T7"}', catalog)
    check("resolve 实体缺失定向追问",
          intent["status"] == intent_mod.STATUS_AMBIGUOUS
          and "补料率异常首发" in intent["clarify_question"], str(intent))

    intent = intent_mod.resolve_intent(
        "q", '{"scene_id":"scene-refill-rate-first","entities":{"production_line":"B"},'
             '"time_window":"T90"}', catalog)
    check("resolve 非法时间窗追问",
          intent["status"] == intent_mod.STATUS_AMBIGUOUS
          and "时间窗" in intent["clarify_question"], str(intent))

    fake = [{"scene_id": "scene-fake", "title": "假场景", "question": "",
             "metric_id": "", "metric_name": "", "entity_scope": {}}]
    intent = intent_mod.resolve_intent(
        "q", '{"scene_id":"scene-fake","entities":{"production_line":"B"},"time_window":"T7"}',
        fake)
    check("resolve 未注册数据源拒绝",
          intent["status"] == intent_mod.STATUS_SCENE_NOT_MATCHED
          and "未注册数据源" in intent["reason"], str(intent))

    intent = intent_mod.resolve_intent("q", "这不是 JSON", catalog)
    check("resolve 解析失败不编造",
          intent["status"] == intent_mod.STATUS_PARSE_FAILED
          and intent["raw_excerpt"] == "这不是 JSON", str(intent))


def case_check_a0():
    ok_intent = {"status": "ok", "scene_candidates": [{"scene_id": "s"}], "scene_id": "s",
                 "entities": {"production_line": "B"},
                 "time_window": {"kind": "T7"}, "data_source": {"dataset": "d"}}
    check("A0 完整 ok 通过", intent_mod.check_a0(ok_intent) == [])

    broken = {**ok_intent, "entities": {}}
    check("A0 ok 缺实体违规", len(intent_mod.check_a0(broken)) == 1,
          str(intent_mod.check_a0(broken)))

    check("A0 软状态带原因通过",
          intent_mod.check_a0({"status": "ambiguous", "clarify_question": "？"}) == [])
    check("A0 软状态缺原因违规",
          len(intent_mod.check_a0({"status": "ambiguous"})) >= 1)
    check("A0 未知状态违规",
          any("未知状态" in r for r in intent_mod.check_a0({"status": "weird", "reason": "x"})))
    check("A0 非 dict 违规", intent_mod.check_a0("nope") == ["intent 不是对象"])


class _FakeSendSock:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data


class _FakeRecvSock:
    def __init__(self, chunk):
        self.chunk = chunk
        self.sent = b""

    def recv(self, n):
        out, self.chunk = self.chunk[:n], self.chunk[n:]
        return out

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


def _unmask(raw):
    indicator = raw[1] & 0x7F
    if indicator <= 125:
        offset, length = 2, indicator
    elif indicator == 126:
        length = struct.unpack(">H", raw[2:4])[0]
        offset = 4
    else:
        length = struct.unpack(">Q", raw[2:10])[0]
        offset = 10
    mask = raw[offset:offset + 4]
    return raw[0], length, bytes(
        b ^ mask[i % 4] for i, b in enumerate(raw[offset + 4:offset + 4 + length]))


def case_mini_ws():
    client = mini_ws.WSClient("h", 1, "/p")
    try:
        client.send_text("hi")
        check("未连接发送报错", False, "应抛 WSError")
    except mini_ws.WSError:
        check("未连接发送报错", True)

    sock = _FakeSendSock()
    client._sock = sock
    client.send_text("hi")
    fin_op, length, payload = _unmask(sock.sent)
    check("客户端帧带掩码且可还原",
          fin_op == 0x81 and length == 2 and payload == b"hi",
          f"raw={sock.sent!r}")

    long_text = "x" * 300
    sock.sent = b""
    client.send_text(long_text)
    fin_op, length, payload = _unmask(sock.sent)
    check("126 长度扩展帧", fin_op == 0x81 and length == 300 and payload == long_text.encode())

    frames = b"\x81\x02hi" + b"\x89\x00" + b"\x88\x02\x03\xe8"
    client = mini_ws.WSClient("h", 1, "/p")
    rsock = _FakeRecvSock(frames)
    client._sock = rsock
    check("收文本帧", client.recv_text() == "hi")
    check("ping 自动回 pong 后 close 收尾返回 None", client.recv_text() is None)
    check("pong+close 帧已回写",
          rsock.sent.startswith(b"\x8a\x80") and b"\x88\x82" in rsock.sent,
          f"sent={rsock.sent!r}")

    client = mini_ws.WSClient("h", 1, "/p")
    client._sock = _FakeRecvSock(b"\x01\x03Hel" + b"\x80\x02lo")
    check("分片重组", client.recv_text() == "Hello")

    frames = [{"type": "message.chunk", "delta": "你"},
              {"type": "turn.end", "text": "好"},
              {"type": "done"}]
    check("assistant 文本聚合", mini_ws.extract_assistant_text(frames) == "你好")
    check("无文本帧聚合为空", mini_ws.extract_assistant_text([{"type": "done"}]) == "")

    live = [{"type": "reasoning", "content": "{草稿}"},
            {"type": "token", "content": "{\"scene_id\":"},
            {"type": "token", "content": "\"scene-refill-rate-first\"}"},
            {"type": "done"}]
    check("token 帧聚合（实测帧型）",
          mini_ws.extract_assistant_text(live) == '{"scene_id":"scene-refill-rate-first"}')
    check("reasoning 帧被排除",
          "{草稿}" not in mini_ws.extract_assistant_text(live))


def case_cli_happy():
    outdir = Path(tempfile.mkdtemp(prefix="intent-happy-"))
    proc = _run_cli(["--skip-llm", "--scene-id", "scene-refill-rate-first",
                     "--line", "B", "--window", "T7"], outdir)
    check("正例 exit 0", proc.returncode == 0, f"rc={proc.returncode} out={proc.stdout} err={proc.stderr[-400:]}")
    summary = json.loads(proc.stdout)
    check("正例 summary ok 且零降级",
          summary["status"] == "ok" and summary["degradation"] == [], str(summary))
    check("正例 current_value 非 null", summary["current_value"] is not None,
          str(summary.get("current_value")))
    check("正例 control_line.mean 非空",
          isinstance(summary.get("control_line"), dict)
          and summary["control_line"].get("mean") is not None,
          str(summary.get("control_line")))

    intent_doc = json.loads((outdir / "intent.json").read_text(encoding="utf-8"))
    check("intent.json 四字段齐全", intent_mod.check_a0(intent_doc) == [],
          str(intent_doc))
    scene_bytes = (outdir / "scene.json").read_bytes()
    check("scene.json sha256 一致",
          hashlib.sha256(scene_bytes).hexdigest() == summary["scene_sha256"])

    # 自然话术「B线」-> 数据集枚举「B」的确定性归一化（2026-10-23 t14 实测缺口）。
    outdir = Path(tempfile.mkdtemp(prefix="intent-norm-"))
    proc = _run_cli(["--skip-llm", "--scene-id", "scene-refill-rate-first",
                     "--line", "B线", "--window", "T7"], outdir)
    check("「B线」归一化后 exit 0", proc.returncode == 0,
          f"rc={proc.returncode} out={proc.stdout}")
    intent_doc = json.loads((outdir / "intent.json").read_text(encoding="utf-8"))
    check("「B线」intent 记录归一化值 B",
          intent_doc["entities"]["production_line"] == "B", str(intent_doc))
    check("「B线」产出场景卡", (outdir / "scene.json").exists())


def case_cli_negative():
    outdir = Path(tempfile.mkdtemp(prefix="intent-empty-"))
    proc = _run_cli(["--skip-llm"], outdir)
    check("空解析 exit 3 未命中", proc.returncode == 3, f"rc={proc.returncode} out={proc.stdout}")
    check("空解析列出可选场景", json.loads(proc.stdout)["options"], proc.stdout)

    outdir = Path(tempfile.mkdtemp(prefix="intent-badline-"))
    proc = _run_cli(["--skip-llm", "--scene-id", "scene-refill-rate-first",
                     "--line", "Z", "--window", "T7"], outdir)
    check("幽灵产线 exit 5", proc.returncode == 5, f"rc={proc.returncode} out={proc.stdout}")
    body = json.loads(proc.stdout)
    check("幽灵产线禁编造", body["status"] == "data_insufficient"
          and "Z" in body["reason"] and "禁止编造" in body["reason"], proc.stdout)
    check("幽灵产线不出场景卡", not (outdir / "scene.json").exists())
    intent_doc = json.loads((outdir / "intent.json").read_text(encoding="utf-8"))
    check("幽灵产线 intent 标注数据不足",
          intent_doc["status"] == "data_insufficient" and intent_doc.get("reason"),
          str(intent_doc))


def case_variant_c_degradation(pack):
    _, rec_c = intent_mod.load_records("demo/material_refill_T_variantC_missing.json")
    _, rec_b = intent_mod.load_records("demo/material_refill_T30.json")
    seed = scene_mod.load_seed("scene-refill-rate-first", pack)
    manifest = {"fields": sorted(intent_mod.usable_fields(rec_c)
                                 & intent_mod.usable_fields(rec_b))}
    degradation = scene_mod.degradation_reasons(seed, manifest)
    joined = "；".join(degradation)
    check("variantC 触发降级", len(degradation) >= 2, joined)
    check("variantC 缺 refill_rate 与 mold_status",
          "refill_rate" in joined and "mold_status" in joined, joined)
    check("variantC 不误伤 maintenance_records", "maintenance_records" not in joined, joined)


def main():
    pack = pack_mod.load()
    catalog = case_catalog(pack)
    case_parse_llm_json()
    case_resolve_intent(catalog)
    case_check_a0()
    case_mini_ws()
    case_cli_happy()
    case_cli_negative()
    case_variant_c_degradation(pack)
    print(f"\n== test_intent_l0: {CHECKS} checks, {len(FAILS)} failed ==")
    if FAILS:
        for name in FAILS:
            print(f"  FAIL: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
