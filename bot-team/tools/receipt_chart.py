# -*- coding: utf-8 -*-
"""交付回执配图：把任务的关键指标序列与归因置信度画成一张 PNG。

设计约束：
- 只画报告里真实存在的数，不臆造指标；取不到就返回 None，卡片退回纯文字。
- 指标选择是通用的：在所有 data_refs 的 records 里选「变异系数最大」的数值字段，
  这样换一个场景（良率/OEE/库存）不用改代码。
"""
import json
import statistics
from pathlib import Path

# 只译少数高频字段名，其余原样显示（不猜业务含义）
METRIC_LABELS = {
    "refill_rate": "补料率",
    "yield": "良率",
    "defect_rate": "不良率",
    "output_volume": "产量",
    "oee": "OEE",
}


def _numeric_fields(records):
    """返回覆盖率 >=80% 的数值字段名 -> 按变异系数降序。"""
    n = len(records)
    if not n:
        return []
    out = []
    for key in records[0]:
        vals = []
        for r in records:
            v = r.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
        if len(vals) < 0.8 * n or len(vals) < 3:
            continue
        mean = statistics.fmean(vals)
        if abs(mean) < 1e-9:
            continue
        cv = statistics.pstdev(vals) / abs(mean)
        out.append((cv, key))
    out.sort(reverse=True)
    return [k for _cv, k in out]


def _load_series(tdir):
    """合并所有 data_refs 里的 records，按 date 去重排序。"""
    report = json.loads((tdir / "report.json").read_text(encoding="utf-8-sig"))
    rows, metric = {}, None
    for ref in report.get("data_refs") or []:
        p = Path(ref.get("path") or "")
        if not p.is_absolute():
            # data_refs 里的路径相对 bot-team 根（runs/<task>/data/...）
            p = tdir.parents[1] / p if p.parts[:1] == ("runs",) else tdir / p
        if not p.exists():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        recs = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(recs, list) or not recs:
            continue
        if metric is None:
            fields = _numeric_fields(recs)
            metric = fields[0] if fields else None
        if not metric:
            continue
        for r in recs:
            d, v = r.get("date"), r.get(metric)
            if d and isinstance(v, (int, float)) and not isinstance(v, bool):
                rows.setdefault(str(d), float(v))
    return metric, sorted(rows.items())


def render(tdir, out_png=None):
    """画图。返回 PNG 路径；数据不够则返回 None。"""
    tdir = Path(tdir)
    metric, series = _load_series(tdir)
    if not metric or len(series) < 5:
        return None
    report = json.loads((tdir / "report.json").read_text(encoding="utf-8-sig"))
    findings = report.get("finding") or []
    if not isinstance(findings, list):
        findings = []

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(9.0, 3.6), dpi=150,
        gridspec_kw={"width_ratios": [2.0, 1.0]})
    fig.patch.set_facecolor("white")

    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    xs = list(range(len(vals)))
    ax1.plot(xs, vals, color="#2454FF", lw=1.8, marker="o", ms=3.2,
             markerfacecolor="white", markeredgewidth=1.2)
    mean = statistics.fmean(vals)
    ax1.axhline(mean, color="#9AA4B2", ls="--", lw=1.0)
    ax1.annotate(f"{len(vals)} 天均值 {mean:.2f}", xy=(0.02, mean),
                 xycoords=("axes fraction", "data"),
                 fontsize=7.5, color="#6B7280", va="bottom")
    if vals:
        hi = max(range(len(vals)), key=lambda i: vals[i])
        ax1.scatter([xs[hi]], [vals[hi]], color="#D93F3F", zorder=5, s=26)
        ax1.annotate(f"{vals[hi]:.2f}", xy=(xs[hi], vals[hi]), xytext=(0, 8),
                     textcoords="offset points", fontsize=7.5, color="#D93F3F",
                     ha="center")
    step = max(1, len(dates) // 6)
    ax1.set_xticks(xs[::step])
    ax1.set_xticklabels([dates[i][5:] for i in xs[::step]], fontsize=7.5,
                        color="#6B7280")
    ax1.set_title(f"{METRIC_LABELS.get(metric, metric)} 日序列（{len(vals)} 天）",
                  fontsize=9.5, color="#111827", loc="left")
    ax1.grid(axis="y", color="#E5E7EB", lw=0.8)
    for side in ("top", "right"):
        ax1.spines[side].set_visible(False)
    ax1.tick_params(colors="#6B7280", labelsize=7.5)

    labels, conf = [], []
    for f in findings[:5]:
        try:
            conf_v = float(f.get("confidence"))
        except (TypeError, ValueError):
            continue
        labels.append(str(f.get("factor") or "-")[:10])
        conf.append(conf_v)
    if conf:
        ys = list(range(len(conf)))[::-1]
        ax2.barh(ys, conf, color="#2454FF", height=0.55)
        for y, c in zip(ys, conf):
            ax2.text(c + 0.02, y, f"{c:.2f}", va="center", fontsize=7.5,
                     color="#374151")
        ax2.set_yticks(ys)
        ax2.set_yticklabels(labels, fontsize=7.5, color="#374151")
        ax2.set_xlim(0, 1.0)
        ax2.set_title("归因置信度", fontsize=9.5, color="#111827", loc="left")
    else:
        ax2.axis("off")
        ax2.text(0.5, 0.5, "无结构化归因", ha="center", va="center",
                 fontsize=8, color="#9AA4B2")
    ax2.grid(axis="x", color="#E5E7EB", lw=0.8)
    for side in ("top", "right"):
        ax2.spines[side].set_visible(False)
    ax2.tick_params(colors="#6B7280", labelsize=7.5)

    n_cannot = len(report.get("cannot_attribute") or [])
    if n_cannot:
        fig.text(0.985, 0.02, f"待核项 {n_cannot}", ha="right", va="bottom",
                 fontsize=7, color="#9AA4B2")
    fig.tight_layout()

    out = Path(out_png) if out_png else tdir / "chart.png"
    fig.savefig(out, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return out
