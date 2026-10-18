#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚鲸工作流 · 机制消费探针（控制齿回流，防条款空转）
抽条款 → 会话中查「最近产出里真有执行痕迹吗」→ 记录证据（或诚实记无）；
连续达配置窗口轮无消费 → 候选退役（走棘轮+人工确认）。

参数全部外置：条款池、样本条数、退役窗口、证据来源枚举、加载预算判据、
漂移黑名单 —— 都在 assets/params.json；本脚本不内置任何数值与清单，
缺必填键即报错（不设兜底默认值，兜底＝隐藏的硬编码）。

效果实测（复用本脚本，解「复用性 🟡」之困——区分「有痕迹」与「效果对」）:
  python probe_usage.py effect --list
  python probe_usage.py effect --id 1 --mode full_test --verdict pass --note "带skill按规模表选编制，baseline未做裁剪"
  python probe_usage.py effect --id 2 --mode dry_run --verdict fail --note "漏了用户视角断言，被判『绿测掩盖』"

证据独立性（P1.1 补，修验收 B7「自证闭环」）:
  记录必带 --source: self=自报(默认，**不计有效消费**) / witness=独立上下文核实 / machine=命令可复现
  retire-check 只把独立来源当有效消费；self 单列公示（防改动者自己给自己发合格证）。

加载预算机检（替代人工测量，修 B6）:
  python probe_usage.py size [--session SKILL.md,forms/b.md]（--session 复现会话口径）
  size 同时做三件校对：① 文档声明的锚点 ⊆ 抽检池（修 B4 悬空锚点）
  ② 文档不得复制配置数值、不得引用已删件（防两处漂移）③ 数值命中行的豁免须写理由

用法:
  python probe_usage.py sample [--n <条数，缺省取配置>]
  python probe_usage.py record --clause "G2.5" --actor kf41 --evidence "xxx" --source machine
    （M2 留痕纪律：--actor 必带且须在 assets/params.json#memory.actors 准入登记，缺省/未登记即拒）
  python probe_usage.py record --clause "成本四行" --actor kf41 --none
  python probe_usage.py record --clause "上线四问" --actor kf41 --off-round --evidence "…" --source witness
    （会话消费通道：本轮未抽中但真实用过的经验；仅独立源、锚点须在池、同锚点去重、
    不入轮转降级判定）
  python probe_usage.py retire-check [--window <轮数，缺省取配置>]
  python probe_usage.py cutoff --reason "…"（历史退役分界行正规通道；理由长度机检，
    下限＝assets/params.json#probe.cutoff_min_reason_len——P2 验收整改：免检理由曾致
    与事实不符的陈述永久留档，WAL #24 勘误）
  python probe_usage.py kb-lint（经验库分层十项机检，P3 五项＋M1 记忆系统三项＋M2 佐证 v2：
    ⑤ 读 params.json#memory.endorsement.mode 切档（dual_track/cross_actor_only）＋actors 准入比对，
    同人/无 actor 历史行标弱佐证，未登记 actor 报红；判据见 assets/params.json#knowledge 与 #memory）
  python probe_usage.py heat（条款热度回流榜：正向＝独立源真消费命中加分／负向＝空转扣分，
    非对称权重与榜单条数取 assets/params.json#governance；**观测非门**常态恒 0 退出
    （非 0 仅配置错误一类 fail-closed，非判定），
    只作晋升/退役评审参考——不作任何自动判定）

版本指纹自检（⑥版本管理判据用于 skill 自身，2026-09-28 产品负责人令）:
  python probe_usage.py version（data/VERSION＝SKILL.md H1＝frontmatter description 三处对照，
    不一致/缺失即红退出 1；升版本三处同改后必跑；分发件 zip 仅观测不作门）

待发版池机检（v2.2.0·D8 三层分离：机制件在 skill，实例池在产品每仓一份）:
  python probe_usage.py pool-check [--pool <产品仓/PENDING_RELEASES.md>]
    （模板同构/状态值域/日期格式＝红退出 1；观察滞留与发版触发＝提示不进退出码；
    判据 assets/params.json#release_pool，模板 assets/pending-releases.template.md）

措辞绝对化观测（锁定批·甲 ②-B；判据 assets/params.json#wording）:
  python probe_usage.py wording [--since YYYY-MM-DD] [--files a,b] [--show-exc]
    （原提案＝词表机检门，实测 95 行次命中/77 处未带例外且主体是合法用法 ⇒ 判定面过宽必被绕过
     （陷阱 #19），且「措辞是否失实」属判断（公理 1 归人）⇒ 降级为**观测非门**：
     只列清单、不判罪、常态恒 0 退出、不进任何机检码；scope=changed 靠 --since 按文件 mtime 圈定）

机检断言自检（锁定批·甲 ②-A；判据＝assets/params.json 各节）:
  python probe_usage.py selfcheck
    （把对外**绝对化的事实断言**从文档搬进可跑复算：每条断言＝一个隔离负测试，逐条打印
     「断言→期望→实测→OK/FAIL」，任一 FAIL 即红退出 1；wal 侧走 JUJING_DATA_DIR 临时库，
     跑完即删，真实台账零污染。文档只留判据名＋证据出处＝本子命令）

数据: <skill>/data/probe.jsonl （消费痕迹）；<skill>/data/effect.jsonl （效果实测）
参数: <skill>/assets/params.json
"""
import argparse, fnmatch, json, os, random, re, shutil, subprocess, sys, tempfile, datetime, pathlib, collections

# 编码坑根治（陷阱 #7 第 4 次复发）：Windows 控制台 GBK 直接 print 符号会崩。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_DIR = _ROOT / "data"
_DATA_DIR = pathlib.Path(os.environ.get("JUJING_DATA_DIR", str(_DEFAULT_DIR)))
DATA = _DATA_DIR / "probe.jsonl"
EFFECT = _DATA_DIR / "effect.jsonl"
_PROMPTS = _ROOT / "assets" / "test-prompts.json"
_PARAMS = _ROOT / "assets" / "params.json"

_REQUIRED_TOP = ("budget", "probe", "wal", "rubric", "team", "governance", "kb", "knowledge",
                 "memory", "loop", "drift", "wording", "clauses", "consumers", "release_pool")


def _req(cfg, keys, where):
    """必填键校验（键可为点路径，如 budget.session.limit_kb）：缺键即报 ——
    不设兜底默认值（兜底＝隐藏的硬编码，且会把「缺配置」静默降级成假绿）。"""
    for k in keys:
        cur = cfg
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                sys.exit(f"[probe] {where} 缺少必填键 {k}")
            cur = cur[part]


def _dig(cfg, path, where):
    """按点路径取值（缺键即报，不返回 None 静默继续）。"""
    _req(cfg, (path,), where)
    cur = cfg
    for part in path.split("."):
        cur = cur[part]
    return cur


def params():
    """全量参数读 assets/params.json —— 唯一事实源，代码不内置数值与清单。"""
    if not _PARAMS.exists():
        sys.exit(f"[probe] 缺少配置 {_PARAMS} —— 参数唯一事实源")
    try:
        conf = json.loads(_PARAMS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[probe] {_PARAMS.name} 解析失败: {e}")
    _req(conf, _REQUIRED_TOP, "assets/params.json")
    _req(conf["budget"], ("default", "by_glob", "session", "scan_roots", "scan_files"),
         "assets/params.json#budget")
    _req(conf["budget"]["session"], ("limit_kb",), "assets/params.json#budget.session")
    _req(conf["probe"], ("sample_n", "retire_window", "self_stall_warn", "effect_sample_max",
                         "evidence_sources", "independent_sources", "cutoff_min_reason_len"),
         "assets/params.json#probe")
    _req(conf["drift"], ("forbid", "forbid_regex", "dead", "exempt_marker", "exempt_min_reason_len"),
         "assets/params.json#drift")
    _req(conf["wording"], ("scope", "absolute_words", "exception_markers", "scan_extra"),
         "assets/params.json#wording")
    _req(conf["knowledge"], ("index_dir", "entries_dir", "required_meta", "index_limit_kb"),
         "assets/params.json#knowledge")
    _req(conf["memory"], ("routing_table", "privacy_default", "privacy_values",
                          "status_values_extra", "weight_tiers", "ownership_criteria", "endorsement"),
         "assets/params.json#memory")
    _req(conf["loop"], ("experience_loop", "backflow_hooks", "profile_template", "profile_anchor_doc"),
         "assets/params.json#loop")
    # 消费声明覆盖校验：每节必须写清「谁读它」，只被文档引用的节须显式标注
    # —— 否则「数值搬家」会被当成「已机检」，是去硬编码后最像进步的假绿。
    undeclared = [k for k in conf
                  if not k.startswith("_") and k != "consumers" and k not in conf["consumers"]]
    if undeclared:
        sys.exit(f"[probe] assets/params.json 有节未声明消费者: {undeclared}")
    return conf


def actor_check(conf, actor, where):
    """M2 actor 准入校验（判据 assets/params.json#memory.actors·actor_policy）：
    required=true 时缺 --actor 即拒；未登记 actor 即拒——准入登记表是唯一事实源，
    无默认人（默认人会把匿名留痕洗白成他人佐证）。"""
    mem = conf["memory"]
    _req(mem, ("actors", "actor_policy"), "assets/params.json#memory")
    _req(mem["actor_policy"], ("required",), "assets/params.json#memory.actor_policy")
    actor = (actor or "").strip()
    registered = [k for k in mem["actors"] if not k.startswith("_")]
    if not actor:
        if mem["actor_policy"]["required"]:
            sys.exit(f"[{where}] 拒绝：留痕必带 --actor（判据 params.json#memory.actor_policy.required；"
                     f"登记表 params.json#memory.actors，未登记者先走 routing.md §七 第三步准入）")
        return None
    if actor not in registered:
        sys.exit(f"[{where}] 拒绝：actor '{actor}' 未登记（准入登记表 params.json#memory.actors，"
                 f"现登记 {registered}）——未登记留痕不得入档，先准入或改用已登记 actor")
    return actor


def clauses():
    """抽检池来自 assets/params.json#clauses —— 代码不内置条款清单（去硬编码）。"""
    pool = params()["clauses"]
    if not pool:
        sys.exit("[probe] assets/params.json#clauses 为空 —— 抽检池不得为空")
    return list(pool)


def load():
    if not DATA.exists():
        return []
    rows = []
    for line in DATA.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("action") == "cutoff":
            # 分界行（P2 销债 S2，evolution#17）：此前历史（如无 source 的旧格式记录）
            # 整体退役。append-only：不改写历史行，只加分界行，读侧丢弃。
            rows = []
            continue
        rows.append(row)
    return rows


def append(row):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    with DATA.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rounds_of(rows):
    """按 sample 的批次分组：sample 行开启一轮，record 归属最近一轮。
    off_round 行（P3 会话消费通道）不入轮：降级判定只看抽检轮，轮转抽检不被绕过。"""
    rounds, cur = [], None
    for r in rows:
        if r.get("action") == "sample":
            cur = {"ts": r["ts"], "clauses": r["clauses"], "records": []}
            rounds.append(cur)
        elif r.get("action") == "record" and cur is not None:
            if r.get("off_round"):
                continue
            cur["records"].append(r)
    return rounds


