#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IM 签字白名单与签字流程的离线测试（不联网、不启服务）。

覆盖两类容易出事的点：
1. 白名单的 fail-closed —— 配置坏了必须拒人，不能退化成"谁都能签"；
2. 签字前必须先过身份 —— 未授权的人连 /sign 都不该被调（用假 HTTP 断言调用次数）。

跑法：python -B tests/test_im_signers.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import im_sign  # noqa: E402
import im_signers  # noqa: E402

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  PASS " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ("  <<< " + str(detail) if detail else ""))


def write_cfg(tmp, payload):
    p = Path(tmp) / "im_signers.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_identity():
    with tempfile.TemporaryDirectory() as tmp:
        # 1. 文件不存在 → legacy 兼容
        missing = str(Path(tmp) / "nope.json")
        who = im_signers.resolve(im_signers.LEGACY_OPEN_ID, path=missing)
        check("无配置文件时 legacy open_id 仍可签", who and who["signer"] == "老板", who)
        check("无配置文件时 legacy 标记来源", who and who["source"] == "legacy", who)
        check("无配置文件时陌生人被拒",
              im_signers.resolve("ou_unknown_user_0001", path=missing) is None)

        # 2. 正常配置
        cfg = write_cfg(tmp, {"signers": [
            {"id": "ou_boss_00011234567890", "name": "Mo", "signer": "老板"},
            {"id": "ou_guest_0002", "name": "访客", "signer": "老板", "enabled": False},
            {"id": "ou_chat_0003", "name": "限定群", "signer": "boss", "chat_id": "oc_abc"},
        ]})
        who = im_signers.resolve("ou_boss_00011234567890", path=cfg)
        check("白名单命中且署名正确", who and who["signer"] == "老板", who)
        check("白名单命中标记 source=file", who and who["source"] == "file", who)
        check("enabled=false 被拒", im_signers.resolve("ou_guest_0002", path=cfg) is None)
        check("限定群命中", im_signers.resolve("ou_chat_0003", chat_id="oc_abc", path=cfg) is not None)
        check("限定群外的消息被拒",
              im_signers.resolve("ou_chat_0003", chat_id="oc_other", path=cfg) is None)
        check("限定群但没给会话被拒", im_signers.resolve("ou_chat_0003", path=cfg) is None)
        check("不在白名单被拒", im_signers.resolve("ou_nobody_0009", path=cfg) is None)
        check("legacy id 不在白名单时不放行",
              im_signers.resolve(im_signers.LEGACY_OPEN_ID, path=cfg) is None)
        check("空 sender 被拒", im_signers.resolve("", path=cfg) is None)

        # 3. fail-closed
        bad = Path(tmp) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        try:
            im_signers.load(str(bad))
            check("坏 JSON 必须抛错", False)
        except im_signers.SignerConfigError as exc:
            check("坏 JSON 必须抛错", "不可解析" in str(exc), exc)

        empty = write_cfg(tmp, {"signers": []})
        check("空名单拒绝所有人（含 legacy）",
              im_signers.resolve(im_signers.LEGACY_OPEN_ID, path=empty) is None)

        no_signer = Path(tmp) / "nosign.json"
        no_signer.write_text(json.dumps({"signers": [{"id": "ou_x"}]}), encoding="utf-8")
        try:
            im_signers.load(str(no_signer))
            check("缺 signer 必须抛错", False)
        except im_signers.SignerConfigError:
            check("缺 signer 必须抛错", True)

        bad_type = Path(tmp) / "badtype.json"
        bad_type.write_text(json.dumps({"signers": [{"id": "ou_x", "signer": "老板", "id_type": "phone"}]}),
                            encoding="utf-8")
        try:
            im_signers.load(str(bad_type))
            check("未知 id_type 必须抛错", False)
        except im_signers.SignerConfigError:
            check("未知 id_type 必须抛错", True)

        d = im_signers.describe(cfg)
        check("describe 不泄露完整 id",
              all("…" in s["id"] for s in d["signers"]), d)
        check("mask 短串不截断", im_signers.mask("ou_1") == "ou_1", im_signers.mask("ou_1"))


class FakeServer:
    """假服务：按 URL 后缀分发，记录被调用的路径。"""

    def __init__(self, state="assembled", sign_ok=True, final_ok=True, sha="deadbeef"):
        self.state = state
        self.sign_ok = sign_ok
        self.final_ok = final_ok
        self.sha = sha
        self.calls = []

    def __call__(self, method, url, body=None, timeout=30):
        self.calls.append(method + " " + url.split("/api/")[-1])
        tail = url.split("/api/")[-1]
        if tail.endswith("/delivery"):
            return {"ok": True, "signature_sha256": self.sha}
        if tail.endswith("/sign"):
            if not self.sign_ok:
                return {"ok": False, "error": "签名 sha 不一致"}
            return {"ok": True, "sha256": self.sha, "signer": (body or {}).get("signer")}
        if tail.endswith("/finalize"):
            if not self.final_ok:
                return {"ok": False, "error": "loop/ 不存在"}
            return {"ok": True, "decision": "D-TEST-001", "status": "approved"}
        return {"ok": True, "task_id": "T-TEST", "status": {"state": self.state}}


