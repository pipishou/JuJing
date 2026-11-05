#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交付物版式机检（结论先行 / 一页纸 / 证据进附录）。

老板 2026-11-05：「bot 团队的输出物都不能直接使用，全是 AI 语言的逻辑，经常沉浸在细节里，
目标和任务往往放在不是核心的地方」。根因取证见
`docs/product/DELIVERY_QUALITY_ROOTCAUSE_2026-11-05.md`：SOUL 里没有一条约束**交付文档**
的版式，模型默认文风就成了交付物。

本文件只做一件事：把「能不能直接给老板看」变成 6 条可机检的判据。

判据（默认阈值，可用 --config 覆盖）：
  R1 conclusion_first   前 3 个非空行里必须出现「结论/建议/报价/值得/不做」之一，且不得是修订记录
  R2 body_length        正文（第一个「## 附录」之前）≤ 120 行
  R3 appendix_ratio     附录行数 / 总行数 ≤ 0.40
  R4 one_sentence       出现「一句话定义」时，同一行其后的说明 ≤ 40 字
  R5 no_process_talk    正文禁「上一轮/我说过/今天这一轮/自我复盘/变更日志/我之前的判断」
  R6 numbers_have_scope 正文中带 % 的行必须同时带「样本/窗口/实测/口径/N=」

用法：
    python -B tools/delivery_lint.py --file runs/T-x/deliverable.md
    python -B tools/delivery_lint.py --file x.md --json --config '{"body_length_max":160}'
退出码：0 合规 / 1 不合规 / 2 用法或读文件错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULTS = {
    "body_length_max": 120,
    "appendix_ratio_max": 0.40,
    "one_sentence_max": 40,
    "head_lines": 3,
}

CONCLUSION_WORDS = ("结论", "建议", "报价", "值得", "不做", "不要做", "可以签", "不可签")
META_WORDS = ("版本", "变更", "修订", "取代", "本文件为", "v1.", "v2.", "v3.")
PROCESS_WORDS = ("上一轮", "我说过", "今天这一轮", "自我复盘", "变更日志", "我之前的判断", "之前我说")
SCOPE_WORDS = ("样本", "窗口", "实测", "口径", "N=", "n=", "条", "天")
APPENDIX_RE = re.compile(r"^#{1,3}\s*(附录|Appendix)", re.I)
ONE_SENT_RE = re.compile(r"一句话定义[^\n]*")
ADJ_RE = re.compile(r"^\[(附录|仅供核查)\]")  # 兼容前缀


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _is_blank(line: str) -> bool:
    return not line.strip() or line.strip().startswith("---")


def _first_lines(lines: list[str], n: int) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(lines, 1):
        if _is_blank(line):
            continue
        out.append((i, line.strip()))
        if len(out) >= n:
            break
    return out


def _split_appendix(lines: list[str]) -> tuple[int, int]:
    """返回 (正文行数, 附录起始行号 1-based；无附录则 0)。"""
    for i, line in enumerate(lines, 1):
        if APPENDIX_RE.match(line.strip()):
            body = sum(1 for l in lines[: i - 1] if l.strip())
            return body, i
    return sum(1 for l in lines if l.strip()), 0


def lint(text: str, config: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    if config:
        cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
    lines = _lines(text)
    errors: list[dict] = []
    warnings: list[dict] = []

    # R1 结论先行
    head = _first_lines(lines, int(cfg["head_lines"]))
    head_text = " ".join(t for _, t in head)
    if not head_text:
        errors.append({"rule": "conclusion_first", "line": 0, "msg": "文档为空"})
    else:
        if not any(w in head_text for w in CONCLUSION_WORDS):
            errors.append({"rule": "conclusion_first", "line": head[0][0],
                           "msg": f"前 {cfg['head_lines']} 个非空行没有结论/建议/报价之类的话"})
        if any(w in head_text for w in META_WORDS):
            errors.append({"rule": "conclusion_first", "line": head[0][0],
                           "msg": "前几行是版本/修订记录，不是结论——修订记录应进附录"})

    # R2/R3 正文长度与附录占比
    body_lines, app_start = _split_appendix(lines)
    if body_lines > int(cfg["body_length_max"]):
        errors.append({"rule": "body_length", "line": app_start or len(lines),
                       "msg": f"正文 {body_lines} 行 > {cfg['body_length_max']} 行（一页纸放不下）"})
    total = sum(1 for l in lines if l.strip())
    app_lines = total - body_lines if app_start else 0
    ratio = (app_lines / total) if total else 0.0
    if ratio > float(cfg["appendix_ratio_max"]):
        errors.append({"rule": "appendix_ratio", "line": app_start or 0,
                       "msg": f"附录占 {ratio:.0%} > {cfg['appendix_ratio_max']:.0%}（证据压过了结论）"})

    # R4 一句话定义
    for i, line in enumerate(lines, 1):
        m = ONE_SENT_RE.search(line)
        if not m:
            continue
        seg = line[m.start():]
        seg = re.sub(r"^一句话定义[：:]\s*", "", seg)
        seg = seg.strip().lstrip("*").strip()
        if len(seg) > int(cfg["one_sentence_max"]):
            errors.append({"rule": "one_sentence", "line": i,
                           "msg": f"「一句话定义」实际 {len(seg)} 字 > {cfg['one_sentence_max']} 字"})
        break

    # R5 过程反思禁入正文
    body_end = (app_start - 1) if app_start else len(lines)
    for i, line in enumerate(lines[:body_end], 1):
        for w in PROCESS_WORDS:
            if w in line:
                errors.append({"rule": "no_process_talk", "line": i,
                               "msg": f"正文出现过程反思用词「{w}」（复盘进附录）"})
                break

    # R6 百分比必须带口径
    for i, line in enumerate(lines[:body_end], 1):
        if "%" not in line and "％" not in line:
            continue
        if not any(w in line for w in SCOPE_WORDS):
            warnings.append({"rule": "numbers_have_scope", "line": i,
                             "msg": "该行有百分比但没写样本/窗口/口径"})

    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "stats": {"total_lines": total, "body_lines": body_lines,
                  "appendix_start": app_start,
                  "appendix_ratio": round(ratio, 4),
                  "head": [t[:60] for _, t in head]},
        "config": cfg,
    }


def required_fixes(result: dict) -> list[str]:
    return [f"{e['rule']}（第 {e['line']} 行）：{e['msg']}" for e in result.get("errors", [])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="交付物版式机检")
    ap.add_argument("--file", required=True, help="交付物 markdown 路径")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--config", default="", help='JSON，例如 {"body_length_max":160}')
    a = ap.parse_args(argv)
    p = Path(a.file)
    if not p.is_file():
        print(json.dumps({"ok": False, "error": f"文件不存在：{p}"}, ensure_ascii=False))
        return 2
    try:
        cfg = json.loads(a.config) if a.config.strip() else None
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"config 不是合法 JSON：{exc}"}, ensure_ascii=False))
        return 2
    result = lint(p.read_text(encoding="utf-8", errors="replace"), cfg)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        s = result["stats"]
        print(f"{'PASS' if result['ok'] else 'FAIL'}  总 {s['total_lines']} 行 / 正文 {s['body_lines']} 行"
              f" / 附录 {s['appendix_ratio']:.0%}")
        for e in result["errors"]:
            print(f"  ✗ {e['rule']} 第 {e['line']} 行：{e['msg']}")
        for w in result["warnings"]:
            print(f"  ! {w['rule']} 第 {w['line']} 行：{w['msg']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
