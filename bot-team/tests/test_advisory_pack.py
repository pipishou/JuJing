#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经营参谋团 pack 机检（空壳员工防线）

判据分三层：
  L1 结构：manifest 必备键、agent frontmatter(`id` / `tools: []`)、PERSONA 标记；
  L2 场景：每个角色必须有「职责边界 + 场景输出契约 + 禁止」三节，
          且契约里必须写清 输入 / 必须产出 / 验收口径 —— 缺一节 = 空壳角色；
  L3 可更新：人格层由女娲源生成且无漂移（rebuild_persona.py --check 退出 0），
          且 persona/<dir>/ 留有增量采集入口（SOURCES.md + inbox/）。

用法：python tests/test_advisory_pack.py
非零退出 = 不得声称「团队组建完成」。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = Path(os.environ.get("ADVISORY_PACK")
            or ROOT.parents[1] / ".octop" / "expert_market" / "manufacturing-advisory-board")
PY = sys.executable

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- L1 结构
def check_manifest() -> None:
    p = PACK / "manifest.json"
    ok(p.exists(), f"manifest 存在：{p}")
    if not p.exists():
        return
    m = json.loads(read(p))
    ok(m.get("id") == "manufacturing-advisory-board", "manifest.id 正确")
    for key in ("label", "description", "welcome_message"):
        for lang in ("zh", "en"):
            ok(bool((m.get(key) or {}).get(lang)), f"manifest.{key}.{lang} 非空")
    qps = m.get("quick_prompts") or []
    ok(len(qps) >= 3, f"quick_prompts ≥3（实际 {len(qps)}）—— 每个入口必须对应真实场景")
    for i, q in enumerate(qps, 1):
        ok(bool(q.get("title", {}).get("zh")) and bool(q.get("prompt", {}).get("zh")),
           f"quick_prompts#{i} 有 zh title+prompt")
        ok(bool(q.get("description", {}).get("zh")), f"quick_prompts#{i} 有 zh description（场景一句话）")
    pfs = m.get("prompt_files") or []
    ok(bool(pfs), "manifest.prompt_files 非空")
    for f in pfs:
        ok((PACK / f).is_file(), f"prompt_files 存在：{f}")


def check_agents() -> None:
    expected = {"karpathy", "jobs", "manufacturing-ops-expert", "content-operator"}
    found = {p.stem for p in (PACK / "agents").glob("*.md")}
    ok(found == expected, f"agents 名单与设计一致：{sorted(found)}")
    for stem in sorted(expected & found):
        path = PACK / "agents" / f"{stem}.md"
        text = read(path)
        fm = frontmatter(text)
        ok(fm.get("id") == stem, f"[{stem}] frontmatter.id == 文件名（{fm.get('id')!r}）")
        # 项目硬约束：空值/default 会继承父 agent 全部工具，只有 [] 才真禁用
        ok(fm.get("tools") == "[]", f"[{stem}] tools: [] 硬限制（实际 {fm.get('tools')!r}）")
        ok(len(fm.get("description", "")) >= 10, f"[{stem}] description 非空且含场景")
        ok(("PERSONA:START" in text) and ("PERSONA:END" in text), f"[{stem}] PERSONA 标记成对")
        ok(text.find("PERSONA:START") < text.find("PERSONA:END"), f"[{stem}] PERSONA 标记顺序正确")


# ---------------------------------------------------------------- L2 场景契约
def check_scene_contracts() -> None:
    required_sections = ["## 职责边界", "## 场景输出契约", "## 禁止"]
    for stem in ("karpathy", "jobs", "manufacturing-ops-expert", "content-operator"):
        text = read(PACK / "agents" / f"{stem}.md")
        for sec in required_sections:
            ok(sec in text, f"[{stem}] 有「{sec}」")
        contract = text.split("## 场景输出契约", 1)[1] if "## 场景输出契约" in text else ""
        contract = contract.split("\n## ", 1)[0]
        # 场景必须点明"什么时候用"，否则就是无场景的人格空壳
        ok(bool(re.search(r"\*\*场景", contract)), f"[{stem}] 契约点名了场景（**场景**）")
        ok("输入" in contract, f"[{stem}] 契约写明输入")
        ok(("必须产出" in contract) or ("必须交付" in contract), f"[{stem}] 契约写明必须产出")
        ok(("验收" in contract) or ("格式" in contract), f"[{stem}] 契约写明产出格式/验收口径")
        ok(len(contract) >= 300, f"[{stem}] 契约有实质内容（{len(contract)} 字符）")
        # 空壳防线：人格层之外必须有职责与契约，不能只剩人设
        persona = text.split("PERSONA:END", 1)[1] if "PERSONA:END" in text else ""
        ok(len(persona.strip()) >= 500, f"[{stem}] 职责层内容 ≥500 字符（{len(persona.strip())}）")