def cmd_sample(a):
    rows = load()
    pool = clauses()
    # 轮转优先：从「历史被抽中次数最少」的条款里随机选。
    # 理由（独立复评）：纯随机会让单条被连续抽中的概率极低 ⇒ 退役判定永不触发 ＝ 假门。
    hist = collections.Counter()
    for r in rows:
        if r.get("action") == "sample":
            hist.update(r.get("clauses") or [])
    least = min(hist.get(c, 0) for c in pool)
    front = [c for c in pool if hist.get(c, 0) == least]
    picks = random.sample(front, min(a.n, len(front)))
    if len(picks) < a.n:
        rest = [c for c in pool if c not in picks]
        picks += random.sample(rest, min(a.n - len(picks), len(rest)))
    append({"action": "sample", "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "clauses": picks})
    print(f"[probe] 本轮抽检 {len(picks)} 条（轮转优先：先抽历史被抽中最少的条款）:")
    for c in picks:
        print(f"  - {c}")
    print("[probe] 记录: record --clause <条> --evidence <证据> 或 --none（诚实第一）")


def cmd_record(a):
    conf = params()
    sources = tuple(conf["probe"]["evidence_sources"])
    independent = tuple(conf["probe"]["independent_sources"])
    actor = actor_check(conf, a.actor, "probe")          # M2：准入校验（缺省/未登记均拒）
    if a.source not in sources:
        sys.exit(f"[probe] --source 必须是 {sources}")
    if bool(a.evidence) == bool(a.none):
        sys.exit("[probe] --evidence 与 --none 二选一（无消费证据也要诚实记录）")
    if a.off_round:
        # 会话消费通道（P3，PM 裁判必改项 F1）：真实会话消费了某条经验但本轮未被抽中时，
        # 消费留痕有路可走。防滥用四闸：① 仅独立源（self 自报不入档）；
        # ② 不入轮转统计（rounds_of 跳过）——降级判定仍只看抽检轮；
        # ③ clause 须在抽检池（AI Manager 二审必改3：池外锚点不得借道 off-round 入档，
        # 否则「先编个池外锚点再留痕」可绕过锚点注册机检）；
        # ④ 同 (clause, actor) 去重（防单人刷次数）；M2：跨 actor 允许追加——同一经验
        # 第二个人的消费留痕正是佐证 v2 跨人强佐证的来源，不拒。
        if a.source not in independent:
            sys.exit(f"[probe] --off-round 会话消费仅接受独立源 {independent}（self 自报不入档）")
        if a.none:
            sys.exit("[probe] --off-round 用于记录真实发生的会话消费，无消费无须留痕（去掉 --none）")
        if not (a.evidence or "").strip():
            sys.exit("[probe] --off-round 须带 --evidence（会话消费须指认可核证据）")
        if a.clause not in clauses():
            sys.exit(f"[probe] '{a.clause}' 不在抽检池（off-round 只给已注册锚点留痕，"
                     f"判据 assets/params.json#clauses）")
        dup = [r for r in load() if r.get("off_round") and r.get("clause") == a.clause
               and r.get("actor") == actor]
        if dup:
            sys.exit(f"[probe] '{a.clause}' 已有 actor={actor} 的 off-round 留痕（{dup[0].get('ts')}）——"
                     "同 actor 同 clause 不重复入档；跨人消费另计佐证（endorsement v2）")
        append({"action": "record", "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "clause": a.clause, "consumed": True, "source": a.source, "independent": True,
                "actor": actor,
                "evidence": (a.evidence or "").strip(), "off_round": True})
        print(f"[probe] {a.clause}: 会话消费留痕（off-round·{a.source}·actor={actor}；不入轮转降级判定，降级只看抽检轮）")
        return
    rows = load()
    rs = rounds_of(rows)
    if not rs:
        sys.exit("[probe] 先 sample 再 record")
    latest = rs[-1]
    if a.clause not in latest["clauses"]:
        sys.exit(f"[probe] '{a.clause}' 不在本轮抽检清单 {latest['clauses']}")
    dup = [r for r in latest["records"] if r["clause"] == a.clause]
    if dup:
        sys.exit(f"[probe] '{a.clause}' 本轮已记录")
    src = "none" if a.none else a.source
    append({"action": "record", "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "clause": a.clause,
            "consumed": not a.none,
            "source": src,
            "independent": (not a.none) and a.source in independent,
            "actor": actor,
            "evidence": (a.evidence or "").strip()})
    if a.none:
        tag = "无消费（诚实记录）"
    elif a.source == "self":
        tag = f"自报证据（**不计入有效消费**；需 {'/'.join(independent)} 才清零）"
    else:
        tag = f"有效消费（{a.source}）"
    print(f"[probe] {a.clause}: {tag}（actor={actor}）")


def cmd_retire_check(a):
    conf = params()
    rows = load()
    rs = rounds_of(rows)
    if len(rs) < a.window:
        print(f"[probe] 样本不足（完成轮数 {len(rs)} < window {a.window}），暂不判定")
        return
    streak = collections.defaultdict(int)
    self_only = collections.Counter()
    legacy = 0
    for r in rs[-a.window:]:
        for rec in r["records"]:
            c = rec["clause"]
            if not rec.get("consumed"):
                streak[c] += 1
            elif rec.get("independent"):
                streak[c] = 0          # 只有独立来源才算有效消费、才清零
            elif rec.get("source") is None:
                legacy += 1            # 新规则前写入的历史记录：不追认为自报，也不清零
            else:
                self_only[c] += 1      # 自报：不清零、不计空转，单列公示（防自证闭环）
    if legacy:
        print(f"[probe] 历史记录 {legacy} 条无 source 字段（新规则前写入）→ 不追认为自报、也不清零")
    if self_only:
        print("[probe] 自报证据（自证，不计有效消费）: " +
              "、".join(f"{c}×{n}" for c, n in self_only.items()))
    cands = {c: n for c, n in streak.items() if n >= a.window}
    if cands:
        print(f"[probe] 机制空转告警/退役候选（连续 {a.window} 轮无消费）:")
        for c, n in cands.items():
            print(f"  - {c} x{n} -> 按退役五判据评估，走棘轮+人工确认")
    else:
        print("[probe] 无退役候选")
    # 自身空转监督：只数「尾部连续」未完成轮次 —— 一旦完成一轮即归零。
    # （独立复评：原实现把全部历史轮次计入 ⇒ 旧遗留轮次让告警恒红、永不归零 ＝ 假门）
    stall = conf["probe"]["self_stall_warn"]
    tail = 0
    for r in reversed(rs):
        if len(r["records"]) >= len(r["clauses"]):
            break
        tail += 1
    if tail >= stall:
        print(f"[probe] 警告: 探针自身连续 {tail} 轮未完成全量记录 -> 向用户诚实报告机制空转")


def cmd_heat(a):
    """条款热度回流（锁定批·D2，观测非门）：把消费探针的痕迹按**条款**聚合回流——
    正向加分＝独立源真消费（命中）；空转扣分＝**未记消费证据**（`consumed=false`，含
    `--none` 与其它无消费路径——不止 --none 一种，勿窄读）。两项用非对称权重（误报惩罚
    重于命中奖励，空转权重取负值即扣分项）加权，权重读 assets/params.json#governance，
    脚本不内置数值（零硬编码）。
    为何要它：原飞轮只有负向闭环（零消费→retire-check 退役），条款「被真实高频使用」
    这一正向信号无处可见；评审晋升/退役只能看「有没有痕迹」，看不出「用得多不多」。
    诚实边界：① 观测非门——**常态恒 0 退出**，不作退役/晋升自动判定（退役仍走 retire-check
    五判据＋人工确认）；**非 0 路径＝配置错误一类**（`params.json` 缺失或解析失败、必填节/
    子键缺失、`clauses` 池为空 ⇒ `_req()`／`clauses()` fail-closed 报错退出）——不是本命令
    的「判定」，故不违观测非门；写「恒 0」时须带此例外，勿绝对化；② 只读不改写
    （probe.jsonl append-only 不受影响）；③ 榜单按热度分降序取 top_n（**含负分与零分条目**，
    不是「只挑正分」），扣分条款与零痕迹条款另有全量公示段——不藏坏消息；④ 历史无源行与
    自报行均不计有效命中（防自证闭环）；**「同口径」= 继承 record 期写入的 `independent`
    标志**——本命令不复算 `probe.independent_sources`，故是**继承而非复验**（record 侧判据
    若变，heat 须同步复核）；且**口径三处差异须记住**：⁽¹⁾窗口 heat＝cutoff 后全史、
    retire-check＝尾窗 `probe.retire_window` 轮；⁽²⁾heat 计入 `--off-round` 会话消费行、
    retire-check 轮转统计不含；⁽³⁾legacy 分支＝历史兼容：`load()` 遇 cutoff 分界行即**丢弃其前
    全部行**，故 legacy 只在「尚无 cutoff 行的库」中可达（本库无分界行时历史无源行存活并计入
    空转扣分；一旦写入分界行，legacy 归零）——勿读成「cutoff 前的行仍被本命令统计」；⑤ 负测试
    整改（本批自测发现）：「独立源人」只数独立命中的人、「最近命中」只认独立命中时间——
    自报/空转行不得冒充独立佐证，否则榜面会误导评审；⑥ 空转计数受抽检样本量影响（同一
    clause 被抽中越多、扣分越大），故输出侧须提示样本量：判读先看命中/空转比，再看绝对分；
    ⑦ 排序对缺 clause 的历史行容错（None→"" 参与兜底排序，防并列分时 str/None 比较抛
    TypeError）。"""
    conf = params()
    gov = conf["governance"]
    _req(gov, ("asym_hit_weight", "asym_false_positive_weight", "heat_top_n"),
         "assets/params.json#governance")
    w_hit = gov["asym_hit_weight"]
    w_fp = gov["asym_false_positive_weight"]
    top_n = max(0, int(gov["heat_top_n"]))
    rows = load()
    stat = collections.defaultdict(lambda: {"hit": 0, "self": 0, "legacy": 0, "miss": 0,
                                            "actors": set(), "last": None})
    n_rec = 0
    for r in rows:
        if r.get("action") != "record":
            continue
        n_rec += 1
        s = stat[r.get("clause")]
        if not r.get("consumed"):
            s["miss"] += 1
        elif r.get("independent"):
            s["hit"] += 1
            if r.get("actor"):
                s["actors"].add(r["actor"])
            ts = r.get("ts") or ""
            if ts and (s["last"] is None or ts > s["last"]):
                s["last"] = ts
        elif r.get("source") is None:
            s["legacy"] += 1
        else:
            s["self"] += 1
    if not n_rec:
        print("[heat] 无消费记录（data/probe.jsonl 无 record 行）——热度榜空，先跑 sample/record")
        return 0
    scored = sorted(((c, s["hit"] * w_hit + s["miss"] * w_fp, s) for c, s in stat.items()),
                    key=lambda t: (-t[1], -t[2]["hit"], t[0] or ""))
    print("[heat] 条款热度回流榜（命中x权重 ＋ 空转x权重；非对称，权重见 params.json#governance）")
    print(f"[heat] 覆盖 {len(scored)} 条款 / {n_rec} 条探针记录｜榜单条数上限 "
          f"params.json#governance.heat_top_n（按热度分降序，含负分/零分条目）")
    for c, score, s in scored[:top_n]:
        extra = f"｜历史无源 {s['legacy']}" if s["legacy"] else ""
        last = s["last"][:10] if s["last"] else "—"
        print(f"  {score:+.2f}  {c}  [命中 {s['hit']}｜自报 {s['self']}｜空转 {s['miss']}{extra}"
              f"｜独立源 {len(s['actors'])} 人｜最近命中 {last}]")
    rest = scored[top_n:]
    if rest:
        print(f"[heat] 未上榜 {len(rest)} 条（全量公示，不藏坏消息）: "
              + "、".join(f"{c}{sc:+.2f}" for c, sc, _ in rest))
    neg_all = [(c, sc) for c, sc, _ in scored if sc < 0]
    if neg_all:
        print(f"[heat] 扣分条款 {len(neg_all)} 条（无论是否上榜，一律公示）: "
              + "、".join(f"{c}{sc:+.2f}" for c, sc in neg_all))
    pool = clauses()
    cold = [c for c in pool if c not in stat]
    if cold:
        print(f"[heat] 池内零痕迹 {len(cold)}/{len(pool)} 条（未记为错：sample 轮转优先会覆盖；"
              f"连续无消费见 retire-check）: " + "、".join(cold))
    print("[heat] 判读口径：空转计数受抽检样本量影响（同一 clause 被抽中越多、扣分越大），"
          "先看「命中/空转比」再看绝对分；时间窗＝cutoff 后全史（与 retire-check 尾窗不可混用）")
    print("[heat] 回流去向: 晋升（promote-check）与退役（retire-check 五判据）评审的热度参考")
    print("[heat] 观测非门：常态恒 0 退出（非 0 仅配置错误 fail-closed），不构成任何自动判定"
          "——热度低≠该退役，热度高≠可免审")
    return 0


def _load_effect():
    if not EFFECT.exists():
        return []
    rows = []
    for line in EFFECT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[probe] 跳过损坏行: {line[:80]}", file=sys.stderr)
    return rows


def _load_prompts():
    if not _PROMPTS.exists():
        sys.exit(f"[probe] 题集缺失: {_PROMPTS}（先建 assets/test-prompts.json）")
    try:
        return json.loads(_PROMPTS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[probe] test-prompts.json 解析失败: {e}")


def cmd_effect(a):
    """效果实测（带 skill vs 不带 baseline 双轨，诚实标注 full_test/dry_run）。
    与「消费探针」的分工：探针问『有没有痕迹』，effect 问『痕迹对不对』——
    条款有痕迹但效果错，同属公理 5『绿测掩盖死功能』。"""
    if a.list:
        prompts = _load_prompts()
        print(f"[probe] 题集（{len(prompts)} 条）:")
        for t in prompts:
            print(f"  #{t['id']} {t['prompt'][:40]}…")
        rows = _load_effect()
        if rows:
            print(f"[probe] 已记录 {len(rows)} 次效果实测")
        return
    if a.mode not in ("full_test", "dry_run"):
        sys.exit("[probe] --mode 必须是 full_test（真跑子agent双轨）或 dry_run（模拟推演）")
    if a.verdict not in ("pass", "fail"):
        sys.exit("[probe] --verdict 必须是 pass 或 fail")
    prompts = _load_prompts()
    ids = {t["id"] for t in prompts}
    if a.id not in ids:
        sys.exit(f"[probe] 题 #{a.id} 不在 test-prompts.json（现有: {sorted(ids)}）")
    row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "id": a.id, "mode": a.mode, "verdict": a.verdict,
           "note": (a.note or "").strip()}
    if not row["note"]:
        sys.exit("[probe] --note 必填：效果实测必须写『带skill vs baseline 差在哪』，空话=无效（防自欺）")
    EFFECT.parent.mkdir(parents=True, exist_ok=True)
    with EFFECT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[probe] 题#{a.id} 效果实测已记录 [{a.mode}/{a.verdict}]")


def _drift_scan(conf, files):
    """防数值漂移 + 死引用：文档不得复制配置里的数值，也不得引用已删件。
    历史引述须在同行写 <exempt_marker>:<理由> —— 白名单必须附理由，否则形同合法签发。"""
    drift = conf["drift"]
    marker, forbid, dead = drift["exempt_marker"], drift["forbid"], drift["dead"]
    regexes = [(p, re.compile(p)) for p in drift["forbid_regex"]]
    min_reason = drift["exempt_min_reason_len"]
    scan = sorted(set(files) | {p for p in (_ROOT / "assets").glob("*.json") if p != _PARAMS})
    hits, exempt, bare = [], [], []
    for f in scan:
        rel = f.relative_to(_ROOT).as_posix()
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if marker in line:
                reason = line.split(marker, 1)[1].split("-->")[0].strip(" :：-\t")
                (exempt if len(reason) >= min_reason else bare).append((rel, i, reason))
                continue
            for lit in forbid + dead:
                if lit in line:
                    hits.append((rel, i, lit, line.strip()[:70]))
            for pat, rx in regexes:
                if rx.search(line):
                    hits.append((rel, i, pat, line.strip()[:70]))
    print(f"[size] 数值/死引用漂移: {len(hits)} 处未解释命中（豁免 {len(exempt)} 行逐行列出；"
          f"豁免未写理由 {len(bare)} 行）")
    for rel, i, lit, snip in hits:
        print(f"  ! {rel}:{i} 命中「{lit}」 -> {snip}")
    # 豁免必须可审计：逐行打印理由，空/过短理由＝未豁免（否则白名单就是合法的绕过口子）
    for rel, i, reason in exempt:
        print(f"  ~ {rel}:{i} 豁免（{reason}）")
    for rel, i, reason in bare:
        print(f"  ! {rel}:{i} 豁免未写理由（须≥{min_reason} 字）-> {reason!r}")
    return len(hits) + len(bare)


def _key_ref_scan(conf, files):
    """参数键引用校对：文档写的 `params.json#<点路径>` 必须在配置里真的存在。
    修 B4 的同类病（悬空引用）——去硬编码后文档改成引用键名，若键名打错就成了新的死引用，
    靠人眼复查等于没查（公理 5），故机检。"""
    pat = re.compile(r"params\.json#([A-Za-z_][A-Za-z0-9_.]*)")
    bad = []
    for f in files:
        rel = f.relative_to(_ROOT).as_posix()
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for key in pat.findall(line):
                cur = conf
                for part in key.split("."):
                    if not isinstance(cur, dict) or part not in cur:
                        bad.append((rel, i, key))
                        break
                    cur = cur[part]
    print(f"[size] 参数键引用: {len(bad)} 处指向不存在的键")
    for rel, i, key in bad:
        print(f"  ! {rel}:{i} 引用了不存在的参数键 params.json#{key}")
    return len(bad)


def _consumers_scan(conf):
    """消费声明的**可落实**校验：只查「有没有写声明」＝声明可以空转（覆盖 n/n 恒绿＝死判据）。
    故：显式写「仅文档引用」的节放行；其余节必须指名脚本/文件，且指名件真的存在——
    声明与实际消费对不上（改了代码不更声明）即非零退出，不靠人记。"""
    bad = []
    pat = re.compile(r"([A-Za-z_][A-Za-z0-9_/.-]*\.(?:py|md|json))")
    rows = {k: str(v).strip() for k, v in conf["consumers"].items() if not k.startswith("_")}
    for k, v in rows.items():
        if not v:
            bad.append((k, "声明为空"))
        elif v.startswith("仅文档"):
            continue
        else:
            named = pat.findall(v)
            if not named:
                bad.append((k, "声称机检却未指名脚本/文件（空话声明）"))
            else:
                miss = [p for p in named
                        if not (_ROOT / p).exists() and not (_ROOT / "references" / p).exists()
                        and not (_ROOT / "scripts" / p).exists()]
                if miss:
                    bad.append((k, f"指名的消费者不存在：{miss}"))
                    continue
                # 追到代码级（P1.2，修独立复评「可落实仍是弱校验」）：指名的 .py 脚本必须
                # 在源码里真的引用了该节名 —— 只验文件存在＝声明可指向任意脚本而恒绿。
                code_ok = []
                for p in named:
                    fp = next((c for c in (_ROOT / p, _ROOT / "references" / p, _ROOT / "scripts" / p)
                               if c.exists()), None)
                    if fp is None or fp.suffix != ".py":
                        continue
                    try:
                        src = fp.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    if k in src:
                        code_ok.append(p)
                if not code_ok:
                    bad.append((k, f"指名的脚本源码未见引用节名「{k}」（声明与代码对不上）"))
    idle = [k for k, v in rows.items() if v.startswith("仅文档")]
    print(f"[size] 消费声明可落实: {len(rows) - len(idle)}/{len(rows)} 节声称机检"
          f"（{len(idle)} 节显式仅文档引用）" + ("" if not bad else f"；{len(bad)} 节不合规"))
    for k, why in bad:
        print(f"  ! consumers.{k}: {why}")
    return len(bad)


def cmd_cutoff(a):
    """历史退役分界行正规通道（P2 验收整改）：此前历史读侧整体退役，append-only 不改写行。
    reason 必填且达 assets/params.json#probe.cutoff_min_reason_len —— 分界行理由是永久
    留档的事实声明，免检曾致「以偏概全的理由」入档（WAL #24 勘误），故与 drift 豁免同规格设机检。"""
    conf = params()
    reason = (a.reason or "").strip()
    minlen = conf["probe"]["cutoff_min_reason_len"]
    if len(reason) < minlen:
        print(f"[cutoff] 拒绝：--reason 须达 {minlen} 字（当前 {len(reason)}）——"
              f"分界行理由永久留档，不得免检（判据 assets/params.json#probe.cutoff_min_reason_len）")
        return 1
    append({"action": "cutoff", "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "reason": reason})
    print("[cutoff] 分界行已记录（此前历史读侧退役；append-only 未改写任何行）")


def _entry_anchors(head):
    """条目锚点行提取（> 锚点（供探针抽样）：`A`｜`B`）——与 size 声明扫描同语义。"""
    for line in head.splitlines():
        if "锚点" in line and "：" in line:
            toks = [t.strip().strip("`* ") for t in line.split("：", 1)[-1].split("｜")]
            return [t for t in toks if t]
    return []


def cmd_kblint(a):
    """经验库分层八项机检（P3 五项判据读 assets/params.json#knowledge；M1 记忆系统 ⑥⑦⑧
    判据读 assets/params.json#memory，同修 B6 病）。
    ① 索引完整（INDEX 列出的条目文件存在，无死链）② 条目全注册（entries/ 无游离件，
    递归含子目录——逃进子目录躲机检＝游离）③ 元数据齐（required_meta 逐字段）
    ④ 锚点在抽检池（探针抽得到＝可被消费）
    ⑤ 消费可追溯（retire 态条目须有 probe.jsonl **独立源真消费**轨迹佐证——
    consumed=true 且 source∈independent_sources，抽检轮或 off-round 均算；
    --none（零消费）与 self 自报不解锁佐证＝假绿；candidate 无记录＝合法初始态）
    ⑥ 记忆区健康（README 调用判据表在位，params.json#memory.routing_table）
    ⑦ 目录纪律（两级封顶＝层/二级子目录/文件；结构纪律非大小门，条数只观测）
    ⑧ 隐私值域（privacy ∈ params.json#memory.privacy_values；archived＝人工判旧，召回默认跳过）
    ⑨ 双循环咬合（P4：机制件在位＋④/⑧出口判据挂双回流＋档案模板被接入协议引用＋
    内环四段留痕重放＝写入→消费→评分→晋升/退役；promoted ids 悬空＝复算断裂红）。
    ⑩ 个人层咬合（M3-lite：active 层目录在盘＋条目注册/元数据/锚点在池/隐私值域；
    pending 层偷建目录＝红——「顺手就建」＝膨胀，减法纪律机检化）。"""
    conf = params()
    kn = conf["knowledge"]
    mem = conf["memory"]
    idx = _ROOT / kn["index_dir"] / "INDEX.md"
    ent = _ROOT / kn["entries_dir"]
    meta_keys = tuple(kn["required_meta"])
    privacy_values = tuple(mem["privacy_values"])
    privacy_default = mem["privacy_default"]
    rt = _ROOT / mem["routing_table"]
    archived_note = []
    errs = []
    if not idx.exists():
        print(f"[kb-lint] 缺索引 {idx} —— 分层失效")
        return 1
    if not rt.exists():
        errs.append(f"路由表缺失:{mem['routing_table']}（kb-lint ⑥；判据 params.json#memory.routing_table）")
    text = idx.read_text(encoding="utf-8")
    listed = sorted(set(re.findall(r"knowledge/entries/[\w.\-/]+\.md", text)))
    actual = sorted(f"knowledge/entries/{p.relative_to(ent).as_posix()}"
                    for p in ent.rglob("*.md")) if ent.exists() else []
    deep = [f"knowledge/entries/{p.relative_to(ent).as_posix()}"
            for p in ent.rglob("*.md") if len(p.relative_to(ent).parts) > 1] if ent.exists() else []
    if deep:
        errs.append(f"三级目录违反两级封顶:{deep}（层/二级子目录/文件）")
    dead = [p for p in listed if not (_ROOT / p).exists()]
    stray = [p for p in actual if p not in listed]
    print(f"[kb-lint] ① 索引完整: {'OK' if not dead else '死链 ' + str(dead)}"
          f"（索引列 {len(listed)} 条）")
    print(f"[kb-lint] ② 条目全注册: {'OK' if not stray else '游离件 ' + str(stray) + '（存在但 INDEX 未收录）'}"
          f"（entries {len(actual)} 件）")
    errs += [f"死链:{p}" for p in dead] + [f"游离:{p}" for p in stray]
    pool = set(clauses())
    independent = tuple(conf["probe"]["independent_sources"])
    # 佐证口径（AI Manager 二审必改2后半）：只认「独立源真消费」——consumed=true 且
    # source∈independent_sources；sample（只是抽中≠消费）、--none（consumed=false）、
    # self 自报（自证）均不解锁 retire 佐证。off-round 行同为独立源真消费，算数。
    # M2 佐证 v2（判据 params.json#memory.endorsement·actors）：独立源闸之上按 mode 切档——
    # dual_track：独立源即解锁，但同人／无 actor 历史行标「弱佐证」提示；
    # cross_actor_only：须同一锚点有效消费行来自 ≥2 个已登记 actor 才解锁（切档零代码）。
    rows = load()
    consumed = {r.get("clause") for r in rows
                if r.get("action") == "record" and r.get("consumed")
                and r.get("source") in independent}
    _req(mem, ("endorsement", "actors", "actor_policy", "personal_layers"), "assets/params.json#memory")
    _req(mem["endorsement"], ("mode",), "assets/params.json#memory.endorsement")
    mode = mem["endorsement"]["mode"]
    actors_reg = {k: v for k, v in mem["actors"].items() if not k.startswith("_")}
    by_clause = collections.defaultdict(set)   # 锚点 -> 有效消费行的已登记 actor 集
    legacy_rows = 0                            # M1 前无 actor 历史行：弱佐证诚实标注，不洗白
    unreg_actors = set()                       # 未登记 actor 行（多为合并带入）：准入纪律，红
    for r in rows:
        if r.get("action") == "record" and r.get("consumed") and r.get("source") in independent:
            act = r.get("actor")
            if act is None:
                legacy_rows += 1
            elif act in actors_reg:
                by_clause[r.get("clause")].add(act)
            else:
                unreg_actors.add(act)
    if unreg_actors:
        errs.append(f"留痕含未登记 actor {sorted(unreg_actors)}（准入登记表 params.json#memory.actors；"
                    "人工准入登记或人工删除该批行——append-only 数据 kb-lint 不代改）")
    if mode == "cross_actor_only":
        consumed_v2 = {c for c, acts in by_clause.items() if len(acts) >= 2}
    else:
        consumed_v2 = consumed
    weak_anchors = sorted(c for c in consumed_v2 if len(by_clause.get(c, set())) < 2)
    pending = []
    for rel in actual:
        head = (_ROOT / rel).read_text(encoding="utf-8")
        miss = [k for k in meta_keys
                if not re.search(rf"^\s*>?\s*{re.escape(k)}\s*[（(：:]", head, re.M)]
        if miss:
            errs.append(f"{rel}: 缺元数据 {'、'.join(miss)}")
        m_src = re.search(r"^\s*>?\s*溯源\s*[（(：:](.*)$", head, re.M)
        if m_src and len(m_src.group(1).strip().strip("`* ")) < 4:
            errs.append(f"{rel}: 溯源非空校验失败（须指认真实来源，TBD／占位不可过）")
        anchors = _entry_anchors(head)
        if not anchors:
            errs.append(f"{rel}: 无锚点行（探针永远抽不到＝不可消费的死条目）")
        unreg = [t for t in anchors if t not in pool]
        if unreg:
            errs.append(f"{rel}: 锚点未注册进抽检池 {unreg}")
        m = re.search(r"^\s*>?\s*状态\s*[（(：:](.+)$", head, re.M)
        state = m.group(1) if m else ""
        if ("retire" in state.lower() or "退役" in state) and not (set(anchors) & consumed_v2):
            errs.append(f"{rel}: retire 态但 probe.jsonl 无锚点轨迹佐证（消费不可追溯，佐证口径 endorsement.mode={mode}）")
        if "archived" in state:
            archived_note.append(rel)
        m_pv = re.search(r"^\s*>?\s*隐私\s*(?:[（(][^）)]*[）)])?\s*[：:](.*)$", head, re.M)
        if m_pv:
            pv = m_pv.group(1).strip().strip("`* ")
            if pv and pv not in privacy_values:
                errs.append(f"{rel}: 隐私值 {pv!r} 不在值域 {list(privacy_values)}（判据 params.json#memory.privacy_values）")
        if not (set(anchors) & consumed):
            pending.append(rel)
    print(f"[kb-lint] ③ 元数据齐: {'OK' if not any('缺元数据' in e for e in errs) else '见错误清单'}"
          f"（必填字段 {'、'.join(meta_keys)}，判据 params.json#knowledge.required_meta）")
    print(f"[kb-lint] ④ 锚点在抽检池: {'OK' if not any('未注册' in e or '无锚点' in e for e in errs) else '见错误清单'}"
          f"（池 {len(pool)} 锚）")
    print(f"[kb-lint] ⑤ 消费可追溯 v2: retire 态佐证 {'OK' if not any('不可追溯' in e for e in errs) else '缺'}；"
          f"口径 endorsement.mode={mode}（切档＝改键值零代码，判据 params.json#memory.endorsement）；"
          f"有效消费锚点 {len(consumed_v2)}（跨人强佐证 {len(consumed_v2) - len(weak_anchors)}／"
          f"弱佐证 {len(weak_anchors)}{'：' + '、'.join(weak_anchors) if weak_anchors else ''}）；"
          f"前M2无actor历史行 {legacy_rows} 条＝弱佐证诚实标注不洗白；"
          + (f"待首轮抽检 {len(pending)} 条（candidate 无记录＝合法初始态，非错）" if pending else "无待抽检条目"))
    print(f"[kb-lint] ⑥ 记忆区健康: {'OK' if not any('路由表缺失' in e for e in errs) else '缺路由表'}"
          f"（调用判据表 {mem['routing_table']}＝路由权威，判据 params.json#memory.routing_table）")
    print(f"[kb-lint] ⑦ 目录纪律: {'OK' if not any('两级封顶' in e for e in errs) else '见错误清单'}"
          f"（两级封顶＝层/二级子目录/文件，结构纪律非大小门；条目 {len(actual)} 件——观测非门）")
    print(f"[kb-lint] ⑧ 隐私值域: {'OK' if not any('隐私值' in e for e in errs) else '见错误清单'}"
          f"（值域 {list(privacy_values)}，缺省 {privacy_default}，判据 params.json#memory.privacy_values；"
          f"archived={len(archived_note)} 件＝人工判旧态，召回默认跳过——行为协议非机检）")

    # ⑨ 双循环咬合（P4，判据 params.json#loop）：文档结构机检（挂载在位）＋内环留痕重放。
    # 诚实边界：事件级「双回流两件在位」是流程行为面，机检锁的是出口判据挂载与数据链完整；
    # 事件本身由 WAL 行＋修正卡（项目侧台账）佐证，kb-lint 不替你做事。
    lp = conf["loop"]
    el = _ROOT / lp["experience_loop"]
    el_text = el.read_text(encoding="utf-8") if el.exists() else ""
    if not el.exists():
        errs.append(f"双循环机制件缺失:{lp['experience_loop']}（kb-lint ⑨；判据 params.json#loop.experience_loop）")
    else:
        for need, why in (("双回流", "咬合协议"), ("skill-creator", "姊妹边界"), ("skill-improver", "姊妹边界")):
            if need not in el_text:
                errs.append(f"机制件缺「{need}」{why}声明:{lp['experience_loop']}（kb-lint ⑨）")
    hook_bad = []
    for h in lp["backflow_hooks"]:
        hp = _ROOT / h
        if not hp.exists():
            hook_bad.append(f"{h}(缺文件)")
            continue
        ht = hp.read_text(encoding="utf-8")
        if "双回流" not in ht or "experience-loop" not in ht:
            hook_bad.append(f"{h}(出口判据未挂双回流/未引机制件)")
    if hook_bad:
        errs.append(f"双回流出口判据挂载断裂:{'；'.join(hook_bad)}（判据 params.json#loop.backflow_hooks）")
    pt = _ROOT / lp["profile_template"]
    pa = _ROOT / lp["profile_anchor_doc"]
    if not pt.exists():
        errs.append(f"档案模板缺失:{lp['profile_template']}（kb-lint ⑨；判据 params.json#loop.profile_template）")
    if not (pa.exists() and lp["profile_template"] in pa.read_text(encoding="utf-8")):
        errs.append(f"接入协议未引用档案模板:{lp['profile_anchor_doc']}（判据 params.json#loop.profile_anchor_doc）")

    def _jl(p):
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    wal_rows = _jl(_ROOT / "data" / "wal.jsonl")
    n_prom = sum(1 for r in wal_rows if r.get("action") == "promoted")
    ids_seen = {r.get("id") for r in wal_rows if r.get("id") is not None}
    dangling = sorted({i for r in wal_rows if r.get("action") == "promoted"
                       for i in (r.get("ids") or []) if i not in ids_seen})
    if dangling:
        errs.append(f"promoted 行引用悬空 id {dangling}（内环留痕复算断裂，kb-lint ⑨）")
    # 修正链复算（锁定批·甲 D-a）：chain 是**机器可读**字段，故悬空/倒指＝复算断裂＝红；
    # 跨锚点链＝「同根复发换了锚点」的信号（聚类键 (type,clause) 看不见，阈值永远数不到）→ 计数公示（观测）。
    chain_bad, chain_cross, n_chain = _wal_chain_scan(wal_rows)
    if chain_bad:
        errs.append(f"修正链悬空/倒指 {chain_bad}（chain 是机器可读字段：父行须先入档且 id 更早，kb-lint ⑨）")
    n_rec = sum(1 for r in _jl(_ROOT / "data" / "probe.jsonl") if r.get("action") == "record")
    n_score = len(_jl(_ROOT / "data" / "scores.jsonl"))
    print(f"[kb-lint] ⑨ 双循环咬合: {'OK' if not any('双循环' in e or '双回流' in e or '档案模板' in e or '接入协议' in e or '复算断裂' in e or '修正链' in e for e in errs) else '见错误清单'}"
          f"（机制件 {lp['experience_loop']}｜出口挂载 {len(lp['backflow_hooks'])} 件｜"
          f"内环重放：WAL {len(wal_rows)} 行→晋升 {n_prom} 批→条目 {len(actual)} 件｜消费 {n_rec} 行→评分 {n_score} 行；"
          f"修正链 {n_chain} 条（跨锚点 {len(chain_cross)}＝复发换锚点信号，观测非门）；"
          f"全循环留痕＝写入→消费→评分→晋升/退役，判据 params.json#loop）")

    # ⑩ 个人层咬合（M3-lite，判据 params.json#memory.personal_layers）：active 层目录在盘、
    # 条目注册进 INDEX＋元数据/溯源/锚点在池/隐私值域（同 ③④⑧ 口径）；pending 层偷建
    # 目录＝红（「顺手就建」＝膨胀，减法纪律机检化；其余层待真实痛点过扩展四问再激活）。
    pl = mem["personal_layers"]
    act_layers, pend_layers = pl["active"], pl["pending"]
    n_pl_files = 0
    for layer in act_layers:
        ldir = _ROOT / layer
        if not ldir.is_dir():
            errs.append(f"个人层 active 但目录缺失:{layer}/（kb-lint ⑩；判据 params.json#memory.personal_layers.active）")
            continue
        pfiles = sorted(f"{layer}/{p.relative_to(ldir).as_posix()}" for p in ldir.rglob("*.md"))
        n_pl_files += len(pfiles)
        if not pfiles:
            errs.append(f"个人层 {layer}/ 无条目（active 层须有真实条目，零则诚实降级，kb-lint ⑩）")
        deep = [f"{layer}/{p.relative_to(ldir).as_posix()}"
                for p in ldir.rglob("*.md") if len(p.relative_to(ldir).parts) > 1]
        if deep:
            errs.append(f"三级目录违反两级封顶:{deep}（层/二级子目录/文件，kb-lint ⑩）")
        for rel in pfiles:
            if rel not in text:
                errs.append(f"个人层条目未注册:{rel}（应入 INDEX 个人层区段，kb-lint ⑩）")
            head = (_ROOT / rel).read_text(encoding="utf-8")
            miss = [k for k in meta_keys
                    if not re.search(rf"^\s*>?\s*{re.escape(k)}\s*[（(：:]", head, re.M)]
            if miss:
                errs.append(f"{rel}: 缺元数据 {'、'.join(miss)}（kb-lint ⑩ 同 ③ 口径）")
            m_src = re.search(r"^\s*>?\s*溯源\s*[（(：:](.*)$", head, re.M)
            if m_src and len(m_src.group(1).strip().strip("`* ")) < 4:
                errs.append(f"{rel}: 溯源非空校验失败（个人层条目须指认真实会话/拍板来源，AI 不代笔预填，kb-lint ⑩）")
            anchors = _entry_anchors(head)
            unreg = [t for t in anchors if t not in pool]
            if unreg:
                errs.append(f"{rel}: 锚点未注册进抽检池 {unreg}（kb-lint ⑩ 同 ④ 口径）")
            m_pv = re.search(r"^\s*>?\s*隐私\s*(?:[（(][^）)]*[）)])?\s*[：:](.*)$", head, re.M)
            if m_pv:
                pv = m_pv.group(1).strip().strip("`* ")
                if pv and pv not in privacy_values:
                    errs.append(f"{rel}: 隐私值 {pv!r} 不在值域 {list(privacy_values)}（kb-lint ⑩ 同 ⑧ 口径）")
    built = [layer for layer in pend_layers if (_ROOT / layer).exists()]
    if built:
        errs.append(f"pending 个人层已偷建目录 {built}（待真实痛点过扩展四问后移 active 再建，"
                    "kb-lint ⑩；判据 params.json#memory.personal_layers.pending）")
    print(f"[kb-lint] ⑩ 个人层咬合: {'OK' if not any('kb-lint ⑩' in e for e in errs) else '见错误清单'}"
          f"（active {len(act_layers)} 层 {n_pl_files} 件｜pending {len(pend_layers)} 层未建＝诚实空态；"
          f"判据 params.json#memory.personal_layers，M3-lite B 案）")
    for e in errs:
        print(f"  ! {e}")
    if not errs:
        print(f"[kb-lint] 聚鲸协作伙伴已就绪：条目 {len(actual)}／锚 {len(pool)}／已登记 actor {len(actors_reg)}／kb-lint 全绿"
              f"（三步接入完成，判据 routing.md §七；自检＝probe_usage.py size＋kb-lint）")
    return 1 if errs else 0


def _pitfalls_scan(files):
    """陷阱引用存在性（P2 验收整改，P3 前置清障）：文档引用 pitfalls #N／陷阱 #N，
    号必须在 pitfalls.md 表中真实存在——本库引用密度最高的一类链接此前全靠人眼复核。
    收窄（锁定批·甲）：同一行常同时出现 **WAL 行号**与陷阱号（如「WAL #42 触发陷阱 #19」），
    故仅当 `#N` 紧邻前文含「WAL」（5 字符窗）时按台账行号放过——否则台账行号会被误判成悬空陷阱号
    （真事故：evolution.md #30 登记卡因引 WAL 行号触发 9 处假红）。"""
    pf = _ROOT / "references" / "pitfalls.md"
    known = set()
    if pf.exists():
        for line in pf.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*(\d+)\s*\|", line)
            if m:
                known.add(int(m.group(1)))
    bad = []
    for f in files:
        if f.resolve() == pf.resolve():
            continue
        rel = f.relative_to(_ROOT).as_posix()
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "pitfalls" not in line.lower() and "陷阱 #" not in line and "陷阱#" not in line:
                continue
            for m in re.finditer(r"#\s*(\d+)", line):
                n = m.group(1)
                if "WAL" in line[max(0, m.start() - 5):m.start()]:
                    continue                     # 「WAL #NN」＝台账行号，不是陷阱号（收窄见 docstring）
                if known and int(n) not in known:
                    bad.append((rel, i, n))
    return bad


def cmd_size(a):
    """加载预算机检：判据读 assets/params.json#budget（修 B6「预算表自报」）；
    同处校对锚点一致性（修 B4）、数值漂移与死引用。数值与清单都不写死在代码里。"""
    conf = params()
    # 自检（防「校验函数被改回静默兜底」＝机检自己变假门）：故意喂缺键配置，必须报错才算通过
    try:
        _req({"probe": {}}, ("probe.sample_n",), "自检")
        selfcheck = False
    except SystemExit:
        selfcheck = True
    print(f"[size] 缺必填键自检: {'通过（缺键必报，无静默兜底）' if selfcheck else '未通过 —— 校验被写成兜底，配置缺失会假绿'}")
    # 「仅文档引用」＝整节无代码消费；写成「scripts/... ；某键仅文档引用」的节算部分机检，不计入
    idle = [k for k, v in conf["consumers"].items() if not k.startswith("_") and v.startswith("仅文档")]
    print(f"[size] 消费声明: {len(conf['consumers']) - 1} 节已声明，其中 {len(idle)} 节**仅文档引用、非机检**"
          f"（{'、'.join(idle)}）—— 不得据此声称已机检")
    # 配置节全覆盖（兑现 consumers._说明 的声明）：漏声明的节＝没写消费者的死配置，机检直接非零
    sections = [k for k in conf if not k.startswith("_") and k != "consumers"]
    undeclared = [k for k in sections if k not in conf["consumers"]]
    print(f"[size] 配置节覆盖: {len(sections) - len(undeclared)}/{len(sections)} 节有消费者声明"
          + (f"；未声明 {len(undeclared)} 节：{'、'.join(undeclared)}（无消费者的配置节＝无人读的死配置）"
             if undeclared else ""))
    # 覆盖 n/n 只说明「写了」；声明是否与真实消费对得上，由可落实校验判（防恒绿死判据）
    consumer_hits = _consumers_scan(conf)
    cfg = conf["budget"]
    default, rules = cfg["default"], cfg["by_glob"]
    roots = tuple(cfg["scan_roots"])
    files = [p for p in _ROOT.rglob("*.md")
             if p.relative_to(_ROOT).parts[0] in roots]
    files += [_ROOT / s for s in cfg["scan_files"]]
    files = sorted({f for f in files if f.exists()})
    over, unmet, total = [], [], 0.0
    print("[size] 加载预算观测（KB，软目标观测线来自 assets/params.json#budget；"
          "铁律·不设硬上限：超线只提示，不进退出码）")
    for f in files:
        rel = f.relative_to(_ROOT).as_posix()
        kb = f.stat().st_size / 1024
        total += kb
        spec = dict(default)
        for pat, s in rules.items():
            if fnmatch.fnmatch(rel, pat):
                spec.update(s)
        limit, goal, layer = spec.get("limit_kb"), spec.get("target_kb"), spec.get("layer", "")
        mark = ""
        if limit is not None and kb > limit:
            mark = f"  ~ 超观测线({limit}，非门)"
            over.append(rel)
        elif goal and kb > goal:
            mark = f"  ! 超参考目标({goal})"
            unmet.append(rel)
        print(f"  {layer:<5} {rel:<38} {kb:>6.2f}  线{limit}{mark}")
    print(f"[size] 全量合计 {total:.2f} KB（上界，不等于单次会话量）")
    print(f"[size] 超观测线 {len(over)} 件（铁律：大小＝观测值非门，不阻塞、不构成债）"
          + (f"：{'、'.join(over)}" if over else ""))
    print(f"[size] 超参考目标 {len(unmet)} 件（仅提示）" + (f"：{'、'.join(unmet)}" if unmet else ""))
    # 会话口径：可复现（取代人工估算）
    if getattr(a, "session", None):
        picked = [s.strip() for s in a.session.split(",") if s.strip()]
        missing = [s for s in picked if not (_ROOT / s).exists()]
        ses = sum((_ROOT / s).stat().st_size for s in picked if (_ROOT / s).exists()) / 1024
        cap = cfg["session"]["limit_kb"]
        print(f"[size] 会话加载 {len(picked) - len(missing)} 件 = {ses:.2f} KB（观测线 {cap}）→ "
              + ("OK" if ses <= cap else "超观测线（非门：按减法顺序自查取舍，不阻塞）")
              + (f"；缺文件 {missing}" if missing else ""))
    # 锚点一致性：文档「声明」必须 ⊆ 抽检池（缺一即报，不靠人记）
    pool = set(clauses())
    declared = {}
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if "锚点" in line and "｜" in line:
                for tok in line.split("：", 1)[-1].split("｜"):
                    tok = tok.strip().strip("`* ")
                    if tok:
                        declared.setdefault(tok, f.relative_to(_ROOT).as_posix())
    unreg = {t: s for t, s in declared.items() if t not in pool}
    print(f"[size] 锚点声明 {len(declared)} 个 / 抽检池 {len(pool)} 个 → "
          + ("一致" if not unreg else
             "未注册 " + str(len(unreg)) + " 个: "
             + "、".join(f"{t}({s})" for t, s in unreg.items())))
    drift_hits = _drift_scan(conf, files)
    key_hits = _key_ref_scan(conf, files)
    pit_hits = _pitfalls_scan(files)
    print("[size] 陷阱引用存在性: "
          + (f"{len(pit_hits)} 处指向不存在的陷阱号 —— "
             + "、".join(f"{p}:{ln}#{n}" for p, ln, n in pit_hits)
             if pit_hits else "全部 pitfalls #N 引用真实存在"))
    # 铁律·不设硬上限（2026-09-27 用户令，evolution.md#18）：大小类（over/unmet/会话超线）
    # 只提示，绝不进退出码——字数门曾把 P1 两轮验收拖进「压文件大小」死循环。
    # 非零只留给真防自欺项：锚点未注册／漂移／悬空参数键／消费声明不可落实。
    return 1 if (unreg or drift_hits or key_hits or pit_hits or undeclared or consumer_hits
                 or not selfcheck) else 0


def cmd_pool_check(a):
    """待发版池机检（v2.2.0·D8 三层分离：机制件在 skill 一份，实例池在产品每仓一份）。
    扫产品侧常驻池文件（--pool 路径；缺省＝当前工作目录/instance_filename）：
    ① 模板同构（表头必含 ID/状态/日期列）② 状态值域（statuses）③ 观察滞留提示
    ④ 发版触发核算（adopted≥adopt_trigger_n／距上批≥stale_days，引用键名不打死数值）。
    格式红＝退出 1；触发/滞留提示只打印不进退出码——攒批时机由人按 blocker_definition 裁决，
    机检不替人发版（防「机检说能发」变成「机检逼着发」）。"""
    conf = params()
    _req(conf, ("release_pool",), "assets/params.json")
    rp = conf["release_pool"]
    _req(rp, ("template", "instance_filename", "statuses", "adopt_trigger_n",
              "stale_days", "observe_window_rounds"), "assets/params.json#release_pool")
    tmpl = _ROOT / rp["template"]
    if not tmpl.exists():
        print(f"[pool] 红：模板缺失 {tmpl}（判据 params.json#release_pool.template）")
        return 1
    pool = pathlib.Path(a.pool) if a.pool else pathlib.Path.cwd() / rp["instance_filename"]
    if not pool.exists():
        print(f"[pool] 红：实例池不存在 {pool}——routing §六 第 2 步建档时应随建档初始化"
              f"（实例化自 {rp['template']}）；缺池＝产品侧待发版项无台账")
        return 1
    lines = pool.read_text(encoding="utf-8").splitlines()
    hdr_i = next((i for i, l in enumerate(lines) if re.match(r"^\|\s*ID\s*\|", l)), None)
    if hdr_i is None or hdr_i + 1 >= len(lines):
        print("[pool] 红：找不到条目表表头（须以 `| ID |` 开头的行）——模板同构破坏")
        return 1
    cols = [c.strip() for c in lines[hdr_i].strip().strip("|").split("|")]
    need = ("ID", "状态", "日期")
    miss = [c for c in need if c not in cols]
    if miss:
        print(f"[pool] 红：表头缺列 {miss}（模板同构＝列结构固定，判据 assets/pending-releases.template.md）")
        return 1
    i_status, i_date = cols.index("状态"), cols.index("日期")
    rows, bad = [], []
    for l in lines[hdr_i + 2:]:
        if not l.strip().startswith("|"):
            break
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if len(cells) < len(cols):
            bad.append(l[:60])
            continue
        rows.append(cells)
    if bad:
        print(f"[pool] 红：{len(bad)} 行列数不足（模板同构破坏），首行: {bad[0]}")
        return 1
    statuses = set(rp["statuses"])
    red = [(c[0], c[i_status]) for c in rows if c[i_status] not in statuses]
    if red:
        print(f"[pool] 红：状态值越域（值域={sorted(statuses)}）: {red}")
        return 1
    today = datetime.date.today()
    stale_obs, adopted, last_ship = [], 0, None
    for c in rows:
        try:
            d = datetime.date.fromisoformat(c[i_date])
        except ValueError:
            print(f"[pool] 红：日期格式须 YYYY-MM-DD（ID={c[0]} 实为「{c[i_date]}」）")
            return 1
        if c[i_status] == "observing":
            stale_obs.append((c[0], (today - d).days))
        elif c[i_status] == "adopted":
            adopted += 1
        elif c[i_status] == "shipped":
            last_ship = d if last_ship is None else max(last_ship, d)
    print(f"[pool] 池: {pool} ｜ 条目 {len(rows)}（adopted={adopted} observing={len(stale_obs)}）")
    for pid, days in stale_obs:
        print(f"[pool] 提示: {pid} 观察滞留 {days} 天（observe_window_rounds={rp['observe_window_rounds']} 轮）——"
              "复发→adopted；超窗未复发→expired（人工裁决，池行留痕）")
    if adopted >= rp["adopt_trigger_n"]:
        print(f"[pool] 触发①: adopted {adopted} ≥ adopt_trigger_n {rp['adopt_trigger_n']} → "
              "打包走扩展四问＋棘轮发版")
    if last_ship and (today - last_ship).days >= rp["stale_days"] and adopted > 0:
        print(f"[pool] 触发③: 距上批 {(today - last_ship).days} 天 ≥ stale_days {rp['stale_days']} 且池内有 adopted → 打包发版")
    if last_ship:
        print(f"[pool] 最近发版: {last_ship.isoformat()}")
    print("[pool] 绿：实例池格式合规（同构/值域/日期）")
    return 0


def cmd_version(a):
    """skill 自身版本指纹三处对照（⑥版本管理判据用于本 skill，2026-09-28 产品负责人令）：
    data/VERSION（账面）＝ SKILL.md H1 标题 ＝ SKILL.md frontmatter description（调用入口指纹，
    IDE 加载列表可见——会话内凭 attached description 的 v 号直接核对是否最新）。
    description 行允许整体引号包裹（2026-10-06 批：WAL #50 跨宿主兼容**要求**引号——严格 YAML 宿主
    因裸冒号拒载整技能，故指纹正则必须兼容引号，防两机检互斥）。
    三处不一致或缺失＝红退出 1；一致＝绿并顺带观测分发件 zip（大小/mtime，存在才列，
    不作一致性门——分发时效属人工部署环节，「改了≠部署了」由人对照观测行判断）。"""
    book = _ROOT / "data" / "VERSION"
    if not book.exists():
        print("[version] 红：data/VERSION 不存在（账面缺失）")
        return 1
    ver = book.read_text(encoding="utf-8").strip()
    skill_md = _ROOT / "SKILL.md"
    if not skill_md.exists():
        print("[version] 红：SKILL.md 不存在")
        return 1
    text = skill_md.read_text(encoding="utf-8")
    h1 = re.search(r"^# 聚鲸 Agent 工作流 v(\d+(?:\.\d+)*)", text, re.M)
    desc = re.search(r'^description: ["\']?聚鲸 agent 工作流 v(\d+(?:\.\d+)*)', text, re.M)
    spots = [("data/VERSION（账面）", ver),
             ("SKILL.md H1 标题", h1.group(1) if h1 else None),
             ("frontmatter description（调用入口）", desc.group(1) if desc else None)]
    missing = [n for n, v in spots if not v]
    for n, v in spots:
        print(f"[version] {n}: {v if v else '（缺失）'}")
    if missing:
        print(f"[version] 红：{len(missing)} 处指纹缺失——升版本「三处同改」纪律被跳过")
        return 1
    if len({v for _, v in spots}) > 1:
        print("[version] 红：三处指纹不一致 ｜ " + " ｜ ".join(f"{n}={v}" for n, v in spots))
        return 1
    zp = _ROOT.parent / "jujing-agent-workflow.zip"
    if zp.exists():
        mt = datetime.datetime.fromtimestamp(zp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"[version] 分发件 zip 观测: {zp.stat().st_size} bytes @ {mt}（仅观测，部署时效人工对照）")
    else:
        print("[version] 分发件 zip 未在旁（skills/ 根无 zip），观测跳过")
    print(f"[version] 绿：聚鲸 skill 版本指纹三处一致 = v{ver}")
    return 0


def _wal_chain_scan(wal_rows):
    """修正链复算（纯函数＝可复算，selfcheck 负测试直接喂合成行；锁定批·甲 D-a）。
    返回 (悬空/倒指清单, 跨锚点清单, 链边数)。
    - 悬空：父 id 不存在；倒指：父 id ≥ 本行 id（append-only 下 id 单调递增，自指/倒指＝链断或成环）。
    - 跨锚点：父子 clause 不同——「同根复发换了锚点」，聚类键 (type,clause) 看不见，故单独计数公示。
    文本里写「订正 #42」只是引用事实、机器校验不到；chain 字段才是留痕。② 者勿混读。"""
    by_id = {r["id"]: r for r in wal_rows if r.get("id") is not None}
    bad, cross, n = [], [], 0
    for r in wal_rows:
        rid = r.get("id")
        for c in (r.get("chain") or []):
            n += 1
            tgt = by_id.get(c)
            if tgt is None or rid is None or c >= rid:
                bad.append(f"#{rid}->#{c}")
            elif tgt.get("clause") != r.get("clause"):
                cross.append(f"#{rid}({r.get('clause')})->#{c}({tgt.get('clause')})")
    return bad, cross, n


def _fm_colon_scan(text):
    """SKILL.md frontmatter 裸冒号扫描（纯函数＝可复算，selfcheck 负测试直接喂合成文本；
    判据＝WAL #50 跨宿主兼容）。返回坏行清单 [(行号, 行截断)]：
    frontmatter 内 `key: value` 行的值段未加引号/块标记而含半角「: 」。
    背景：严格 YAML 宿主（如 Trae）会因裸冒号解析失败→技能整个不注册；
    CodeBuddy 宽松解析故本地从未暴露（公理 5 证据形态：宽松宿主的绿＝跨平台合法性的伪证）。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return []
    bad = []
    for i in range(1, len(lines)):
        ln = lines[i]
        if ln.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_-]+):[ \t]+(.*)$", ln)
        if not m:
            continue
        val = m.group(2).strip()
        if val[:1] in "\"'":
            if len(val) < 2 or val[-1] != val[0]:
                bad.append((i + 1, ln[:60]))   # 引号未闭合＝严格 YAML 同样炸，不得豁免
                continue
            continue                      # 整体引号包裹（含闭合）＝安全
        if val in ("|", "|-", "|+", ">", ">-", ">+"):
            continue                      # 块写法＝安全
        if ": " in val:
            bad.append((i + 1, ln[:60]))
    return bad


def _scan_surface(conf):
    """措辞观测扫描面：budget.scan_roots 下 *.md ＋ budget.scan_files ＋ wording.scan_extra 的 glob。
    与 size 的文档面同源（budget），另加 scan_extra —— 代码件（docstring/注释）里的绝对化断言
    同属「对外表述」（WAL #43 的邻处漏改正是 probe_usage docstring），不纳入＝留盲区。"""
    b = conf["budget"]
    roots = tuple(b["scan_roots"])
    files = [p for p in _ROOT.rglob("*.md") if p.relative_to(_ROOT).parts[0] in roots]
    files += [_ROOT / s for s in b["scan_files"]]
    for g in conf["wording"]["scan_extra"]:
        files += list(_ROOT.glob(g))
    return sorted({f for f in files if f.is_file()})


def _wording_scan(conf, files):
    """措辞绝对化扫描（纯函数＝可复算，selfcheck 负测试直接喂临时件；判据 assets/params.json#wording）。
    返回 (未带例外清单, 同行已列例外清单, 跳过计数)；条目＝(相对路径, 行号, 命中词, 原文)。
    **只报不判**：本函数不做失实判定、无退出码语义（判定留给人——公理 1）。
    跳过范围＝代码块内／exempt_marker 豁免行／Markdown 台账表行（行首 |＝历史登记与评分溯源）。"""
    w = conf["wording"]
    words, excls = w["absolute_words"], w["exception_markers"]
    exempt = conf["drift"]["exempt_marker"]
    miss_exc, has_exc = [], []
    skip = collections.Counter()
    for f in files:
        rel = f.relative_to(_ROOT).as_posix() if f.is_relative_to(_ROOT) else f.as_posix()
        in_code = False
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "```" in line:
                in_code = not in_code
                skip["代码块"] += 1
                continue
            if in_code:
                skip["代码块"] += 1
                continue
            if exempt in line:
                skip["豁免行"] += 1
                continue
            if line.lstrip().startswith("|"):
                skip["台账表行"] += 1
                continue
            hits = [x for x in words if x in line]
            if hits:
                rec = (rel, i, "、".join(hits), line.strip())
                (has_exc if any(e in line for e in excls) else miss_exc).append(rec)
    return miss_exc, has_exc, skip


def cmd_wording(a):
    """措辞绝对化观测（锁定批·甲 ②-B；判据 assets/params.json#wording）——**观测非门**：
    列清单、不判罪、常态恒 0 退出、不进任何机检码（与 heat 同构）。
    原提案＝把「绝对化措辞未列例外」做成机检门：实测命中 95 行次、其中 77 处未带例外，
    主体是**合法用法**（「永远红」＝陷阱描述、「一切判据值只存 params.json」＝铁律、
    「恒 ImagePullBackOff」＝状态描述、「唯一入口」＝正当限定）⇒ 判定面过宽 ⇒ 永远红 ⇒ 被绕过（陷阱 #19）；
    且「这句措辞是否失实」属**判断**（公理 1：是事实就机检，是判断才留给人）⇒ 机器只把清单端到眼前。"""
    conf = params()
    files = []
    if a.files:
        for s in a.files.split(","):
            s = s.strip()
            if not s:
                continue
            p = pathlib.Path(s)
            p = p if p.is_absolute() else (_ROOT / p)
            if not p.is_file():
                sys.exit(f"[probe] --files 指定件不存在: {s}（相对 skill 根；负测试可指向扫描面外文件）")
            files.append(p)
    else:
        files = _scan_surface(conf)
        if a.since:
            try:
                since = datetime.datetime.strptime(a.since, "%Y-%m-%d").date()
            except ValueError:
                sys.exit("[probe] --since 只收 YYYY-MM-DD（缺省＝全树只报）")
            files = [f for f in files if datetime.date.fromtimestamp(f.stat().st_mtime) >= since]
        elif conf["wording"]["scope"] == "changed":
            print("[probe] 提示: wording.scope=changed 口径须用 --since <YYYY-MM-DD> 圈定「本次改动件」"
                  "（**文件级 mtime，非行级**）；未给 --since ⇒ 退化为全树只报（噪声大但不会漏）")
    miss_exc, has_exc, skip = _wording_scan(conf, files)
    print(f"[probe] 措辞绝对化观测（**观测非门**：常态恒 0 退出、不进任何机检码；"
          f"判据 assets/params.json#wording）: 扫描 {len(files)} 件")
    print(f"  未带例外 {len(miss_exc)} 处 / 同行已列例外 {len(has_exc)} 处；跳过 "
          + ("、".join(f"{k} {v} 行" for k, v in sorted(skip.items())) or "无"))
    for rel, i, w, txt in miss_exc:
        print(f"    {rel}:{i} | 命中「{w}」 | {txt[:96]}")
    if a.show_exc:
        for rel, i, w, txt in has_exc:
            print(f"    [已列例外] {rel}:{i} | 命中「{w}」 | {txt[:96]}")
    print("  读法：纪律描述／铁律／状态描述属合法用法，可直接放过；对外**事实断言**建议改写为"
          "「常态…（例外：…）」或挂 params-exempt:理由——是否失实是人的判断（公理 1）。")
    return 0


def cmd_selfcheck(a):
    """机检断言自检（锁定批·甲 ②-A「断言机检化」；判据 assets/params.json 各节）：
    把本 skill 对外**绝对化的事实断言**从文档搬进**可跑的复算**——每条断言＝一个隔离负测试，
    逐条打印「断言 → 期望 → 实测 → OK/FAIL」，任一 FAIL 即红退出 1。
    文档只留判据名＋证据出处（出处＝本子命令：跑一次即复算，不再靠人复述）。
    隔离：wal 侧走 JUJING_DATA_DIR 临时库，跑完即删——**真实台账零污染**。"""
    conf = params()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="jujing-selfcheck-"))
    env = dict(os.environ, JUJING_DATA_DIR=str(tmp))
    checks = []

    def chk(name, ok, detail):
        checks.append((name, ok))
        print(f"  [{'OK' if ok else 'FAIL'}] {name} — {detail}")

    def wal(*args):
        return subprocess.run([sys.executable, str(_ROOT / "scripts" / "wal.py"), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(_ROOT), env=env)

    def rub(*args):
        return subprocess.run([sys.executable, str(_ROOT / "scripts" / "rubric_score.py"), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(_ROOT), env=env)

    def seed(rows):
        """隔离库 scores.jsonl 整体重写（棘轮分支测试用合成行，跑完随 tmp 删）。"""
        (tmp / "scores.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    _dims = tuple(conf["rubric"]["dims"])
    _s_full = ["%s:7" % d for d in _dims]

    def _row(ver, avg_v, **kw):
        r = {"version": ver, "ts": "2026-01-02T00:00:00", "scores": {}, "avg": avg_v,
             "note": "t", "by": "judge", "author": "builder", "batch": "t"}
        r.update(kw)
        return r

    try:
        # ① 缺必填键必报（无兜底默认值＝无隐藏硬编码）
        try:
            _req({}, ("x",), "selfcheck")
            chk("① 缺必填键必报（无静默兜底）", False, "缺键竟未报错")
        except SystemExit:
            chk("① 缺必填键必报（无静默兜底）", True, "_req({}, ('x',)) → SystemExit")

        # ② 修正链复算：悬空/倒指必抓、跨锚点必计数
        rows = [{"id": 1, "clause": "A"}, {"id": 2, "clause": "A", "chain": [1]},
                {"id": 3, "clause": "B", "chain": [1]}, {"id": 4, "clause": "A", "chain": [99]},
                {"id": 5, "clause": "A", "chain": [5]}]
        bad, cross, n = _wal_chain_scan(rows)
        chk("② 修正链悬空/倒指必抓、跨锚点必计数",
            bad == ["#4->#99", "#5->#5"] and cross == ["#3(B)->#1(A)"] and n == 4,
            f"悬空/倒指={bad} 跨锚点={cross} 链边={n}")

        # ③ 措辞扫描：无例外必报、带例外不报、代码块跳过
        f_neg = tmp / "neg.md"
        f_pos = tmp / "pos.md"
        f_neg.write_text("这一版恒 0 退出\n\n```\n恒 x\n```\n", encoding="utf-8")
        f_pos.write_text("这一版常态恒 0 退出（例外：配置错误一类）\n", encoding="utf-8")
        miss, has, skip = _wording_scan(conf, [f_neg, f_pos])
        chk("③ 措辞观测：无例外必报／带例外不报／代码块跳过",
            len(miss) == 1 and len(has) == 1 and skip.get("代码块", 0) == 3,
            f"未带例外={len(miss)} 已列例外={len(has)} 跳过={dict(skip)}")

        # ④⑤⑥ wal 侧负测试（临时库）
        p = wal("add", "--source", "ai", "--type", "lesson", "--clause", "纠偏WAL",
                "--actor", "kf41", "--text", "selfcheck 合成行（临时库，跑完即删）", "--chain", "99")
        chk("④ add --chain 悬空必拒（修正链可复算）",
            p.returncode != 0 and "未知 id" in (p.stdout + p.stderr),
            f"rc={p.returncode} out={(p.stdout + p.stderr).strip().splitlines()[-1][:70] if (p.stdout + p.stderr).strip() else ''}")

        p = wal("add", "--source", "ai", "--type", "lesson", "--clause", "不存在的大类锚点XX",
                "--actor", "kf41", "--text", "selfcheck 合成行")
        chk("⑤ 池外 clause 必拒（悬空锚点＝死台账）",
            p.returncode != 0 and "锚点" in (p.stdout + p.stderr),
            f"rc={p.returncode}")

        p = wal("add", "--source", "ai", "--type", "lesson", "--clause", "纠偏WAL",
                "--actor", "nobody-xyz", "--text", "selfcheck 合成行")
        chk("⑥ 未登记 actor 必拒（M2 准入）",
            p.returncode != 0 and "未登记" in (p.stdout + p.stderr),
            f"rc={p.returncode}")

        p = wal("add", "--source", "ai", "--type", "lesson", "--clause", "纠偏WAL",
                "--actor", "kf41", "--text", "selfcheck 合成行（合规）")
        rid = None
        mt = re.search(r"已记录 #(\d+)", p.stdout)
        rid = mt.group(1) if mt else None
        chk("⑦ 合规 add 入档并回显 id", p.returncode == 0 and rid is not None, f"id=#{rid}")
        if rid:
            p = wal("mark-promoted", "--ids", rid, "--to", "v2.2.0")
            chk("⑧ mark-promoted 缺 --cohesion 必拒（D-b：判断须留痕）",
                p.returncode != 0 and "cohesion" in (p.stdout + p.stderr),
                f"rc={p.returncode}")
            p = wal("mark-promoted", "--ids", rid, "--to", "v2.2.0", "--cohesion", "太短")
            chk("⑨ 过短 cohesion 必拒（长度下限＝params#wal.cohesion_min_len）",
                p.returncode != 0, f"rc={p.returncode}")
            p = wal("promote-check", "--digest", "--ids", rid)
            missing_q = [q[:12] for q in conf["wal"]["cohesion_questions"] if q not in p.stdout]
            chk("⑩ digest 强制打印同类性三问（缺＝材料不完整）",
                not missing_q and "同类性三问" in p.stdout, f"缺={missing_q}")

        # ⑪ heat 观测非门：空库恒 0（非 0 仅配置错误一类）
        p = subprocess.run([sys.executable, str(_ROOT / "scripts" / "probe_usage.py"), "heat"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(_ROOT), env=env)
        chk("⑪ heat 空库恒 0（观测非门不判罪）", p.returncode == 0, f"rc={p.returncode}")

        # ⑫ 全树文档面机检（悬空锚点/漂移/死件/消费声明）零红
        p = subprocess.run([sys.executable, str(_ROOT / "scripts" / "probe_usage.py"), "size"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(_ROOT))
        chk("⑫ size 全树面零红（判据 params#budget·#drift）", p.returncode == 0, f"rc={p.returncode}")

        # ⑬ frontmatter 裸冒号门（WAL #50：严格 YAML 宿主因裸冒号拒载整技能；改坏必须红）
        neg_fm = "---\nname: x\ndescription: bad Use when: colon\n---\nbody\n"
        neg_fm2 = "---\nname: x\ndescription: \"unclosed : x\n---\nbody\n"
        pos_fm = "---\nname: x\ndescription: \"ok Use when: colon\"\n---\nbody\n"
        pos_fm2 = "---\nname: x\ndescription: |\n  ok text\n---\nbody\n"
        tree_fm = _fm_colon_scan((_ROOT / "SKILL.md").read_text(encoding="utf-8"))
        chk("⑬ frontmatter 裸冒号/未闭合引号必抓／引号块写法豁免／真树零红",
            len(_fm_colon_scan(neg_fm)) == 1 and len(_fm_colon_scan(neg_fm2)) == 1
            and not _fm_colon_scan(pos_fm) and not _fm_colon_scan(pos_fm2) and not tree_fm,
            f"neg={len(_fm_colon_scan(neg_fm))} neg_unclosed={len(_fm_colon_scan(neg_fm2))} "
            f"pos={len(_fm_colon_scan(pos_fm))} pos_block={len(_fm_colon_scan(pos_fm2))} tree={len(tree_fm)}")

        # ⑭ 版本指纹三处一致（SKILL §五 三处同改门——升版流程强制门，中间态红＝正确信号）
        p = subprocess.run([sys.executable, str(_ROOT / "scripts" / "probe_usage.py"), "version"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(_ROOT))
        tail = (p.stdout + p.stderr).strip().splitlines()[-1] if (p.stdout + p.stderr).strip() else ""
        chk("⑭ version 三指纹一致零红（SKILL §五；version 子命令真实存在且绿）",
            p.returncode == 0, f"rc={p.returncode} out={tail[:70]}")

        # ⑮-⑱ 棘轮配对四分支（隔离库合成行直接喂 ratchet；#36 A2——配对逻辑实证密度 7→9 路径）
        seed([_row("v1", 7.0, ts="2026-01-01T00:00:00"),
              _row("v2", 8.0, anchor_avg=7.0, anchor_ver="anchor-t")])
        p = rub("ratchet")
        chk("⑮ 配对 ALLOWED：Y>X 且过地板必放行",
            p.returncode == 0 and "ALLOWED(配对)" in p.stdout, f"rc={p.returncode}")

        seed([_row("v1", 7.0, ts="2026-01-01T00:00:00"),
              _row("v2", 7.0, anchor_avg=7.0, anchor_ver="anchor-t")])
        p = rub("ratchet")
        chk("⑯ 配对 LOCKED：Y≤X 必锁（严格超越才放行）",
            p.returncode == 0 and "LOCKED(配对)" in p.stdout and "ALLOWED" not in p.stdout,
            f"rc={p.returncode}")

        seed([_row("v1", 6.0, ts="2026-01-01T00:00:00"),
              _row("v2", 6.5, anchor_avg=6.0, anchor_ver="anchor-t")])
        p = rub("ratchet")
        chk("⑰ 双低防护：Y>X 但低于地板必锁（裁判整体偏松时差值失真）",
            p.returncode == 0 and "LOCKED(配对)" in p.stdout and "地板" in p.stdout,
            f"rc={p.returncode}")

        seed([_row("v1", 8.94, ts="2026-01-01T00:00:00"), _row("v2", 9.5)])
        p = rub("ratchet")
        chk("⑱ 无锚行退回历史最高绝对分（旧行为分支仍活着＝向后兼容）",
            p.returncode == 0 and "ALLOWED" in p.stdout and "配对" not in p.stdout,
            f"rc={p.returncode}")

        # ⑲⑳ 锚分入档门（#36 A1：锚文件存在性校验＋逐维校验＋均分脚本复算）
        p = rub("score", "--version", "t1", "--note", "t", "--by", "j", "--author", "b",
                "--batch", "t", "--s", *_s_full, "--anchor-ver", "anchor-nope",
                "--anchor-s", *_s_full)
        chk("⑲ 锚文件不存在的锚分必拒（防口径凭空捏造）",
            p.returncode != 0 and "锚文件不存在" in (p.stdout + p.stderr), f"rc={p.returncode}")

        (tmp / "calibration").mkdir()
        (tmp / "calibration" / "anchor-t.md").write_text("synthetic", encoding="utf-8")
        p = rub("score", "--version", "t1", "--note", "t", "--by", "j", "--author", "b",
                "--batch", "t", "--s", *_s_full, "--anchor-ver", "anchor-t",
                "--anchor-s", *_s_full)
        last = {}
        try:
            last = json.loads((tmp / "scores.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        except Exception:
            pass
        ad = last.get("anchor_dims") or {}
        chk("⑳ --anchor-s 合规入档：anchor_dims 全维＋anchor_avg 脚本复算",
            p.returncode == 0 and set(ad) == set(_dims)
            and abs(float(last.get("anchor_avg") or 0) - 7.0) < 1e-9,
            f"rc={p.returncode} dims={len(ad)} anchor_avg={last.get('anchor_avg')}")

        # ㉑ 登记行连续（裁决 WAL #5：三指纹一致绿≠登记表连续——当前版本必须在台账有行）
        ver = (_ROOT / "data" / "VERSION").read_text(encoding="utf-8").strip()
        evo_lines = (_ROOT / "references" / "evolution.md").read_text(encoding="utf-8").splitlines()
        i3 = next(i for i, l in enumerate(evo_lines) if l.startswith("## 三、"))
        j4 = next(i for i in range(i3 + 1, len(evo_lines)) if evo_lines[i].startswith("## "))
        led = [l for l in evo_lines[i3:j4] if re.match(r"^\| \d+ \|", l)]
        chk("㉑ 登记行连续：当前 VERSION 在 §三 最新登记行中（版本指纹绿≠台账有行）",
            bool(led) and ver in led[-1],
            f"VERSION={ver} 最新行=#{led[-1][2:].split('|')[0].strip() if led else '无'}")

        # ㉒ 锚组成自洽（anchor-2.3.1 拼接缺陷回归门：声明「N 条止」⇔ 台账节含 |N| 行且无 |N+1| 行）
        probs = []
        anchors = sorted((_ROOT / "data" / "calibration").glob("anchor-*.md"))
        for f in anchors:
            t = f.read_text(encoding="utf-8")
            parts = t.split("# [2/2]")
            if len(parts) < 2:
                probs.append(f.name + ":无台账节")
                continue
            m = re.search(r"组成:.*?（(\d+) 条止）", t)
            if not m:
                probs.append(f.name + ":头部无「N 条止」声明")
                continue
            n = int(m.group(1))
            if not re.search(r"(?m)^\| %d \|" % n, parts[1]):
                probs.append("%s:声明%d条止但缺|%d|行" % (f.name, n, n))
            if re.search(r"(?m)^\| %d \|" % (n + 1), parts[1]):
                probs.append("%s:声明%d条止却含|%d|行" % (f.name, n, n + 1))
        chk("㉒ 锚组成自洽：每锚声明「N 条止」⇔ 台账节真含 |N| 且无 |N+1|（拼接缺陷回归门）",
            not probs, "; ".join(probs) if probs else "自洽×%d" % len(anchors))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok in checks if not ok]
    print(f"[probe] selfcheck: {len(checks) - len(bad)}/{len(checks)} 断言复算通过"
          f"（隔离库 {tmp.name} 已删；稳定为 0 的前提是文档断言与实现不脱钩）")
    if bad:
        print(f"[probe] FAIL 断言: {bad}")
    return 1 if bad else 0


def main():
    conf = params()                          # 启动即校验配置，缺键宁可现在报
    p = argparse.ArgumentParser(description="聚鲸工作流 消费探针")
    sub = p.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("sample")
    a1.add_argument("--n", type=int, default=conf["probe"]["sample_n"],
                    help="本轮抽检条数（缺省取 assets/params.json#probe.sample_n）")
    a2 = sub.add_parser("record")
    a2.add_argument("--clause", required=True)
    a2.add_argument("--actor", help="留痕人 actor_id（准入登记表见 assets/params.json#memory.actors，未登记拒绝）")
    a2.add_argument("--evidence")
    a2.add_argument("--none", action="store_true")
    a2.add_argument("--source", choices=conf["probe"]["evidence_sources"], default="self",
                    help="证据来源（枚举见 assets/params.json#probe.evidence_sources）: "
                         "self=自报(不计有效消费) / witness=独立核实 / machine=命令可复现")
    a2.add_argument("--off-round", action="store_true",
                    help="会话消费通道（P3）：记录本轮未抽中但真实发生的会话消费；"
                         "仅独立源、须带 --evidence、锚点须在池、同锚点去重、不入轮转降级判定")
    a3 = sub.add_parser("retire-check")
    a3.add_argument("--window", type=int, default=conf["probe"]["retire_window"],
                    help="无消费窗口轮数（缺省取 assets/params.json#probe.retire_window）")
    a4 = sub.add_parser("effect", help="效果实测（解复用性🟡：有痕迹≠效果对）")
    a4.add_argument("--id", type=int)
    a4.add_argument("--mode")
    a4.add_argument("--verdict")
    a4.add_argument("--note")
    a4.add_argument("--list", action="store_true")
    a5 = sub.add_parser("size", help="加载预算机检（判据见 assets/params.json#budget）")
    a5.add_argument("--session", help="本次会话加载的件（逗号分隔相对路径），复现会话口径")
    a6 = sub.add_parser("cutoff", help="历史退役分界行（append-only 正规通道；理由长度机检）")
    a6.add_argument("--reason", required=True)
    a7 = sub.add_parser("kb-lint", help="经验库分层十项机检（判据见 assets/params.json#knowledge·#memory·#loop·#memory.personal_layers）")
    a8 = sub.add_parser("version", help="skill 自身版本指纹三处对照（data/VERSION＝H1＝description，⑥版本管理用于 skill 自身）")
    a9 = sub.add_parser("pool-check", help="待发版池机检（v2.2.0·D8：模板同构/状态值域/观察滞留/触发核算，判据 assets/params.json#release_pool）")
    a9.add_argument("--pool", help="实例池文件路径（缺省＝当前工作目录/instance_filename，即产品仓常驻 PENDING_RELEASES.md）")
    a10 = sub.add_parser("heat", help="条款热度回流（锁定批·D2：正向加分＋空转扣分，非对称权重，观测非门；判据 assets/params.json#governance）")
    a11 = sub.add_parser("wording", help="措辞绝对化观测（锁定批·甲 ②-B：只报不判、常态恒 0 退出；判据 assets/params.json#wording）")
    a11.add_argument("--since", help="只报 mtime ≥ 该日的「本次改动件」（YYYY-MM-DD；文件级粒度，非行级）")
    a11.add_argument("--files", help="显式指定件（逗号分隔，相对 skill 根；可指向扫描面外文件，供负测试）")
    a11.add_argument("--show-exc", action="store_true", help="同时列出「同行已列例外」的行（缺省只计数）")
    a12 = sub.add_parser("selfcheck", help="机检断言自检（锁定批·甲 ②-A：把文档绝对化断言搬进可跑复算；隔离库跑完即删）")
    args = p.parse_args()
    fn = {"sample": cmd_sample, "record": cmd_record, "retire-check": cmd_retire_check,
          "effect": cmd_effect, "size": cmd_size, "cutoff": cmd_cutoff,
          "kb-lint": cmd_kblint, "version": cmd_version, "pool-check": cmd_pool_check,
          "heat": cmd_heat, "wording": cmd_wording, "selfcheck": cmd_selfcheck}[args.cmd]
    sys.exit(fn(args) or 0)


if __name__ == "__main__":
    main()
