#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚鲸工作流 · darwin 式评分与棘轮（落地齿）
按配置维度 rubric 打分（维度与量程见 assets/params.json#rubric，本脚本不内置）；
版本号只进不退：新版均分必须严格高于历史最高才允许晋升。

用法:
  python rubric_score.py score --version 1.1.0 --s "结构完整:9" "边界覆盖:9" ... \
  --by "Adversary（独立上下文）" --author "Builder（本版改动方）" --batch "2026-09-23 改动后实测批次" \
  --note "依据批次…"          # --by/--author/--batch 三者必填；by==author 视为自评，ratchet 直接 LOCKED
  python rubric_score.py history
  python rubric_score.py ratchet

数据: <skill>/data/scores.jsonl
参数: <skill>/assets/params.json#rubric
"""
import argparse, json, os, sys, datetime, pathlib

# 编码坑根治（陷阱 #7 第 4 次复发）：Windows 控制台默认 GBK，符号（⇒/｜/→）直接 print 会崩。
# 导入即把 stdout/stderr 重配为 UTF-8 且 errors=replace —— 与内容无关，任何符号都不再可能崩。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_DIR = _ROOT / "data"
DATA = pathlib.Path(os.environ.get("JUJING_DATA_DIR", str(_DEFAULT_DIR))) / "scores.jsonl"
_PARAMS = _ROOT / "assets" / "params.json"


def _req(cfg, keys, where):
    """必填键校验：缺键即报 —— 不设兜底默认值（兜底＝隐藏的硬编码）。"""
    miss = [k for k in keys if k not in cfg]
    if miss:
        sys.exit(f"[rubric] {where} 缺少必填键 {miss}")


def rcfg():
    """维度与量程读 assets/params.json#rubric —— 代码不内置维度清单与量程数值。"""
    if not _PARAMS.exists():
        sys.exit(f"[rubric] 缺少配置 {_PARAMS}")
    try:
        conf = json.loads(_PARAMS.read_text(encoding="utf-8"))["rubric"]
    except (json.JSONDecodeError, KeyError) as e:
        sys.exit(f"[rubric] {_PARAMS.name} 读取失败: {e}")
    _req(conf, ("dims", "score_min", "score_max"), "assets/params.json#rubric")
    return conf


