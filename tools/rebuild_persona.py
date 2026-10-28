#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经营参谋团 · 人格层重建（女娲 → agent md）

纪律：**人格层与职责层分离**。
  - 人格层（心智模型 / 决策启发式 / 表达纪律）由女娲蒸馏产物生成，自动写入
    agents/<role>.md 的 PERSONA 块；
  - 职责层（职责边界 / 场景输出契约 / 禁止）是工程资产，本脚本**一字不改**。

用法
  python tools/rebuild_persona.py --status    # 看源、看 inbox 积压
  python tools/rebuild_persona.py --check     # 漂移检查（漂移=非零退出）
  python tools/rebuild_persona.py --apply     # 重写 PERSONA 块

环境
  ADVISORY_PACK  覆盖 pack 路径（默认 <bot-2>/.octop/expert_market/manufacturing-advisory-board）
  NUWA_ROOT      覆盖女娲 examples 路径（默认见 ROLES 表）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # JuJing/bot-team
DEFAULT_PACK = ROOT.parents[1] / ".octop" / "expert_market" / "manufacturing-advisory-board"
DEFAULT_NUWA = Path(r"C:\Users\kf-41\Desktop\个人内容\skill\nuwa-skill-main\nuwa-skill-main\examples")

sys.path.insert(0, str(ROOT))
from core import telemetry  # noqa: E402

# role -> (agent 文件, persona 目录, 女娲 example 目录名)
ROLES = {
    "karpathy": ("agents/karpathy.md", "persona/andrej-karpathy", "andrej-karpathy-perspective"),
    "jobs": ("agents/jobs.md", "persona/steve-jobs", "steve-jobs-perspective"),
}

START_RE = re.compile(r"<!--\s*PERSONA:START(?P<attrs>[^>]*)-->")
END = "<!-- PERSONA:END -->"

MODEL_HEAD_RE = re.compile(r"^##\s+.*(六个核心)?心智模型\s*$")
HEUR_HEAD_RE = re.compile(r"^##\s+决策启发式\s*$")
MODEL_ITEM_RE = re.compile(r"^###\s+模型[一二三四五六七八九\d]+\s*[:：]\s*(?P<title>.+?)\s*$")
HEUR_ITEM_RE = re.compile(r"^\d+\.\s+\*\*(?P<title>.+?)\*\*")


def pack_dir() -> Path:
    return Path(os.environ.get("ADVISORY_PACK") or DEFAULT_PACK)


def nuwa_root() -> Path:
    return Path(os.environ.get("NUWA_ROOT") or DEFAULT_NUWA)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def extract_section_items(text: str, head_re: re.Pattern, item_re: re.Pattern) -> list[str]:
    """抓取某个二级标题下、匹配 item_re 的标题，直到下一个二级标题。"""
    items: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if head_re.match(line):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            m = item_re.match(line)
            if m:
                items.append(m.group("title").strip())
    return items


def build_block(role: str, persona_rel: str, example: str) -> str:
    src_skill = nuwa_root() / example / "SKILL.md"
    voice_path = pack_dir() / persona_rel / "VOICE.md"
    if not src_skill.exists():
        raise FileNotFoundError(f"女娲源不存在：{src_skill}（用 NUWA_ROOT 覆盖）")
    if not voice_path.exists():
        raise FileNotFoundError(f"缺少表达纪律：{voice_path}")

    skill = read(src_skill)
    models = extract_section_items(skill, MODEL_HEAD_RE, MODEL_ITEM_RE)
    heurs = extract_section_items(skill, HEUR_HEAD_RE, HEUR_ITEM_RE)
    if len(models) < 6 or len(heurs) < 8:
        raise ValueError(f"{example} 抽取不足：模型 {len(models)}/6，启发式 {len(heurs)}/8（源改版？先修 VOICE/正则）")

    voice = read(voice_path)
    # 去掉 VOICE.md 的一级标题与引言，只保留正文纪律
    voice_body = "\n".join(
        line for line in voice.splitlines()
        if not line.startswith("# ") and not line.startswith("> ")
    ).strip()

    body = [
        f"## 人格层（自动生成 · 源 {persona_rel} · 勿手改）",
        "",
        f"> 认知来自女娲蒸馏包 `{example}`；风格来自 `{persona_rel}/VOICE.md`。",
        "> 重刷：`python tools/rebuild_persona.py --apply`（只换人格层，职责层与输出契约不动）。",
        "",
        "**心智模型（6）**",
        "",
        "\n".join(f"{i}. {m}" for i, m in enumerate(models, 1)),
        "",
        "**决策启发式（8）**",
        "",
        "\n".join(f"{i}. {h}" for i, h in enumerate(heurs, 1)),
        "",
        voice_body,
    ]
    return "\n".join(body).strip() + "\n"


