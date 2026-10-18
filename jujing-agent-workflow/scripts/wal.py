#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚鲸工作流 · 纠偏 WAL（信号齿+积累齿）
append-only 纠偏/教训/新模式记录；同类达晋升阈值（见 assets/params.json#wal.promote_min）自动提示晋升条款（走版本号棘轮）。

用法:
  python wal.py add --source user --type correction --text "..." --clause "SKILL.md#G0" [--ref <外链指针>] [--actor <id>] [--chain 父id,...]
  python wal.py list [--clause X] [--open]
  python wal.py promote-check [--min <阈值，缺省取配置>] [--digest] [--ids 30,31]
  python wal.py mark-promoted --ids 12,13,14 --to v1.2.0 --cohesion "<同类性三问逐条回答>"

--digest（v2.2.0·D7#2）：对候选组（或 --ids 指定组）自动拼晋升确认材料——原文全文＋时序＋actor＋修正链（文本内 `#N` 引用自动标注）＋**同类性三问（D-b 强制项）**。
--chain（锁定批·甲 D-a）：修正链的**机器可读**字段（父 id 逗号分隔，须指向更早的真实 id）——
  文本里写「订正 #42」只是引用事实、机器校验不到，故复发换锚点时聚类键 (type,clause) 看不见（阈值永远数不到）。
  悬空/倒指由 probe_usage.py kb-lint ⑨ 报红，跨锚点链在 digest/kb-lint 计数公示。
--cohesion（锁定批·甲 D-b）：阈值只筛「候选」，**不判「同类」**（分组只共享上位公理）；
  故 mark-promoted 必带三问逐条回答（长度下限＝assets/params.json#wal.cohesion_min_len），随晋升行永久留痕。

数据: <skill>/data/wal.jsonl （append-only，永不改写历史行）
参数: <skill>/assets/params.json#wal （阈值与枚举唯一事实源，本脚本不内置）
"""
import argparse, json, os, sys, datetime, collections, pathlib, re

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_DIR = _ROOT / "data"
DATA = pathlib.Path(os.environ.get("JUJING_DATA_DIR", str(_DEFAULT_DIR))) / "wal.jsonl"
_PARAMS = _ROOT / "assets" / "params.json"


def _req(cfg, keys, where):
    """必填键校验：缺键即报 —— 不设兜底默认值（兜底＝隐藏的硬编码，且会把缺配置静默降级成假绿）。"""
    miss = [k for k in keys if k not in cfg]
    if miss:
        sys.exit(f"[wal] {where} 缺少必填键 {miss}")


def wcfg():
    """WAL 参数读 assets/params.json#wal —— 晋升阈值与枚举都不内置（去硬编码）。"""
    if not _PARAMS.exists():
        sys.exit(f"[wal] 缺少配置 {_PARAMS}")
    try:
        conf = json.loads(_PARAMS.read_text(encoding="utf-8"))["wal"]
    except (json.JSONDecodeError, KeyError) as e:
        sys.exit(f"[wal] {_PARAMS.name} 读取失败: {e}")
    _req(conf, ("promote_min", "per_round_min", "text_soft_limit", "types", "sources",
                "cohesion_questions", "cohesion_min_len"), "assets/params.json#wal")
    return conf


def clause_pool():
    """锚点落点池读 assets/params.json#clauses —— clause 必须落在已注册锚点上；
    悬空锚点＝探针永远抽不到＝死台账（独立复评指出 data 里实存悬空 clause）。"""
    try:
        pool = json.loads(_PARAMS.read_text(encoding="utf-8"))["clauses"]
    except (json.JSONDecodeError, KeyError) as e:
        sys.exit(f"[wal] {_PARAMS.name} 读取 clauses 失败: {e}")
    if not pool:
        sys.exit("[wal] assets/params.json#clauses 为空")
    return pool


