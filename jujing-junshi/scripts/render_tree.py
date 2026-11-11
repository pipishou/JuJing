#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_tree.py — 把内建思考树渲染成一页 review.html（军师 v0.8 P4 视觉伴随）。

用法：
  python scripts/render_tree.py --dir "../AI培训教程/.junshi/brainstorm/2026-10-29-ai-course"
  python scripts/render_tree.py --dir <目录> --out review.html

读：tree.md（根问题/成功判据/早停条件）＋ B*.md（分支字段）。
写：<目录>/review.html（无外部依赖、可离线打开；不覆盖 tree.md/B*.md）。
边界：只渲染已有产物，不生成结论；MECE 两问由人/军师回答后回填 tree.md。
"""
import argparse
import html
import re
from pathlib import Path

FIELDS = [
    ("一句话", ("一句话", "核心假设", "假设")),
    ("技法", ("技法",)),
    ("反证信号", ("反证信号", "反证")),
    ("成本", ("成本",)),
    ("可逆性", ("可逆性", "可逆")),
    ("结论", ("结论", "取舍")),
]


def read_fields(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*[-*]\s*\**([^*:：]{1,12})\**\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        out.setdefault(key, val)
    return out


def pick(fields: dict, names) -> str:
    for k, v in fields.items():
        if any(n in k for n in names):
            return v
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="思考树目录（含 tree.md / B*.md）")
    ap.add_argument("--out", default="review.html")
    a = ap.parse_args()
    d = Path(a.dir).resolve()
    if not d.is_dir():
        raise SystemExit(f"目录不存在: {d}")

    tree = read_fields(d / "tree.md")
    topics = {k: pick(tree, [k]) for k in ("根问题", "成功判据", "早停条件")}

    branches = []
    for f in sorted(d.glob("B*.md")):
        fl = read_fields(f)
        row = {"id": f.stem, "file": f.name}
        for label, names in FIELDS:
            row[label] = pick(fl, names)
        branches.append(row)

    def esc(x: str) -> str:
        return html.escape(x or "—")

    cells = "".join(
        f"<tr><td><b>{esc(b['id'])}</b><div class='muted'>{esc(b['file'])}</div></td>"
        f"<td>{esc(b['技法'])}</td><td>{esc(b['一句话'])}</td><td>{esc(b['反证信号'])}</td>"
        f"<td>{esc(b['成本'])}</td><td>{esc(b['可逆性'])}</td><td>{esc(b['结论'])}</td></tr>"
        for b in branches
    ) or "<tr><td colspan='7' class='muted'>未找到 B*.md 分支文件</td></tr>"

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>思考树对比 · {esc(topics['根问题'])}</title>
<style>
body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#1f2328}}
h1{{font-size:18px;margin:0 0 4px}} .muted{{color:#6b7280;font-size:12px}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #d0d7de;padding:8px;vertical-align:top;text-align:left}}
th{{background:#f6f8fa}} tr:nth-child(even) td{{background:#fbfcfd}}
.k{{display:flex;gap:24px;flex-wrap:wrap;margin:8px 0}} .k div{{min-width:220px}}
</style></head><body>
<h1>思考树对比 · {esc(topics['根问题'])}</h1>
<div class="muted">成功判据：{esc(topics['成功判据'])} ｜ 早停条件：{esc(topics['早停条件'])}</div>
<table>
<tr><th>分支</th><th>技法</th><th>一句话</th><th>反证信号</th><th>成本</th><th>可逆性</th><th>结论</th></tr>
{cells}
</table>
<div class="k">
<div><b>MECE·互斥</b><br>分支之间是否互相覆盖/重复？重复→合并同类项。</div>
<div><b>MECE·穷尽</b><br>是否还有明显方向未列？缺→补一支或写明已知缺口。</div>
<div><b>推荐位</b><br>哪支进选项地图、哪支进「不做什么」（相位 3 回填）。</div>
</div>
<p class="muted">生成于 render_tree.py · 军师 v0.8 ｜ 只渲染，不产生结论</p>
</body></html>"""
    out = d / a.out
    out.write_text(doc, encoding="utf-8")
    print(f"[render] {out} ｜ 分支 {len(branches)} 支")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