def digest(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


def locate_span(text: str) -> tuple[int, int, str]:
    m = START_RE.search(text)
    if not m:
        raise ValueError("缺少 <!-- PERSONA:START --> 标记")
    end = text.find(END, m.end())
    if end < 0:
        raise ValueError("缺少 <!-- PERSONA:END --> 标记")
    return m.end(), end, m.group("attrs") or ""


def check_role(role: str, agent_rel: str, persona_rel: str, example: str) -> tuple[bool, str]:
    agent_path = pack_dir() / agent_rel
    if not agent_path.exists():
        return False, f"[{role}] agent 文件缺失：{agent_path}"
    text = read(agent_path)
    try:
        start, end, attrs = locate_span(text)
    except ValueError as exc:
        return False, f"[{role}] {exc}"
    if "source=manual" in attrs:
        return True, f"[{role}] 手工人格（跳过重建）"
    current = text[start:end].strip("\n")
    try:
        expected = build_block(role, persona_rel, example).strip("\n")
    except (FileNotFoundError, ValueError) as exc:
        return False, f"[{role}] 无法生成：{exc}"
    if current != expected:
        return False, f"[{role}] 人格层漂移（sha {digest(current)[:8]} ≠ {digest(expected)[:8]}）——跑 --apply"
    return True, f"[{role}] OK sha {digest(expected)[:8]}"


def apply_role(role: str, agent_rel: str, persona_rel: str, example: str) -> tuple[bool, str]:
    agent_path = pack_dir() / agent_rel
    text = read(agent_path)
    start, end, attrs = locate_span(text)
    if "source=manual" in attrs:
        return True, f"[{role}] 手工人格（跳过）"
    block = build_block(role, persona_rel, example)
    new_text = text[:start] + "\n" + block + text[end:]
    if new_text != text:
        agent_path.write_text(new_text, encoding="utf-8", newline="\n")
        # 埋点不得中断业务
        try:
            props = {"agent_id": role, "prompt_hash": telemetry.text_hash(new_text),
                     "role": role, "source": "rebuild_persona"}
            voice = pack_dir() / persona_rel / "VOICE.md"
            if voice.is_file():
                props["persona_hash"] = telemetry.file_hash(voice)
            telemetry.emit("role_snapshot", props)
        except Exception:  # noqa: BLE001
            pass
        return True, f"[{role}] 已刷新 sha {digest(block.strip(chr(10)))[:8]}"
    return True, f"[{role}] 无变化"


def status() -> int:
    print(f"pack   = {pack_dir()}")
    print(f"nuwa   = {nuwa_root()}")
    rows = []
    for role, (agent_rel, persona_rel, example) in ROLES.items():
        inbox = pack_dir() / persona_rel / "inbox"
        pending = sorted(p.name for p in inbox.glob("*.md")) if inbox.is_dir() else []
        rows.append((role, example, len(pending), ",".join(pending) or "-"))
    for role, example, n, names in rows:
        print(f"  {role:<9} src={example:<30} inbox={n} ({names})")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)

    if not any((args.apply, args.check, args.status)):
        ap.print_help()
        return 2

    if args.status:
        return status()

    ok_all = True
    for role, (agent_rel, persona_rel, example) in ROLES.items():
        if args.apply:
            try:
                ok, msg = apply_role(role, agent_rel, persona_rel, example)
            except (FileNotFoundError, ValueError) as exc:
                ok, msg = False, f"[{role}] {exc}"
        else:
            ok, msg = check_role(role, agent_rel, persona_rel, example)
        print(("PASS " if ok else "FAIL ") + msg)
        ok_all = ok_all and ok

    # inbox 只告警不阻断：素材积压不是结构缺陷
    for role, (_a, persona_rel, _e) in ROLES.items():
        inbox = pack_dir() / persona_rel / "inbox"
        if inbox.is_dir():
            pend = sorted(p.name for p in inbox.glob("*.md"))
            if pend:
                print(f"WARN [{role}] inbox 积压 {len(pend)} 条：{', '.join(pend)}")

    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
