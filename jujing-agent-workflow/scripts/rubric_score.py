#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚鲸工作流 · darwin 式评分与棘轮（落地齿）
按配置维度 rubric 打分（维度与量程见 assets/params.json#rubric，本脚本不内置）；
版本号只进不退：新版均分必须严格高于历史最高才允许晋升。

用法:
  python rubric_score.py score --version 1.1.0 --s "结构完整:9" "边界覆盖:9" ... \
  --by "Adversary（独立上下文）" --author "Builder（本版改动方）" --batch "2026-09-23 改动后实测批次" \
  --note "依据批次…"          # --by/--author/--batch 三者必填；by==author 视为自评，ratchet 直接 LOCKED
  # 配对校准锚分（#36 起首选 --anchor-s 逐维）:
  #   ... --anchor-ver anchor-2.3.1 --anchor-s "结构完整:7" "边界覆盖:7" ...
  #   逐维锚分入档（anchor_dims）且均分由脚本复算（anchor_avg）；锚文件须已在
  #   data/calibration/ 冻结，否则拒绝——锚分必须绑定可查验的锚，防口径凭空捏造
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
    _req(conf, ("dims", "score_min", "score_max", "ratchet_floor", "anchors"), "assets/params.json#rubric")
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
    scores = parse_dims(a.s, tuple(conf["dims"]), conf["score_min"], conf["score_max"])
    if not a.note.strip():
        sys.exit("[rubric] --note 必填：打分必须引用具体批次证据，否则视为无效分（防自欺）")
    rows = load()
    row = {"version": a.version, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "scores": scores, "avg": avg(scores), "note": a.note.strip(),
           # 2026-09-23 加固（独立复评指出：自评式棘轮无防御力——只比分数就能 ALLOWED）
           "by": a.by.strip(), "author": a.author.strip(), "batch": a.batch.strip()}
    # 2026-10-17 #35：配对校准——同一裁判实例对冻结锚的评分随行入档；无则该行走绝对分旧行为
    # 2026-10-17 #36：锚分优先逐维（--anchor-s）——手传 anchor_avg 是单点自报，存在「自欺 7→9」谎报
    # 路径；逐维分校验 dims 匹配与量程、均分由脚本复算；且锚分必须绑定 data/calibration/ 已冻结锚文件
    if a.anchor_s or a.anchor_avg is not None:
        if a.anchor_ver is None:
            sys.exit("[rubric] 给了锚分（--anchor-s/--anchor-avg）就必须给 --anchor-ver：锚分必须绑定锚标识")
        anchor_file = DATA.parent / "calibration" / (a.anchor_ver.strip() + ".md")
        if not anchor_file.exists():
            sys.exit(f"[rubric] 锚文件不存在: {anchor_file} —— anchor_ver 须对应 data/calibration/ 已冻结的锚")
    if a.anchor_s and a.anchor_avg is not None:
        sys.exit("[rubric] --anchor-s 与 --anchor-avg 互斥：给了逐维锚分，均分由脚本从逐维分复算，不再手传")
    if a.anchor_ver is not None:
        row["anchor_ver"] = a.anchor_ver.strip()
        if a.anchor_s:
            row["anchor_dims"] = parse_dims(a.anchor_s, tuple(conf["dims"]), conf["score_min"], conf["score_max"])
            row["anchor_avg"] = avg(row["anchor_dims"])
        elif a.anchor_avg is not None:
            row["anchor_avg"] = float(a.anchor_avg)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    with DATA.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _safe_print(f"[rubric] v{a.version} 均分 {row['avg']} 已记录（评分人 {row['by']} ｜ 实测批次 {row['batch']}）")
    prev = latest_version(rows, exclude=a.version)
    if prev:
        d = round(row["avg"] - prev["avg"], 2)
        _safe_print(f"[rubric] 对比 v{prev['version']}({prev['avg']}): {'+' if d >= 0 else ''}{d}")


def latest_version(rows, exclude=None):
    seen = {}
    for r in rows:
        if exclude and r["version"] == exclude:
            continue
        seen[r["version"]] = r  # 同版本多次打分取最后一次
    if not seen:
        return None
    return max(seen.values(), key=lambda r: r["ts"])


def cmd_history(_):
    rows = load()
    if not rows:
        print("[rubric] 暂无评分记录")
        return
    for r in rows:
        _safe_print(f"v{r['version']} [{r['ts'][:10]}] 均分 {r['avg']}  {r['note'][:60]}")