def _safe_print(msg):
    """双保险：导入处的 UTF-8 重配是根治，此处兜住「输出被重定向到非 UTF-8 流」的残余风险。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("gbk", errors="replace").decode("gbk"))


def load():
    if not DATA.exists():
        return []
    rows = []
    for line in DATA.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def avg(scores):
    vals = list(scores.values())
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def parse_dims(pairs, dims, lo, hi):
    out = {}
    for p in pairs:
        if ":" not in p:
            sys.exit(f"[rubric] 维度格式应为 '名称:分数'，收到: {p}")
        k, v = p.rsplit(":", 1)
        if k not in dims:
            sys.exit(f"[rubric] 未知维度 '{k}'，可用维度: {dims}")
        try:
            fv = float(v)
        except ValueError:
            sys.exit(f"[rubric] 分数须为数字: {p}")
        if not lo <= fv <= hi:
            sys.exit(f"[rubric] 分数须在 {lo}-{hi}: {p}")
        out[k] = fv
    missing = [d for d in dims if d not in out]
    if missing:
        sys.exit(f"[rubric] 缺维度 {missing} —— 缺维度按 0 计会虚抬均分，须打全（维度清单见 params.json#rubric.dims）")
    return out


def cmd_score(a):
    conf = rcfg()
    if a.avg is not None:
        # 跨口径评审（如四维 rubric＋blocking 罚分）只出均分：**必须仍是十分制**，不另立百分制说法。
        if not (float(conf["score_min"]) <= a.avg <= float(conf["score_max"])):
            sys.exit("[rubric] --avg 须在 %s-%s（十分制）；百分制分先折算成均分再记，台账不混口径"
                     % (conf["score_min"], conf["score_max"]))
        if not a.dims_used:
            sys.exit("[rubric] --avg 须配 --dims-used：写明该均分由哪几维合成（防「一个数字无出处」）")
        scores = {}
    else:
        if not a.s:
            sys.exit("[rubric] 缺 --s（逐维打分）或 --avg（跨口径均分）")
        scores = parse_dims(a.s, tuple(conf["dims"]), conf["score_min"], conf["score_max"])
    if not a.note.strip():
        sys.exit("[rubric] --note 必填：打分必须引用具体批次证据，否则视为无效分（防自欺）")
    rows = load()
    _avg = round(float(a.avg), 2) if a.avg is not None else avg(scores)
    row = {"version": a.version, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "scores": scores, "avg": _avg, "note": a.note.strip(),
           "scale": float(conf["score_max"]),
           # 2026-09-23 加固（独立复评指出：自评式棘轮无防御力——只比分数就能 ALLOWED）
           "by": a.by.strip(), "author": a.author.strip(), "batch": a.batch.strip(),
           "rubric": conf.get("version") or "1"}
    if a.avg is not None:
        row["dims_used"] = list(a.dims_used)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    with DATA.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _safe_print(f"[rubric] v{a.version} 均分 {row['avg']} 已记录（评分人 {row['by']} ｜ 实测批次 {row['batch']}）")
    prev = peak_version(rows, exclude=a.version)
    if prev:
        d = round(row["avg"] - prev["avg"], 2)
        _safe_print(f"[rubric] 对比 v{prev['version']}({prev['avg']}): {'+' if d >= 0 else ''}{d}")


def peak_rows(rows, exclude=None):
    """每版本取**最大 avg** 的那一行（十分制；非正与越界百分制行跳过）。

    2026-11-10 修（四次独立复核 blocking B2）：旧 `latest_version()` 取「同版本最后一次」，
    使 score 的对比行打印 8.94（真基准 9.15）、棘轮兜底把百分制行当基准（打印「历史最高 41.0」）。
    本函数是**唯一**取分口径，历史侧与候选侧共用，不得再出现第二份取分逻辑。
    """
    hi = float(rcfg()["score_max"])
    best = {}
    for r in rows:
        if exclude and r["version"] == exclude:
            continue
        v = float(r.get("avg") or 0)
        if v <= 0 or v > hi:
            continue
        cur = best.get(r["version"])
        if cur is None or v > float(cur.get("avg") or 0):
            best[r["version"]] = r
    return best


def peak_version(rows, exclude=None):
    """全部版本中 avg 最大的一行（同为十分制口径）；无有效行返回 None。"""
    best = peak_rows(rows, exclude=exclude)
    if not best:
        return None
    return max(best.values(), key=lambda r: float(r["avg"]))


def cmd_history(_):
    rows = load()
    if not rows:
        print("[rubric] 暂无评分记录")
        return
    for r in rows:
        _safe_print(f"v{r['version']} [{r['ts'][:10]}] 均分 {r['avg']}  {r['note'][:60]}")


def cmd_backfill(_):
    rows = load()
    n = 0
    for r in rows:
        if not (r.get("rubric") or "").strip():
            r["rubric"] = "pre-r1"
            n += 1
    if n:
        DATA.write_text("".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows),
                        encoding="utf-8")
    print(f"[rubric] backfill：{n} 条补 rubric=pre-r1（旧行如实标注，不追溯）")


def cmd_ratchet(_):
    rows = load()
    if not rows:
        sys.exit("[rubric] 无记录，先 score")
    # 2026-11-10 修（四次独立复核 blocking B1）：旧实现取 rows[-1] 当候选分，
    #   同一账本换行序即翻转判决（低→高 ALLOWED / 高→低 LOCKED）——判决成了行序的函数。
    #   候选分改为「候选版本全部行取最大 avg」，与历史侧共用 peak_rows 口径。
    # S6（五次复核）：末行版本会被「补记旧版本评分」打乱 → 候选＝ts 最新的那一行所属版本。
    _last_ver = max(rows, key=lambda r: str(r.get("ts") or ""))["version"]
    _hi0 = float(rcfg()["score_max"])
    _cand_rows = [r for r in rows
                  if r["version"] == _last_ver and 0 < float(r.get("avg") or 0) <= _hi0]
    if not _cand_rows:
        sys.exit("[rubric] 候选版本 %s 无十分制有效行（avg 越界或非正），先补打分" % _last_ver)
    cur = max(_cand_rows, key=lambda r: float(r["avg"]))
    prev = peak_version(rows, exclude=cur["version"])
    if prev is None:
        # 2026-11-10 修（四次复核 blocking B2 的第二处消费点）：历史行可能「全被十分制过滤掉」，
        #   此时若仍打印「首版」会掩盖事实（其实有历史，只是口径不可比），更不得回退去拿百分制行当基准。
        _others = [r for r in rows if r["version"] != cur["version"]]
        if _others:
            print("[rubric] 无可比历史基准（历史行均为百分制或非正，不计入棘轮）："
                  "v%s(%s) 视为首条十分制基线" % (cur["version"], cur["avg"]))
        else:
            print(f"[rubric] 首版 v{cur['version']} 均分 {cur['avg']}，棘轮基线已建立")
        return

    # 2026-09-23 加固（独立复评结论）：棘轮不能只比分数——否则「同一方改条款＋同日自评」即可 ALLOWED。
    # 四条硬约束：① 必须有评分人 ② 必须有改动作者 ③ 二者不得同源（自评） ④ 必须有「改动后」的实测批次。
    blockers = []
    if not (cur.get("by") or "").strip():
        blockers.append("缺 --by（评分人）")
    if not (cur.get("author") or "").strip():
        blockers.append("缺 --author（本版改动作者）")
    if not (cur.get("batch") or "").strip():
        blockers.append("缺 --batch（改动后的实测批次）")
    if (cur.get("by") or "") and cur.get("by") == cur.get("author"):
        blockers.append("评分人＝改动作者（%s）＝自评" % cur["by"])
    if blockers:
        print(f"[rubric] LOCKED: v{cur['version']} 不予晋升 —— " + "；".join(blockers))
        print("          依据：同源自审有结构性上限（SKILL.md 公理 2）；评分须由独立上下文/独立角色出具，"
              "且 --batch 必须引用**改动之后**的实测批次。")
        return

    # 基准＝**历史最高分**（不只看上一行）：防「先记一条异常低分，再拿它当垫脚石」。
    # 2026-11-10 修（用户判「判断基准 bug」）：同版本多行时取**最大值**，不再取最后一条。
    #   旧逻辑让 v1.4.0 两裁判（9.15 Karpathy / 8.94 Jobs）被晚一条的 8.94 覆盖 9.15，
    #   基准被压低 0.21 ＝ 把「严格超越历史最高」放水。
    # 同时统一**十分制**：avg 超出 score_max 的旧百分制行不进基准（只跳过，不改写历史）。
    hi = float(rcfg()["score_max"])
    # 2026-11-10 修（五次独立复核 blocking B1）：这里原本自己又算一遍「同版本取最大」，
    #   等于第三份取分实现——SKILL 写的「唯一取分口径」就成了空话（改坏 peak_rows 也能全绿）。
    #   现在历史侧一律走 peak_rows()，本函数不再持有任何取分逻辑；仍只跳过、不改写百分制行。
    skipped_scale = ["v%s(%s)" % (r["version"], float(r.get("avg") or 0))
                     for r in rows
                     if r["version"] != cur["version"] and float(r.get("avg") or 0) > hi]
    hist = list(peak_rows(rows, exclude=cur["version"]).values())
    if skipped_scale:
        tail = " ..." if len(skipped_scale) > 5 else ""
        print("[rubric] 跳过非十分制（>%s）历史行 %d 条：%s%s —— 统一十分制，百分制旧分不参与棘轮"
              % (hi, len(skipped_scale), ", ".join(skipped_scale[:5]), tail))
    top = max(hist, key=lambda r: float(r["avg"])) if hist else prev
    if cur["avg"] > top["avg"]:
        print(f"[rubric] ALLOWED: v{cur['version']}({cur['avg']}) > 历史最高 v{top['version']}({top['avg']}) rubric={cur.get('rubric','?')} "
              f"-> 可发布新版本号（评分人 {cur['by']} ｜ 实测批次 {cur['batch']}）")
    else:
        print(f"[rubric] rubric={cur.get('rubric', '?')} LOCKED: v{cur['version']}({cur['avg']}) <= 历史最高 v{top['version']}({top['avg']}) "
              f"-> 改动须继续实测提升，或回滚（版本号只进不退）")


def main():
    conf = rcfg()                            # 启动即校验配置，缺键宁可现在报
    dims = conf["dims"]
    p = argparse.ArgumentParser(description="聚鲸工作流 rubric评分+棘轮")
    sub = p.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("score")
    a1.add_argument("--version", required=True)
    a1.add_argument("--s", nargs="+", default=None,
                    help=f"维度:分数（维度与量程见 assets/params.json#rubric，当前 {len(dims)} 维）")
    a1.add_argument("--avg", type=float, default=None,
                    help="跨口径评审均分（十分制），与 --s 二选一；须配 --dims-used")
    a1.add_argument("--dims-used", nargs="+", default=None,
                    help="--avg 模式下说明均分由哪几维合成（防数字无出处）")
    a1.add_argument("--note", required=True)
    a1.add_argument("--by", required=True,
                    help="评分人/角色（必须独立于改动作者；否则 ratchet 判 LOCKED）")
    a1.add_argument("--author", required=True, help="本版改动作者/角色（用于自评检测）")
    a1.add_argument("--batch", required=True,
                    help="改动之后的实测批次标识（不得引用催生改动的那次事故）")
    sub.add_parser("history")
    sub.add_parser("ratchet")
    sub.add_parser("backfill-rubric")
    args = p.parse_args()
    {"score": cmd_score, "history": cmd_history, "ratchet": cmd_ratchet,
     "backfill-rubric": cmd_backfill}[args.cmd](args)


if __name__ == "__main__":
    main()
