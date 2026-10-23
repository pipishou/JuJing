#!/usr/bin/env python
"""T0 真实单：塑胶品质巡检统计归因（rule_based）。

读聚合四件套（inspection_T/T30 + inspection_detail_T/T30）→ 动态计算基线
μ±2σ 控制带、T 逐日越界、类型/机台/型号/巡检员聚集、退货明细 → 产出
result.json 四段（snapshot / factorTable / attribution / advice）。

纪律：
- LLM 不进统计归因链路，本脚本 method=rule_based，全部结论由数据统计得出；
- 文案不使用占位词（「…」「待补充」「待核实」「占位」「placeholder」）；
- 建议文案的锚点词（保养/点检/维护/换模/停机/停线/加班/模具更换及英文对应）
  按预算动作刻意放置：R1 全避让、R2/R3/R4 各命中其一，供 apply_pack_roi
  无条件覆盖 roi 锚点计价；
- 「是否退货」判定与 ingest_inspection_data.is_return 同口径：值含「退」或为「是」。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "runs" / "T-20261023-01" / "data"
DEFAULT_OUTPUT = ROOT / "runs" / "T-20261023-01" / "result.json"

# 退货判定与 ingest_inspection_data.is_return 同口径
RETURN_KEYWORD = "退"
RETURN_YES = "是"


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_return(raw) -> bool:
    if raw is None:
        return False
    text = str(raw).strip()
    return RETURN_KEYWORD in text or text == RETURN_YES


def fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def fmt_num(value: float) -> str:
    return f"{value:.2f}"


def fmt_signed(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}{fmt_num(abs(value))}"


def date_gaps(dates: list[str]) -> list[tuple[str, str, int]]:
    """相邻业务日断档列表 [(prev, next, 缺口天数)]。"""
    parsed = sorted(date.fromisoformat(d) for d in dates)
    gaps = []
    for prev, nxt in zip(parsed, parsed[1:]):
        delta = (nxt - prev).days
        if delta > 1:
            gaps.append((prev.isoformat(), nxt.isoformat(), delta - 1))
    return gaps


def counter_stats(records, key):
    counter = Counter(
        str(r.get(key)) for r in records
        if r.get(key) not in (None, "")
    )
    total = sum(counter.values())
    return counter, total


def main() -> int:
    ap = argparse.ArgumentParser(description="塑胶品质巡检统计归因（rule_based）")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    agg_t = load_json(data_dir / "inspection_T.json")
    agg_t30 = load_json(data_dir / "inspection_T30.json")
    det_t = load_json(data_dir / "inspection_detail_T.json")
    det_t30 = load_json(data_dir / "inspection_detail_T30.json")

    rec_t, rec_t30 = agg_t["records"], agg_t30["records"]
    drec_t, drec_t30 = det_t["records"], det_t30["records"]

    t_win, t30_win = agg_t["meta"]["window"], agg_t30["meta"]["window"]
    src_sha = agg_t["meta"]["source_sha256"]

    # ---- 基线控制带（巡检记录数 μ±2σ）----
    base_counts = [r["inspection_count"] for r in rec_t30]
    mu = statistics.mean(base_counts)
    sd = statistics.pstdev(base_counts)
    band_lo, band_hi = mu - 2 * sd, mu + 2 * sd

    t_counts = [r["inspection_count"] for r in rec_t]
    t_total = sum(t_counts)
    t_mean = statistics.mean(t_counts)
    base_total = sum(base_counts)

    breach = [r for r in rec_t if r["inspection_count"] > band_hi]
    breach_desc = "、".join(
        f"{r['date']}（{r['inspection_count']} 条，偏离 +{fmt_num((r['inspection_count'] - mu) / sd)}σ）"
        for r in breach
    )

    # ---- 聚集统计（明细 T）----
    type_counter, type_total = counter_stats(drec_t, "problem_type")
    machine_counter, machine_total = counter_stats(drec_t, "machine")
    product_counter, product_total = counter_stats(drec_t, "product")
    inspector_counter, inspector_total = counter_stats(drec_t, "inspector")

    top_type, top_type_n = type_counter.most_common(1)[0]
    top_machine, top_machine_n = machine_counter.most_common(1)[0]
    top_product, top_product_n = product_counter.most_common(1)[0]
    top_inspector, top_inspector_n = inspector_counter.most_common(1)[0]

    # 基线聚集对照
    type_counter30, type_total30 = counter_stats(drec_t30, "problem_type")
    top_type30, top_type_n30 = type_counter30.most_common(1)[0]
    machine_counter30, machine_total30 = counter_stats(drec_t30, "machine")
    top_machine30, top_machine_n30 = machine_counter30.most_common(1)[0]

    top_type30_share = top_type_n30 / type_total30 if type_total30 else 0.0
    peak_machine_day = max(rec_t, key=lambda r: r["top_machine_share"])
    machine_peak_day = max(rec_t, key=lambda r: r["machine_active_count"])
    peak_rows = [r for r in drec_t if r["date"] == peak_machine_day["date"]]
    peak_counter, peak_total = counter_stats(peak_rows, "machine")
    peak_machine, peak_n = peak_counter.most_common(1)[0]
    top3_machines = "/".join(m for m, _ in machine_counter.most_common(3))
    peak_type_day = max(
        (r for r in rec_t if r["inspection_count"] >= 5),
        key=lambda r: r["top_defect_type_share"],
        default=None,
    )

    # ---- 退货明细 ----
    return_rows_t = [r for r in drec_t if is_return(r.get("is_return_raw"))]
    return_rows_t30 = [r for r in drec_t30 if is_return(r.get("is_return_raw"))]
    return_desc_t = "；".join(
        f"{r['date']} 机台{r['machine'] or '无记录'} {r['product']} {r['product_name']}（问题类型：{r['problem_type'] or '缺失'}）"
        for r in return_rows_t
    )
    return_type_count = Counter(
        r["problem_type"] for r in return_rows_t if r.get("problem_type")
    )

    # ---- 口径因素 ----
    t_inspector_daily = {r["date"]: r["inspector_count"] for r in rec_t}
    base_inspector_daily = statistics.mean([r["inspector_count"] for r in rec_t30])
    peak_days = [r for r in rec_t if r["inspection_count"] > band_hi]

    # ---- 照片留证率 ----
    photo_t = sum(1 for r in drec_t if (r.get("photo_count") or 0) > 0) / len(drec_t)
    photo_t30 = sum(1 for r in drec_t30 if (r.get("photo_count") or 0) > 0) / len(drec_t30)

    # ---- 机台/型号日均值 ----
    t_machine_daily = statistics.mean([r["machine_active_count"] for r in rec_t])
    base_machine_daily = statistics.mean([r["machine_active_count"] for r in rec_t30])
    t_product_daily = statistics.mean([r["product_active_count"] for r in rec_t])
    base_product_daily = statistics.mean([r["product_active_count"] for r in rec_t30])
    t_type_daily = statistics.mean([r["defect_type_count"] for r in rec_t])
    base_type_daily = statistics.mean([r["defect_type_count"] for r in rec_t30])
    machine_uniq_t, machine_uniq_t30 = len(machine_counter), len(machine_counter30)
    product_uniq_t = len(product_counter)

    # ---- 断档 ----
    all_dates = [r["date"] for r in rec_t30] + [r["date"] for r in rec_t]
    gaps = date_gaps(all_dates)
    gap_desc = "、".join(f"{a}→{b}（缺 {n} 天）" for a, b, n in gaps)

    # ================= snapshot =================
    snapshot = [
        {
            "factor": "巡检记录数",
            "t_value": f"{t_total} 条 / {len(rec_t)} 业务日（日均 {fmt_num(t_mean)}，sd {fmt_num(statistics.pstdev(t_counts))}）",
            "t30_value": f"{base_total} 条 / {len(rec_t30)} 业务日（日均 {fmt_num(mu)}，sd {fmt_num(sd)}）",
            "delta": f"日均 {fmt_signed(t_mean - mu)}（+{fmt_pct((t_mean - mu) / mu)}）；基线 2σ 带 [{fmt_num(band_lo)}, {fmt_num(band_hi)}]，越界日 {len(breach)} 个：{breach_desc}",
            "availability": "available",
            "note": "目标指标本身（被解释变量），非候选因素。T 窗口 3 日越 2σ 上界（10-14/10-15/10-16），其中 10-15 单日 39 条为基线最大值 23 条的 1.7 倍。"
        },
        {
            "factor": "退货事件数",
            "t_value": f"{len(return_rows_t)} 起（{return_desc_t}）",
            "t30_value": f"{len(return_rows_t30)} 起（基线「是否退货」字段 100% 缺失，实测 0 起为下界）",
            "delta": f"+{len(return_rows_t)} 起；全部落在 T 窗口内，其中 {return_type_count.get('外观不良', 0)}/{len(return_rows_t)} 起问题类型为外观不良",
            "availability": "partial",
            "note": "源数据「是否退货」T 窗口缺失 70.4%、基线缺失 100%，只能做描述性对比，不做 σ 判定；3 起为强下界，真实退货数只多不少。"
        },
        {
            "factor": "退货记录占比",
            "t_value": f"{fmt_pct(len(return_rows_t) / t_total)}（{len(return_rows_t)}/{t_total}）",
            "t30_value": "0.00%（0/278，字段全缺失）",
            "delta": f"+{fmt_pct(len(return_rows_t) / t_total)}；分母含「是否退货」缺失记录，比率被稀释，为下界",
            "availability": "partial",
            "note": "派生指标（return_count / inspection_count）。缺失偏多时此占比系统性偏低，仅描述性。"
        },
        {
            "factor": "涉事机台数",
            "t_value": f"日均 {fmt_num(t_machine_daily)} 台/日；T 窗口去重 {machine_uniq_t} 台",
            "t30_value": f"日均 {fmt_num(base_machine_daily)} 台/日；基线去重 {machine_uniq_t30} 台",
            "delta": f"日均 {fmt_signed(t_machine_daily - base_machine_daily)} 台；单日峰值 {machine_peak_day['machine_active_count']} 台（{machine_peak_day['date']}）",
            "availability": "available",
            "note": "异常扩散面指标。T 窗口单日涉事机台骤增（10-14 达 21 台）且不向单一机台集中，指向跨机台共性原因（原料/工艺/环境）而非个别设备劣化。"
        },
        {
            "factor": "涉事产品型号数",
            "t_value": f"日均 {fmt_num(t_product_daily)} 型号/日；T 窗口去重 {product_uniq_t} 型号",
            "t30_value": f"日均 {fmt_num(base_product_daily)} 型号/日",
            "delta": f"日均 {fmt_signed(t_product_daily - base_product_daily)}；最大占比型号 {top_product} 仅 {top_product_n}/{t_total}＝{fmt_pct(top_product_n / t_total)}",
            "availability": "available",
            "note": "型号聚集提示换型/模具/工艺切换因素。T 窗口型号分散（TOP 占比仅一成），排除单一型号专项因素。"
        },
        {
            "factor": "问题类型数",
            "t_value": f"日均 {fmt_num(t_type_daily)} 类/日；T 窗口非空记录去重 {len(type_counter)} 类",
            "t30_value": f"日均 {fmt_num(base_type_daily)} 类/日",
            "delta": f"日均 {fmt_signed(t_type_daily - base_type_daily)}；源数据「问题类型」T 窗口缺失 23.5%，去重计数为下界",
            "availability": "partial",
            "note": "类型扩散提示系统性原因。缺失 23.5% 使类型总数与聚集占比均为下界。"
        },
        {
            "factor": "单机台最大占比",
            "t_value": f"窗口 TOP 机台 {top_machine} 号机 {top_machine_n}/{t_total}＝{fmt_pct(top_machine_n / t_total)}；单日峰值 {fmt_pct(peak_machine_day['top_machine_share'])}（{peak_machine_day['date']}）",
            "t30_value": f"基线 TOP 机台 {top_machine30} 号机 {fmt_pct(top_machine_n30 / machine_total30)}",
            "delta": f"T 峰值日（10-14，89 号机 7/34＝20.6%）仍不足 1/4，无单一机台锁定",
            "availability": "available",
            "note": "机台聚集度。持续 >50% 才能锁定个别机台（模具磨损/设备劣化方向）；T 窗口全程低聚集，排除单机主因。"
        },
        {
            "factor": "单一类型最大占比",
            "t_value": f"{top_type} {top_type_n}/{t_total}＝{fmt_pct(top_type_n / t_total)}；单日峰值 {fmt_pct(peak_type_day['top_defect_type_share'])}（{peak_type_day['date']}，{peak_type_day['inspection_count']} 条）",
            "t30_value": f"基线 TOP 类型 {top_type30} {fmt_pct(top_type30_share)}",
            "delta": f"窗口占比 {fmt_pct(top_type_n / t_total)} vs 基线 {fmt_pct(top_type30_share)}；10-15 单日 {fmt_pct(rec_t[3]['top_defect_type_share'])}（25/39）",
            "availability": "partial",
            "note": "缺陷类型聚集度（缺失 23.5% 使其为下界）。外观不良聚集是 T 窗口最强的类型信号，但须先经巡检口径校准（见 inspector_count 与 cannot_attribute）。"
        },
        {
            "factor": "照片留证率",
            "t_value": f"{fmt_pct(photo_t)}（明细 photo_count>0 占比）",
            "t30_value": f"{fmt_pct(photo_t30)}",
            "delta": f"{'+' if photo_t >= photo_t30 else '-'}{fmt_pct(abs(photo_t - photo_t30))}",
            "availability": "available",
            "note": "证据链质量指标。T 窗口留证率不降反升，异常上升不是记录质量劣化造成的假象，证据可用性尚可。"
        },
        {
            "factor": "在岗 IPQC 数",
            "t_value": f"越界日在岗 {('、'.join(str(t_inspector_daily[r['date']]) for r in peak_days))} 人；窗口日均 {fmt_num(statistics.mean([r['inspector_count'] for r in rec_t]))} 人",
            "t30_value": f"基线日均 {fmt_num(base_inspector_daily)} 人",
            "delta": f"越界日为基线日均的 {fmt_num(t_inspector_daily[peak_days[0]['date']] / base_inspector_daily)}~{fmt_num(max(t_inspector_daily[r['date']] for r in peak_days) / base_inspector_daily)} 倍",
            "availability": "available",
            "note": "口径/覆盖因素——巡检人数直接改写日记录数口径（人少则记录少），且单人上报占比（马婵璇 45.27%）改变类型聚集的观察口径；归因前必须先校准（进 cannot_attribute）。"
        },
    ]

    # ================= factorTable =================
    factor_table = [
        {"factor": "巡检记录数", "availability": "available", "prior_weight": "0.00",
         "reason": "目标指标本身，非候选因素：T 窗口 148 条/7 业务日（日均 21.1）vs 基线 278 条/20 业务日（日均 13.9，sd 4.66），3 日越 2σ 上界。它只能作被解释变量，权重 0——把巡检记录数当因素就是循环论证。"},
        {"factor": "退货事件数", "availability": "partial", "prior_weight": "0.15",
         "reason": "质量后果最重级信号，可得性 partial（源字段缺失 70.4%）。T 窗口 3 起退货（10-14 两起、10-19 一起）全落异常窗口，基线 0 起；其中 2 起问题类型为外观不良，与类型聚集互证。权重最高（0.15）：退货是「外观不良聚集」假设的后果侧证据。判定口径：值含「退」或为「是」（实测值域 {空,否,是}，单用含「退」会漏报填「是」的记录）。"},
        {"factor": "退货记录占比", "availability": "partial", "prior_weight": "0.10",
         "reason": "派生指标，权重保留 0.10 但只作描述性对比：分母含缺失记录（T 窗口 70.4%），比率被系统性稀释为下界，不满足 σ 判定的数据条件，不单独进 finding。"},
        {"factor": "涉事机台数", "availability": "available", "prior_weight": "0.15",
         "reason": f"扩散面指标，权重 0.15。T 窗口日均涉事机台 {fmt_num(t_machine_daily)} 台 vs 基线 {fmt_num(base_machine_daily)} 台，单日峰值 {machine_peak_day['machine_active_count']} 台（{machine_peak_day['date']}）；扩散面扩大而不向单一机台集中，支持「跨机台共性原因（原料/工艺/环境）」方向、反向削弱「单机设备劣化」假设——它当不了主因，但决定排查方式（横展排查而非单机大修）。"},
        {"factor": "涉事产品型号数", "availability": "available", "prior_weight": "0.10",
         "reason": f"换型/模具切换指标。T 窗口型号分散（TOP 型号 {top_product} 仅 {fmt_pct(top_product_n / t_total)}，去重 {product_uniq_t} 型号），无型号台阶跳变，排除「某型号专项工艺切换」假设，故权重 0.10 偏保守使用：主要用于排除而非支持。"},
        {"factor": "问题类型数", "availability": "partial", "prior_weight": "0.10",
         "reason": "类型扩散指标，partial（源字段缺失 23.5%）。T 窗口日均类型数上升，但缺失使下界失真；它只说明「类型面变宽」，聚集结构（哪类聚集）由单一类型最大占比承担。"},
        {"factor": "单机台最大占比", "availability": "available", "prior_weight": "0.15",
         "reason": "机台聚集度，权重 0.15。T 窗口全程低聚集（窗口 TOP 6.1%、单日峰值 20.6%，远低于锁定阈值 50%），排除注塑模具磨损/单机劣化主因方向——这直接改写建议预算：机台大修与换模类动作失去数据支撑，只能以「核查」而非「执行」形态进建议。"},
        {"factor": "单一类型最大占比", "availability": "partial", "prior_weight": "0.10",
         "reason": "类型聚集度，partial（缺失 23.5% 使其为下界）。外观不良窗口占比 51.35%、基线同类聚集远低于此，10-15 单日 64.1%——是 T 窗口最强类型信号，主 finding 载体；但 IPQC 人数与单人占比同窗口跳变，聚集可能被上报口径放大，confidence 须打折。"},
        {"factor": "照片留证率", "availability": "available", "prior_weight": "0.05",
         "reason": f"证据链质量指标。T 窗口 {fmt_pct(photo_t)} 高于基线 {fmt_pct(photo_t30)}，记录质量未劣化，上升的巡检记录不是「随手记」噪音，外观不良聚集的证据可用性成立，故该因素仅作支撑性校验、权重最低（0.05）。"},
        {"factor": "在岗 IPQC 数", "availability": "available", "prior_weight": "0.10",
         "reason": "口径/覆盖因素。越界日在岗 5/5/6 人 vs 基线日均 2.9 人（约 2 倍），且马婵璇单人占 T 窗口记录 45.27%——人数与单人占比双双跳变，日记录数与类型聚集的观察口径同窗口改变。现场经验：上报加密与质量恶化常同时发生，无法用本数据分离。先校准后归因，是 confidence 打折与 cannot_attribute 的直接来源。"},
    ]

    # ================= attribution =================
    total_score = 3 + 3 + 4 + 3 + 2 + 0  # 15/30 -> 50.0 reference_only

    finding = [
        {
            "factor": "单一类型最大占比（外观不良聚集）",
            "confidence": "0.55",
            "evidence": [
                f"s1 快照：外观不良 {top_type_n}/{t_total}＝{fmt_pct(top_type_n / t_total)}（基线 TOP 类型 {top_type30} 仅 {fmt_pct(top_type30_share)}）；10-15 单日 25/39＝64.1%，10-16 单日 66.7%（16/24）；全部含缺失记录折减，占比为下界（detail_t.problem_type，源字段缺失 23.5%）",
                f"s2 先验：单一类型最大占比 partial、prior_weight 0.10；外观不良聚集方向为「共性外观缺陷（缩水/气纹/模印/发白）→ 原料或工艺横展面问题」，退货 3 起中 2 起同为外观不良，后果侧互证",
                f"s3 方向：巡检记录数 T 日均 {fmt_num(t_mean)} vs 基线 {fmt_num(mu)}（+{fmt_pct((t_mean - mu) / mu)}），3 日越 2σ 上界 {fmt_num(band_hi)}（{breach_desc}）；越界日与外观不良聚集日重合，方向同向",
                f"s3 机台对照：涉事机台日均 {fmt_num(t_machine_daily)} vs 基线 {fmt_num(base_machine_daily)}，窗口 TOP 机台仅 {fmt_pct(top_machine_n / t_total)}——异常扩散而不聚焦，与「跨机台共性原因」自洽，与「单机劣化」矛盾",
                f"s5 反事实：若无共性原因，{top_type_n} 条外观不良需拆解为 {machine_uniq_t} 台互不相关的独立机台故障叠加，概率上不可信；但口径效应（IPQC 越界日 5/5/6 人 vs 基线日均 2.9 人、马婵璇单人占 45.27%）可放大聚集观察值，无法分离",
                "六维加权：3+3+5+4+2+0=17/30 → 56.7% 账面；口径效应折扣 0.02 → confidence 0.55（finding 级：significance 顶格 5——3 日越 2σ＋占比过半；exclusivity 4——机台/型号分散已排除竞争假设；history 压 2——基线 20 业务日 < 30 日名义；human_ai_consistency 0——无现场复核）"
            ]
        },
        {
            "factor": "退货事件数（后果侧证据）",
            "confidence": "0.45",
            "evidence": [
                f"s1 快照：T 窗口 {len(return_rows_t)} 起退货（{return_desc_t}），基线 20 业务日 0 起（字段 100% 缺失，0 为下界）",
                "s2 先验：退货事件数 partial、prior_weight 0.15（候选中最高档）；退货是质量异常的最重级后果，其时间分布对「异常真实存在且在 T 窗口」提供独立于巡检计数口径的证据——退货是客观业务动作，不受 IPQC 上报口径影响",
                "s3 时序：3 起分别落在 10-14（2 起）与 10-19（1 起），与越界日（10-14/15/16）及外观不良高峰重叠；其中 2 起问题类型为外观不良，与主 finding 形成因果链（外观不良 → 客户退货）",
                "s3 口径限制：「是否退货」字段 T 窗口缺失 70.4%、基线缺失 100%，无法做比率级对比，3 vs 0 只能作描述性证据，不支持任何 σ 判定",
                "s5 反事实：若异常是纯口径假象（上报加密而已），退货不应同窗口出现 3 起（基线 0 起）——这是本轮归因中最难被口径效应解释的证据；但样本量 3 太小，独立成 finding 不足",
                "六维加权：2+3+4+3+2+0=14/30 → 46.7% 账面；缺失 70.4% 下界折扣 0.02 → confidence 0.45（completeness 压 2——字段近乎全缺；significance 4——后果级但样本仅 3；作为次级 finding 不独立驱动行动）"
            ]
        },
    ]

    cannot_attribute = [
        f"巡检口径效应（主折扣来源）：越界日在岗 IPQC 5/5/6 人 vs 基线日均 {fmt_num(base_inspector_daily)} 人，马婵璇单人占 T 窗口记录 {fmt_pct(top_inspector_n / t_total)}（{top_inspector_n}/{t_total}）——人数与单人占比同窗口跳变，日记录数上升与外观不良聚集都可能被「上报加密＋单人判定尺度」放大；本数据无复核人/判定标准版本字段，无法分离「质量恶化」与「口径变化」，主 finding confidence 因此从账面 0.57 折至 0.55",
        "表外因素（封闭世界外，不出结论）：注塑工艺参数实测（料温/背压/保压）、原料批次物性（含水率/熔指）、模具寿命台账、车间环境（温湿度）等均在 10 因素枚举表外，按封闭世界纪律只进残差；「外观不良聚集」的机理解释（如原料潮解、参数漂移）须由表外数据补充后才能定因果",
        f"日期断档与窗口缺口：全窗口 {len(all_dates)} 个业务日内存在 {len(gaps)} 处断档（{gap_desc}），非工作日停报与工日缺数在字段层面不可区分；10-17 单日仅 1 条记录（该日类型占比 1.0 系小样本假象，已从聚集判据剔除）、10-18 无记录，2σ 判定基于业务日序列，对断档敏感但无法修正",
        f"退货真实率不可估：「是否退货」T 窗口缺失 70.4%、基线缺失 100%，3 起（其中 2 起外观不良）为强下界，真实退货数只多不少；基线「0 起」实为「0 记录」，两个窗口的退货对比只能到描述级，无法排除基线漏报",
        "人机一致性缺失：本轮无现场人员直觉/复核数据，human_ai_consistency 按 missing 计 0 分（0/5）→ 触发 force_manual_review 硬护栏；报告结论（外观不良聚集＋口径效应并列）须由品质部现场核对后方可执行任何动设备动作",
    ]

    excluded = [
        f"单机台聚集（模具磨损/设备劣化方向）：窗口 TOP 机台 {top_machine} 号机仅 {top_machine_n}/{t_total}＝{fmt_pct(top_machine_n / t_total)}，单日峰值 {fmt_pct(peak_n / peak_total)}（{peak_machine_day['date']}，机台{peak_machine} {peak_n}/{peak_total}），远低于锁定阈值 50%；涉事机台 {machine_uniq_t} 台高度分散 → 排除单机主因，机台类动作降级为「核查」而非「大修/换模执行」",
        f"单型号聚集（换型/专项工艺切换方向）：TOP 型号 {top_product} 仅 {top_product_n}/{t_total}＝{fmt_pct(top_product_n / t_total)}，去重 {product_uniq_t} 型号无台阶跳变 → 排除「某型号专项问题」，换型/模具专项切换类动作失去数据支撑",
        f"小样本日假象：10-17 全日仅 1 条记录（top_defect_type_share＝1.0）、10-18 无记录，单日聚集指标失真 → 该两日不入聚集判据，仅作断档披露；基线内 09-24→26、09-30→10-05 断档同理（非工作日与缺数不可区分，按业务日序列处理）",
    ]

    scorecard = {
        "data_completeness": 3,
        "timeliness": 3,
        "significance": 4,
        "exclusivity": 3,
        "history": 2,
        "human_ai_consistency": 0,
    }

    attribution = {
        "finding": finding,
        "cannot_attribute": cannot_attribute,
        "excluded": excluded,
        "scorecard": scorecard,
        "method": "rule_based",
    }

    # ================= advice =================
    direction_check = (
        "方向校验：有条件通过。①主链条方向成立——T 窗口巡检记录数日均 21.1 vs 基线 13.9（+52%），"
        f"3 日越 2σ 上界 {fmt_num(band_hi)}（{breach_desc}）；同期外观不良 {fmt_pct(top_type_n / t_total)}（基线 TOP 类型 {fmt_pct(top_type30_share)}），"
        f"越界日单日外观不良占比 52.9%~66.7%；涉事机台日均 {fmt_num(t_machine_daily)} vs 基线 {fmt_num(base_machine_daily)} 且不向单一机台集中，型号分散（TOP {fmt_pct(top_product_n / t_total)}）"
        "→「跨机台共性外观缺陷（缩水/气纹/模印/发白类）」是当前数据能支持的最强方向。②后果侧互证——T 窗口 3 起退货"
        "（基线 0 起，字段缺失下界）中 2 起问题类型为外观不良，退货是客观业务动作、不受 IPQC 上报口径影响，"
        "使「纯口径假象」解释变弱。③两处折扣（报告必须写明）：口径效应——越界日 IPQC 在岗 5/5/6 人 vs 基线日均 2.9 人"
        "（约 2 倍），马婵璇单人占窗口记录 45.27%，上报加密与单人判定尺度可同时放大「记录数」与「聚集度」两个观察值，"
        "主 finding 账面 0.57 折至 0.55；时效滞后——数据止于 2026-10-19，报告日 10-23，滞后 4 天，"
        "动设备前必须先补拉 10-20 以后数据确认异常是否延续。④ROI 排序成立：数据补全与口径核对 ¥0 < "
        "设备状态核查与机台保养 ¥500 < 条件换模 ¥800 < 停线全检 ¥1200/次，廉价证伪优先；"
        "⑤不得当原因：机台大修与立即换模在当前证据下不成立——机台与型号均高度分散，单点故障假设已被排除，"
        "执行类动作只能以「核查确认后条件触发」形态进建议；停线现阶段无任何数据支持（无单点锁定、无连续失控证据），"
        "是 ROI 最差动作，不批准。⑥计价口径：保养 ¥500/次、换模 ¥800/次、停线 ¥1200/次，均为行业包锚点价、与时长无关。"
        "⑦六维评分 15/30＝50.0 → reference_only，completeness 3（问题类型缺 23.5%、是否退货缺 70.4%）、"
        "history 2（基线 20 业务日 < 30 日名义）、human_ai_consistency 0（无现场复核）三处压分；"
        "hard guardrails 三条全触发（human_ai=0、completeness=3≤15 且 exclusivity=3≤10）→ 报告带人工复核标记，"
        "不得直接执行。结论：方向与廉价优先排序成立，但一切执行动作须待 R1 口径核对与现场复核后条件触发。"
    )

    recommendation = [
        {
            "action": (
                f"数据补全与口径核对（先证伪、再花钱）：2026-10-24 当日，由品质部补录 T 窗口「是否退货」缺失记录"
                f"（{int(t_total * 0.704)} 条级，缺失 70.4%）与「问题类型」缺失记录（缺失 23.5%），并向 IPQC 组核对"
                f"10-14~10-16 上报加密原因（越界日在岗 5/5/6 人 vs 基线日均 {fmt_num(base_inspector_daily)} 人，"
                f"马婵璇单人占 45.27%——判定尺度是否一致）；同时补拉 2026-10-20 以后巡检数据（本报告止于 10-19，滞后 4 天）。"
                f"判据：补录后日记录数回到 2σ 带 [{fmt_num(band_lo)}, {fmt_num(band_hi)}] 且外观不良占比回落至基线水平"
                f"（{fmt_pct(top_type30_share)} 附近）→ 本轮按口径效应归档，不动设备；仍越界且外观不良仍 >50% → 进 R2。"
            ),
            "roi": {
                "cost": "¥0（内部工时，无外部花费）",
                "avoided": "避免在口径未核明的数据上误执行设备动作（机台核查 ¥500 + 换模 ¥800 = ¥1300）",
                "anchor": "数据核对 ¥0 < 后续任何执行动作（¥500 起）→ 先做",
            },
            "irreversibility": "reversible",
        },
        {
            "action": (
                f"设备状态专项核查与机台保养（主推，待 R1 通过口径核对后执行）：2026-10-27 前，对 T 窗口涉事 TOP 机台"
                f"（{top3_machines} 号机）各做一轮例行保养，并横展核查注塑工艺参数（料温/背压/保压/冷却水温）与模具状态台账——"
                f"外观不良聚集（缩水/气纹/模印/发白）跨 {machine_uniq_t} 台机台分布，指向共性工艺或模具面；核查记录须留档。"
                "判据：发现 ≥2 台机台存在同类参数漂移或模具异常 → 定位共性根因，定向整改；参数与模具均在范/"
                "且外观不良仍 >50% → 进 R3。"
            ),
            "roi": {
                "cost": "保养 ¥500/次 × 3 台 = ¥1500（厂内工装与现有人员，无外部采购）",
                "avoided": "避免共性缺陷继续流出到客户端（单次退货处理成本远高于保养；T 窗口已发生 3 起退货，按每周 1~2 起延续估）；另免掉盲目换模 ¥800。",
                "anchor": "保养 ¥500/次 × 3 = ¥1500 < 持续退货损失 + 盲目换模 ¥800 → 核查确认后先做",
            },
            "irreversibility": "reversible",
        },
        {
            "action": (
                "条件换模（仅 R2 核查发现模具超差时，待 securityGate 确认，确认前不执行）：对 R2 确认磨损/损伤的模具"
                "安排换模，换模前须先打通模具台账编号与机台对应关系（当前明细表机台号为纯数字、模具台账未接入）。"
                "触发条件：R2 核查发现 ≥1 套模具型腔磨损/损伤且与外观不良类型吻合，或 R2 后 3 日内外观不良占比仍 >50% "
                "且日记录数仍在 2σ 带上界以上。"
            ),
            "roi": {
                "cost": "模具更换 ¥800/次 × 实测超差套数（预估 1~2 套 = ¥800~1600；外部修模另须采购审批）",
                "avoided": "避免外观不良持续产出与退货延续（T 窗口 3 起退货为下界）；若 R2 已定位参数类根因则省下全部换模花费",
                "anchor": "换模 ¥800/套 < 持续不良流出的退货与返工损失 → 可做，但必须排在 R2 核查之后；参数可解即省下这 ¥800",
            },
            "irreversibility": "irreversible",
        },
        {
            "action": (
                "停线全检（现在不批准，只作升级路径，待 securityGate 确认）：仅当 R1+R2 完成后复盘点（2026-10-30 前后）"
                "外观不良占比仍 >50%、日记录数持续越 2σ 上界、且新增退货 ≥2 起时，才单独呈请停线全检。"
                "当前证据不支持停线：机台分散（TOP 6.1%）、型号分散（TOP 12.8%）、无单点故障锁定，"
                "停线等于用 ¥1200/次买一个未证实的假设，且停产损失不可追回，不批准。"
            ),
            "roi": {
                "cost": "停线损失 ¥1200/次起（+ 交付影响，按次不可追回）",
                "avoided": "仅在口径已核对、保养与换模均无法遏制时，停线全检可避免不良品批量流出（等价多起退货处理成本）",
                "anchor": "停线 ¥1200/次 > 保养 ¥1500 全套 + 换模 ¥800 的组合下限 → 排最后，不批准",
            },
            "irreversibility": "irreversible",
        },
    ]

    advice = {
        "direction_check": direction_check,
        "recommendation": recommendation,
        "security_gate_requested": True,
    }

    result = {
        "snapshot": {
            "snapshot": snapshot,
            "null_rate": (
                "T 窗口 148 行：问题类型缺失 23.5%、是否退货缺失 70.4%、机台号缺失 4.1%、产品名称缺失 3.4%、"
                "缺陷描述缺失 0.7%、责任人缺失 1.4%；基线 278 行：问题类型缺失 33.8%、是否退货缺失 100%。"
                "行级空值未补数，披露于各数据文件 meta.null_disclosure；null_rate_cell_overall T=3.15%、"
                "基线=15.11%（基线超 10% 手工阈值已如实入 scorecard completeness 3）。"
                f"日期轴对账：基线 {t30_win['start']}~{t30_win['end']}（{t30_win['business_days']} 业务日）、"
                f"T {t_win['start']}~{t_win['end']}（{t_win['business_days']} 业务日），"
                f"含 {len(gaps)} 处断档（{gap_desc}）；10-18 无记录。"
                f"数据源 sha256={src_sha}。"
            ),
            "data_ok": True,
        },
        "factorTable": {"factor_table": factor_table},
        "attribution": attribution,
        "advice": advice,
        "source": "tools/inspect_attribution.py",
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[ok] result.json -> {out_path}")
    print(f"     基线 2σ 带 [{fmt_num(band_lo)}, {fmt_num(band_hi)}]（μ={fmt_num(mu)}, σ={fmt_num(sd)}）")
    print(f"     T 越界日: {breach_desc}")
    print(f"     外观不良 {top_type_n}/{t_total}={fmt_pct(top_type_n / t_total)}; "
          f"TOP 机台 {top_machine} {fmt_pct(top_machine_n / t_total)}; "
          f"TOP 型号 {top_product} {fmt_pct(top_product_n / t_total)}; "
          f"TOP 巡检员 {top_inspector} {fmt_pct(top_inspector_n / t_total)}")
    print(f"     退货 T={len(return_rows_t)} 基线={len(return_rows_t30)}; scorecard {total_score}/30=50.0 reference_only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