def cmd_ratchet(_):
    rows = load()
    if not rows:
        sys.exit("[rubric] 无记录，先 score")
    cur = rows[-1]
    prev = latest_version(rows[:-1], exclude=cur["version"])
    if prev is None:
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

    # 2026-10-17 #35：配对校准优先——跨裁判实例绝对分不可比（历史 8.94 与后续分出自不同全新
    # 上下文实例，尺度漂移致「严格超越」退化为「几乎必然 LOCKED」）。同一裁判实例评冻结锚（X）
    # 与当前批（Y），比 Y-X 不比绝对分；ratchet_floor 双低防护防「裁判整体偏松、锚批同涨」。
    conf = rcfg()
    floor = float(conf["ratchet_floor"])
    anchor_avg = cur.get("anchor_avg")
    if anchor_avg is not None:
        if cur["avg"] < floor:
            print(f"[rubric] LOCKED(配对): v{cur['version']}({cur['avg']}) < 地板 {floor} —— 双低防护："
                  f"均分未过合格线，配对差值再高不升 minor")
            return
        if cur["avg"] > float(anchor_avg):
            print(f"[rubric] ALLOWED(配对): v{cur['version']}({cur['avg']}) > 同裁判评锚 {anchor_avg}"
                  f"（锚 {cur.get('anchor_ver')}，差值 +{round(cur['avg'] - float(anchor_avg), 2)}）"
                  f"-> 可发布新版本号（评分人 {cur['by']} ｜ 实测批次 {cur['batch']}）；"
                  f"换锚：以本批改动后核心样本冻结 anchor-<{cur['version']}>")
        else:
            print(f"[rubric] LOCKED(配对): v{cur['version']}({cur['avg']}) <= 同裁判评锚 {anchor_avg}"
                  f"（锚 {cur.get('anchor_ver')}）-> 改动须继续实测提升，或回滚（版本号只进不退）")
        return

    # 基准＝**历史最高分**（不只看上一行）：防「先记一条异常低分，再拿它当垫脚石」。
    # 同版本多行时以**最后一行为准**（复评/订正会覆盖前一条，避免已判无效的自评分继续当基准）。
    by_ver = {}
    for r in rows:
        if r["version"] == cur["version"]:
            continue                      # 候选版本自己的历史行（含被推翻的自评分）不得充当基准
        by_ver[r["version"]] = r          # 其他版本：按出现顺序覆盖 ⇒ 同版本取最后一条
    hist = [r for r in by_ver.values() if float(r.get("avg") or 0) > 0]
    top = max(hist, key=lambda r: r["avg"]) if hist else prev
    if cur["avg"] > top["avg"]:
        print(f"[rubric] ALLOWED: v{cur['version']}({cur['avg']}) > 历史最高 v{top['version']}({top['avg']}) "
              f"-> 可发布新版本号（评分人 {cur['by']} ｜ 实测批次 {cur['batch']}）")
    else:
        print(f"[rubric] LOCKED: v{cur['version']}({cur['avg']}) <= 历史最高 v{top['version']}({top['avg']}) "
              f"-> 改动须继续实测提升，或回滚（版本号只进不退）")


def main():
    conf = rcfg()                            # 启动即校验配置，缺键宁可现在报
    dims = conf["dims"]
    p = argparse.ArgumentParser(description="聚鲸工作流 rubric评分+棘轮")
    sub = p.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("score")
    a1.add_argument("--version", required=True)
    a1.add_argument("--s", nargs="+", required=True,
                    help=f"维度:分数（维度与量程见 assets/params.json#rubric，当前 {len(dims)} 维）")
    a1.add_argument("--note", required=True)
    a1.add_argument("--by", required=True,
                    help="评分人/角色（必须独立于改动作者；否则 ratchet 判 LOCKED）")
    a1.add_argument("--author", required=True, help="本版改动作者/角色（用于自评检测）")
    a1.add_argument("--batch", required=True,
                    help="改动之后的实测批次标识（不得引用催生改动的那次事故）")
    a1.add_argument("--anchor-ver", default=None,
                    help="配对校准：冻结锚标识（如 anchor-2.3.1，文件须在 data/calibration/ 已冻结）")
    a1.add_argument("--anchor-s", default=None, nargs="+",
                    help="配对校准（首选）：逐维锚分 名称:分数（同一裁判实例评锚的分维明细；均分由脚本复算入档）")
    a1.add_argument("--anchor-avg", default=None,
                    help="配对校准（兼容）：手传锚评均分（单点自报，弱证据；与 --anchor-s 互斥）")
    sub.add_parser("history")
    sub.add_parser("ratchet")
    args = p.parse_args()
    {"score": cmd_score, "history": cmd_history, "ratchet": cmd_ratchet}[args.cmd](args)


if __name__ == "__main__":
    main()