def actor_check(actor):
    """M2 actor 准入校验（判据 assets/params.json#memory.actors·actor_policy）：
    required=true 时缺 --actor 即拒；未登记 actor 即拒——与 probe_usage.py 同判据同口径。"""
    try:
        mem = json.loads(_PARAMS.read_text(encoding="utf-8"))["memory"]
        policy = mem["actor_policy"]
        actors = [k for k in mem["actors"] if not k.startswith("_")]
        required = policy["required"]
    except (json.JSONDecodeError, KeyError) as e:
        sys.exit(f"[wal] {_PARAMS.name} 读取 memory.actor_policy/actors 失败: {e}")
    actor = (actor or "").strip()
    if not actor:
        if required:
            sys.exit("[wal] 拒绝：留痕必带 --actor（判据 params.json#memory.actor_policy.required；"
                     "登记表 params.json#memory.actors，未登记者先走 routing.md §七 第三步准入）")
        return None
    if actor not in actors:
        sys.exit(f"[wal] 拒绝：actor '{actor}' 未登记（准入登记表 params.json#memory.actors，"
                 f"现登记 {sorted(actors)}）——未登记留痕不得入档，先准入或改用已登记 actor")
    return actor


def load():
    if not DATA.exists():
        return []
    rows = []
    for line in DATA.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[wal] 跳过损坏行: {line[:80]}", file=sys.stderr)
    return rows


def append(row):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    with DATA.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def next_id(rows):
    return max([r.get("id", 0) for r in rows] or [0]) + 1


def promoted_ids(rows):
    done = set()
    for r in rows:
        if r.get("action") == "promoted":
            done.update(r.get("ids", []))
    return done