def check_host_soul() -> None:
    soul = read(PACK / "SOUL.md")
    for token, label in [
        ("s1_ai_reliability", "DAG 有 s1 卡帕西"),
        ("s2_ops_advice", "DAG 有 s2 业务专家"),
        ("s3_product_verdict", "DAG 有 s3 乔布斯"),
        ("depends_on: [s1_ai_reliability, s2_ops_advice]", "s3 等 s1+s2（串行收口）"),
        ("验收表", "有逐条验收表"),
        ("A8", "验收表到 A8"),
        ("决策日志", "有决策日志"),
        ("T+7", "有回看行"),
        ("securityGate", "有不可逆动作硬门"),
        # 2026-10-26 实测教训：DAG 写得再全，只要没点名派发工具，host 就会自己单干
        ("`task`", "SOUL 点名派发工具 task"),
        ("subagent_type", "SOUL 写明 task 的 subagent_type 参数"),
        ("general-purpose", "SOUL 显式禁止 general-purpose"),
        ("同一轮并发", "SOUL 要求并行派发（同一轮多个 task 调用）"),
        # 2026-10-26 s5 实测：并行派发的 result 会被整批丢失，host 必须自己重发
        ("没拿到 result", "SOUL 要求 task 无 result 时重发"),
        ("数据纪律", "SOUL 有数据纪律（禁止编造输入）"),
        ("禁止补造", "SOUL 禁止补造缺失数据"),
    ]:
        ok(token in soul, f"SOUL：{label}")
    ok("不替" in soul, "SOUL：禁止替专家发言（空壳防线）")
    for slug in ("karpathy", "manufacturing-ops-expert", "jobs", "content-operator"):
        ok(slug in soul, f"SOUL：点名可派发对象 {slug}")


# ---------------------------------------------------------------- L4 活体接线
def check_live_agent() -> None:
    """SOUL 必须真的进了 system_prompt，否则 pack 再对也不会执行 DAG。"""
    agent_id = os.environ.get("ADVISORY_AGENT_ID", "9BZAQT")
    db = ROOT.parents[1] / ".octop" / "octop.db"
    ok(db.is_file(), f"octop.db 存在：{db}")
    if not db.is_file():
        return
    import sqlite3

    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT system_prompt FROM agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
    finally:
        con.close()
    ok(row is not None, f"活体 agent {agent_id} 存在")
    if row is None:
        return
    prompt = (row[0] or "").strip()
    ok(len(prompt) >= 3000, f"[{agent_id}] DB system_prompt 非空（{len(prompt)} 字符）")
    ok("验收表" in prompt, f"[{agent_id}] system_prompt 含验收表 —— SOUL 已真正注入")
    ok("`task`" in prompt, f"[{agent_id}] system_prompt 含派发协议")


# ---------------------------------------------------------------- L3 可更新性
def check_update_pipeline() -> None:
    script = ROOT / "tools" / "rebuild_persona.py"
    ok(script.exists(), "人格重建脚本存在")
    if not script.exists():
        return
    proc = subprocess.run([PY, str(script), "--check"], capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    ok(proc.returncode == 0, "人格层无漂移（--check 退出 0）")

    for role_dir in ("persona/andrej-karpathy", "persona/steve-jobs", "persona/manufacturing-content-operator"):
        src = read(PACK / role_dir / "SOURCES.md")
        ok("女娲" in src, f"{role_dir}/SOURCES.md 记录女娲源")
        ok("inbox" in src, f"{role_dir}/SOURCES.md 有增量采集入口")
        # 声明了入口就必须真的有目录，否则「可更新」只是口号
        ok((PACK / role_dir / "inbox").is_dir(), f"{role_dir}/inbox/ 物理存在")
        ok((PACK / role_dir / "ingested").is_dir(), f"{role_dir}/ingested/ 物理存在")
        ok((PACK / role_dir / "VOICE.md").is_file(), f"{role_dir}/VOICE.md 存在（表达纪律可改）")

    ops = read(PACK / "agents" / "manufacturing-ops-expert.md")
    ok("source=manual" in ops, "业务专家人格标记为手工（无女娲源，诚实声明）")


def main() -> int:
    print(f"pack = {PACK}")
    check_manifest()
    check_agents()
    check_scene_contracts()
    check_host_soul()
    check_live_agent()
    check_update_pipeline()
    print("-" * 60)
    if FAILS:
        print(f"FAIL 共 {len(FAILS)} 项：")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("PASS 全绿：四个角色均有场景契约，人格层可更新且无漂移")
    return 0


if __name__ == "__main__":
    sys.exit(main())