def test_sign_flow():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = write_cfg(tmp, {"signers": [{"id": "ou_boss_00011234567890", "name": "Mo", "signer": "老板"}]})
        runs = Path(tmp) / "runs"
        orig = im_sign._http

        # 未授权：一个 /api 调用都不该发生
        fake = FakeServer()
        im_sign._http = fake
        try:
            r = im_sign.sign_task("T-TEST", "ou_stranger", runs_dir=runs, signers_path=cfg)
            check("未授权 → ok=false 且 stage=identity", r["ok"] is False and r["stage"] == "identity", r)
            check("未授权 → 不触达服务端", fake.calls == [], fake.calls)
            check("未授权 → 不写审计", not (runs / "T-TEST" / "im-sign.json").exists())

            # dry-run：只读，不动签字
            fake = FakeServer()
            im_sign._http = fake
            r = im_sign.sign_task("T-TEST", "ou_boss_00011234567890", runs_dir=runs, signers_path=cfg, dry_run=True)
            check("dry-run → stage=dry-run", r["ok"] and r["stage"] == "dry-run", r)
            check("dry-run → 不调 sign/finalize",
                  not any("sign" in c or "finalize" in c for c in fake.calls), fake.calls)
            check("dry-run → 拿到将签的 sha", r.get("sha256") == "deadbeef", r)

            # 已签过：幂等
            fake = FakeServer(state="approved")
            im_sign._http = fake
            r = im_sign.sign_task("T-TEST", "ou_boss_00011234567890", runs_dir=runs, signers_path=cfg)
            check("已 approved → stage=already", r["ok"] and r["stage"] == "already", r)
            check("已 approved → 不重复签",
                  not any("/sign" in c for c in fake.calls), fake.calls)

            # 正常签字 + 审计旁证
            fake = FakeServer(state="assembled")
            im_sign._http = fake
            r = im_sign.sign_task("T-TEST", "ou_boss_00011234567890", chat_id="oc_1", message_id="om_9",
                                  runs_dir=runs, signers_path=cfg)
            check("签字成功 → stage=approved", r["ok"] and r["stage"] == "approved", r)
            check("签字成功 → 带决策号", r.get("decision") == "D-TEST-001", r)
            audit = runs / "T-TEST" / "im-sign.json"
            check("审计旁证已落盘", audit.exists(), audit)
            if audit.exists():
                doc = json.loads(audit.read_text(encoding="utf-8"))
                ev = doc["events"][-1]
                check("旁证记录 signer/来源/会话",
                      ev["signer"] == "老板" and ev["source"] == "file" and ev["chat_id"] == "oc_1", ev)
                check("旁证记录所签 sha", ev["sha256"] == "deadbeef", ev)
                check("旁证记录 im_id 是掩码", "…" in ev["im_id"], ev)

            # finalize 失败：审计要能定位到卡在哪
            fake = FakeServer(state="assembled", final_ok=False)
            im_sign._http = fake
            r = im_sign.sign_task("T-TEST", "ou_boss_00011234567890", runs_dir=runs, signers_path=cfg)
            check("finalize 失败 → stage=finalize", r["ok"] is False and r["stage"] == "finalize", r)
            doc = json.loads((runs / "T-TEST" / "im-sign.json").read_text(encoding="utf-8"))
            check("finalize 失败也留痕", doc["events"][-1]["status"] is None, doc["events"][-1])

            # sign 失败
            fake = FakeServer(state="assembled", sign_ok=False)
            im_sign._http = fake
            r = im_sign.sign_task("T-TEST", "ou_boss_00011234567890", runs_dir=runs, signers_path=cfg)
            check("sign 失败 → stage=sign", r["ok"] is False and r["stage"] == "sign", r)
        finally:
            im_sign._http = orig

        # 配置坏掉：返回 rc=2 而不是放行
        bad = str(Path(tmp) / "broken.json")
        Path(bad).write_text("{oops", encoding="utf-8")
        rc = im_sign.main(["--task-id", "T-TEST", "--sender", "ou_boss_00011234567890",
                           "--signers-file", bad, "--dry-run"])
        check("CLI 坏配置 rc=2", rc == 2, rc)


def main():
    test_identity()
    test_sign_flow()
    print()
    if FAILS:
        print("FAILED %d / %d: %s" % (len(FAILS), COUNT, ", ".join(FAILS)))
        return 1
    print("PASS 全绿（%d 项）" % COUNT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
