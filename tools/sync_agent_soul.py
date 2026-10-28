"""把 Expert Pack 的 SOUL / 子角色 / 人格文件同步到已创建的活 Agent，并刷新 system_prompt。

为什么需要：Octop 的 `agent from-expert` 只把 pack 文件拷进工作区，不会把 SOUL.md
写进 agents.system_prompt；而行之有效的团队 agent（如 JJG00N）都是 system_prompt 非空。
本脚本以 pack 为单一事实源，做三件事：

1. 拷贝 pack 的 SOUL.md + agents/*.md + persona/* 到 agent 工作区
2. UPDATE agents.system_prompt = SOUL.md 全文
3. 调 `octop agent reload` 让图重编译

用法：
    python tools/sync_agent_soul.py --agent-id 9BZAQT
    python tools/sync_agent_soul.py --agent-id 9BZAQT --check   # 只比对不写
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # JuJing/bot-team
BOT2 = ROOT.parents[1]
OCTOP_HOME = BOT2 / ".octop"
DB = OCTOP_HOME / "octop.db"
DEFAULT_PACK = OCTOP_HOME / "expert_market" / "manufacturing-advisory-board"
# 必须写 Provider/model 全引用：只写 "deepseek-v4-flash" 会被判不可用，
# 静默回退到 settings.active_model（曾因此连撞 glm 免费额度 429）。
DEFAULT_MODEL = os.environ.get("ADVISORY_MODEL", "Zhipu/glm-4.7-flash")

sys.path.insert(0, str(ROOT))
from core import telemetry  # noqa: E402


def _target_workspace(agent_id: str) -> Path:
    return OCTOP_HOME / "agents" / agent_id


def _plan_copies(pack: Path, ws: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = [(pack / "SOUL.md", ws / "SOUL.md")]
    for sub in ("agents", "persona"):
        src_root = pack / sub
        if not src_root.is_dir():
            continue
        for src in sorted(src_root.rglob("*")):
            if src.is_file():
                pairs.append((src, ws / sub / src.relative_to(src_root)))
    return pairs


def _read_db_prompt(agent_id: str) -> str:
    con = sqlite3.connect(DB)
    try:
        row = con.execute(
            "SELECT system_prompt FROM agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(f"agent {agent_id} not found in DB")
    return row[0] or ""


def _write_db_prompt(agent_id: str, text: str, model: str) -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "UPDATE agents SET system_prompt=?, default_model=? WHERE agent_id=?",
            (text, model, agent_id),
        )
        con.commit()
    finally:
        con.close()


def _reload(agent_id: str) -> int:
    env = dict(os.environ)
    env["OCTOP_HOME"] = str(OCTOP_HOME)
    proc = subprocess.run(
        [sys.executable, "-m", "octop", "agent", "reload", agent_id],
        cwd=str(BOT2),
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--pack", default=str(DEFAULT_PACK))
    ap.add_argument("--check", action="store_true", help="只比对，不写入")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Provider/model 全引用")
    args = ap.parse_args()

    pack = Path(args.pack)
    ws = _target_workspace(args.agent_id)
    soul = (pack / "SOUL.md").read_text(encoding="utf-8")

    if args.check:
        drift = []
        for src, dst in _plan_copies(pack, ws):
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                drift.append(str(dst.relative_to(ws)))
        if _read_db_prompt(args.agent_id).strip() != soul.strip():
            drift.append("DB.system_prompt")
        if drift:
            print("DRIFT:", ", ".join(drift))
            return 1
        print("OK: 工作区文件与 DB system_prompt 均与 pack 一致")
        return 0

    copied = 0
    for src, dst in _plan_copies(pack, ws):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
            copied += 1
            print("updated:", dst.relative_to(ws))
    try:
        before_hash = telemetry.text_hash(_read_db_prompt(args.agent_id))
    except (SystemExit, Exception):  # noqa: BLE001
        before_hash = ""          # agent 不存在：无旧指纹，也不报 role_change
    _write_db_prompt(args.agent_id, soul, args.model)
    print(f"system_prompt 已写入 DB（{len(soul)} 字符），model={args.model}，copied={copied}")

    # 埋点不得中断业务
    try:
        after_hash = telemetry.text_hash(soul)
        telemetry.emit("role_snapshot", {
            "agent_id": args.agent_id, "prompt_hash": after_hash,
            "source": "sync_agent_soul", "model": args.model,
        })
        if before_hash and before_hash != after_hash:
            telemetry.emit("role_change", {
                "agent_id": args.agent_id, "field": "system_prompt",
                "before_hash": before_hash, "after_hash": after_hash,
                "reason": "sync_agent_soul", "actor": "sync_agent_soul",
            })
    except Exception:  # noqa: BLE001
        pass

    rc = _reload(args.agent_id)
    print(f"reload exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