def cmd_add(a):
    cfg = wcfg()
    actor = actor_check(a.actor)                 # M2：准入校验（缺省/未登记均拒）
    types, sources = tuple(cfg["types"]), tuple(cfg["sources"])
    if a.type not in types:
        sys.exit(f"[wal] --type 必须是 {types}")
    if a.source not in sources:
        sys.exit(f"[wal] --source 必须是 {sources}")
    if not a.text.strip() or not a.clause.strip():
        sys.exit("[wal] --text 与 --clause 均必填（clause 是探针锚点，取值见 assets/params.json#clauses）")
    pool = clause_pool()
    if a.clause.strip() not in pool:
        sys.exit(f"[wal] clause 「{a.clause.strip()}」不在抽检池（现注册 {len(pool)} 条）——"
                 "悬空锚点＝探针抽不到＝死台账；先在 params.json#clauses 注册，或改用既有锚点")
    rows = load()
    known = {r["id"] for r in rows if r.get("action") is None}
    chain = []
    for tok in (getattr(a, "chain", None) or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            sys.exit(f"[wal] 拒绝：--chain 只收数字 id（收到 {tok!r}）——"
                     "修正链是机器可读字段，自由文本一律不入档（写文本＝机器校验不到的假留痕）")
        cid = int(tok)
        if cid not in known:
            sys.exit(f"[wal] 拒绝：--chain 指向未知 id {cid}（现存最大 #{max(known) if known else 0}）——"
                     "悬空修正链＝复算断裂（kb-lint ⑨ 报红）；父行须先入档")
        chain.append(cid)
    chain = sorted(set(chain))
    text = a.text.strip()
    row = {"id": next_id(rows), "ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "source": a.source, "type": a.type, "text": text,
           "clause": a.clause.strip(), "promoted": False, "actor": actor}
    if chain:
        row["chain"] = chain
    if a.ref:
        row["ref"] = a.ref.strip()
    # 软上限（判据 params.json#wal.text_soft_limit）：超限不拒——append-only 留痕优先；
    # 但打印警告要求 --ref 外链详版，防 list 不可扫读（v2.2.0 D7#1）。
    if len(text) > cfg["text_soft_limit"]:
        warn = (f"[wal] 警告: #{row['id']} 文本 {len(text)} 字符超软上限 {cfg['text_soft_limit']}"
                f"——行文本易读难，请补 --ref 外链详版（本轮已放行，append-only 不改写）")
        print(warn, file=sys.stderr)
    append(row)
    print(f"[wal] 已记录 #{row['id']} [{a.source}/{a.type} actor={actor}] -> {row['clause']}"
          + (f" ref={row['ref']}" if "ref" in row else "")
          + (f" chain={row['chain']}" if chain else ""))
    groups = pending_groups(rows + [row])
    floor = cfg["promote_min"]
    hits = {k: v for k, v in groups.items() if len(v) >= floor}
    if hits:
        print(f"[wal] 提示: {len(hits)} 组已达晋升阈值({floor})，运行 promote-check 查看")


def pending_groups(rows):
    done = promoted_ids(rows)
    g = collections.defaultdict(list)
    for r in rows:
        if r.get("action") is None and r["id"] not in done:
            g[(r["type"], r["clause"])].append(r["id"])
    return g


def cmd_list(a):
    rows = load()
    done = promoted_ids(rows)
    shown = 0
    for r in rows:
        if r.get("action") is not None:
            continue
        if a.clause and r["clause"] != a.clause:
            continue
        if a.open and r["id"] in done:
            continue
        flag = "已晋升" if r["id"] in done else "待定"
        print(f"#{r['id']} [{r['ts'][:10]} {r['source']}/{r['type']} {flag} actor={r.get('actor', '—')}] {r['clause']}\n    {r['text']}")
        shown += 1
    print(f"[wal] 共 {shown} 条记录")


def _digest_rows(rows, ids, cfg):
    """晋升确认材料自动拼（v2.2.0 D7#2 ＋ 锁定批·甲 D-a/D-b）：原文全文＋时序＋actor＋修正链＋外链
    ＋**同类性三问（强制项）**。
    修正链有两个来源，分开呈现、勿混读：
      ① chain 字段（add --chain，**机器可读可复算**，kb-lint ⑨ 校验悬空/倒指）；
      ② 文本内 `#N` 提及（自动标注——只是「引用事实」，机器校验不到，故不当作留痕）。
    D-b：阈值只筛「候选」不判「同类」；三问必须随材料给出（digest 缺此段＝材料不完整），
    回答由 mark-promoted --cohesion 永久留痕——判断留给人（公理 1），但判断须可审。"""
    by_id = {r["id"]: r for r in rows if r.get("action") is None}
    refs_in_text, structured, cross_anchor = {}, {}, []
    for i in ids:
        r = by_id.get(i)
        if not r:
            continue
        cited = [int(x) for x in re.findall(r"#(\d+)", r["text"]) if int(x) in by_id and int(x) != i]
        if cited:
            refs_in_text[i] = cited
        if r.get("chain"):
            structured[i] = sorted(r["chain"])
            for c in r["chain"]:
                t = by_id.get(c)
                if t and t.get("clause") != r.get("clause"):
                    cross_anchor.append(f"#{i}({r.get('clause')})->#{c}({t.get('clause')})")
    for i in ids:
        r = by_id.get(i)
        if not r:
            print(f"  #{i} <未知 id，跳过>")
            continue
        head = f"#{i} [{r['ts']} {r['source']}/{r['type']} actor={r.get('actor', '—')}] {r['clause']}"
        print(head)
        print(f"    原文: {r['text']}")
        if r.get("ref"):
            print(f"    外链: {r['ref']}")
        if i in structured:
            print(f"    修正链(chain 字段·可复算): {'、'.join('#%d' % c for c in structured[i])}")
        if i in refs_in_text:
            print(f"    修正链(文本提及·仅供语境): {'、'.join('#%d' % c for c in refs_in_text[i])}")
    cited_any = sorted({c for v in refs_in_text.values() for c in v})
    if cited_any:
        print(f"[wal] 修正链汇总（文本提及）: {'、'.join('#%d' % c for c in cited_any)} 被本组引用（随组语境，评估时须一并读）")
    clauses_in_group = sorted({by_id[i].get("clause") for i in ids if i in by_id})
    if len(clauses_in_group) > 1:
        print(f"[wal] 提示: 本组跨 {len(clauses_in_group)} 个锚点（{'、'.join(clauses_in_group)}）"
              "——跨锚点组只可能来自 --ids 显式指定（(type,clause) 聚类键看不见），同类性更可能不成立")
    if cross_anchor:
        print(f"[wal] 跨锚点修正链 {len(cross_anchor)} 处: {'、'.join(cross_anchor)}"
              " ——同根复发换了锚点、聚类键看不见（D-a 事故形态）；判同类性须按根因，不按锚点")
    print("[wal] 同类性三问（判据 assets/params.json#wal.cohesion_questions；阈值只筛候选、不判同类）:")
    for n, q in enumerate(cfg["cohesion_questions"], 1):
        print(f"    {n}. {q}")
    print(f"[wal] 确认后: mark-promoted --ids <...> --to <新版本号> --cohesion \"<三问逐条回答>\"")


def cmd_promote_check(a):
    cfg = wcfg()
    floor = a.min if a.min is not None else cfg["promote_min"]
    rows = load()
    groups = pending_groups(rows)
    cands = {k: v for k, v in groups.items() if len(v) >= floor}
    if a.digest:
        ids = sorted({int(x) for x in a.ids.split(",") if x.strip()}) if a.ids else \
              sorted(i for v in cands.values() for i in v)
        if not ids:
            print("[wal] --digest 无对象：无候选组且未给 --ids")
            return
        print(f"[wal] 晋升确认材料（digest，共 {len(ids)} 条）:")
        _digest_rows(rows, ids, cfg)
        return
    if not cands:
        tail = ", ".join(f"{k[0]}@{k[1]}x{len(v)}" for k, v in sorted(groups.items())) or "无"
        print(f"[wal] 无晋升候选（阈值 min={floor}）。当前未晋升分组: {tail}")
        return
    print(f"[wal] 晋升候选（同类>={floor}，须走棘轮+人工确认）:")
    for (typ, clause), ids in cands.items():
        print(f"  [{typ}] {clause} x{len(ids)} -> ids {ids}")
    print("[wal] 确认材料: promote-check --digest（自动拼原文+chain 修正链+外链+**同类性三问**）；"
          "mark: mark-promoted --ids <...> --to <新版本号> --cohesion \"<三问逐条回答>\""
          "（判断留给人，但回答须随晋升行留痕，判据 params.json#wal.cohesion_questions）")


def cmd_mark(a):
    """晋升标记（人类控制点）：D-b 起必带 --cohesion「同类性三问逐条回答」——
    阈值只筛候选、不判同类；判断留给人（公理 1），但须随晋升行永久留痕可审（append-only 行加字段）。"""
    cfg = wcfg()
    rows = load()
    ids = {int(x) for x in a.ids.split(",") if x.strip()}
    known = {r["id"] for r in rows if r.get("action") is None}
    bad = ids - known
    if bad:
        sys.exit(f"[wal] 未知 id: {sorted(bad)}")
    resp = (getattr(a, "cohesion", None) or "").strip()
    minlen = cfg["cohesion_min_len"]
    if len(resp) < minlen:
        got = '缺' if not resp else f'{len(resp)} 字'
        sys.exit(f"[wal] 拒绝：mark-promoted 必带 --cohesion「同类性三问逐条回答」（现 {got}，"
                 f"长度下限 {minlen}＝assets/params.json#wal.cohesion_min_len）——"
                 "分组只共享上位公理 ≠ 同类，合并晋升前须逐条回答"
                 "「同一操作/同一判据/可否合成一条动作」（D-b），回答随行留痕")
    append({"action": "promoted", "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "ids": sorted(ids), "to": a.to, "cohesion": resp})
    print(f"[wal] ids {sorted(ids)} 标记为已晋升 -> {a.to}（同类性结论已随行留痕；记得跑 rubric_score.py 棘轮验证）")


def main():
    wcfg()                                  # 启动即校验配置，缺键宁可现在报
    p = argparse.ArgumentParser(description="聚鲸工作流 纠偏WAL")
    sub = p.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("add")
    a1.add_argument("--source", required=True)
    a1.add_argument("--type", required=True)
    a1.add_argument("--text", required=True)
    a1.add_argument("--clause", required=True)
    a1.add_argument("--actor", help="留痕人 actor_id（准入登记表见 assets/params.json#memory.actors，未登记拒绝）")
    a1.add_argument("--ref", help="外链指针（详版文档/条目路径）；文本超软上限时必补")
    a1.add_argument("--chain", help="修正链（机器可读，逗号分隔父 id，须为更早的真实 id）——"
                                    "同根复发换锚点时聚类键看不见，靠此字段让复发可复算（D-a）")
    a2 = sub.add_parser("list")
    a2.add_argument("--clause")
    a2.add_argument("--open", action="store_true")
    a3 = sub.add_parser("promote-check")
    a3.add_argument("--min", type=int, help="晋升阈值（缺省取 assets/params.json#wal.promote_min）")
    a3.add_argument("--digest", action="store_true", help="拼晋升确认材料：原文全文+chain 修正链+外链+同类性三问")
    a3.add_argument("--ids", help="--digest 显式指定组（逗号分隔），缺省用候选组")
    a4 = sub.add_parser("mark-promoted")
    a4.add_argument("--ids", required=True)
    a4.add_argument("--to", required=True)
    a4.add_argument("--cohesion", help="同类性三问逐条回答（必带；长度下限＝assets/params.json#wal.cohesion_min_len）")
    args = p.parse_args()
    {"add": cmd_add, "list": cmd_list, "promote-check": cmd_promote_check,
     "mark-promoted": cmd_mark}[args.cmd](args)


if __name__ == "__main__":
    main()
