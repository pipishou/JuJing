#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚鲸军师取证调用器——DeepSeek 官方 Tools API 薄壳（search/fetch）。

成本纪律（无硬编码硬上限；数值均为出厂默认，env 可覆盖）：
  单价出厂参考 0.005 元/成功请求（env JUNSHI_COST_PER_CALL 可改）；空结果与报错不计费。
  单文件账本 .junshi_state.json（cwd，env JUNSHI_STATE 可改路径）。
  预算线默认关闭，只观测不阻停；env JUNSHI_DAILY_BUDGET=N 开观测线，
  加 --hard-budget 才在超线时停（显式同意，非默认硬上限）。
  同 query 去重：缓存命中不计费（缓存条数出厂 50 条 FIFO，env JUNSHI_CACHE_MAX 可改）。
  --no-net 或 env JUNSHI_NO_NET=1 → 纯推理军师（退出码 2）。

key 解析链（只报来源，不回显值）：
  --key > env DEEPSEEK_API_KEY > env VITE_DEEPSEEK_KEY > ./.env.local > --env-file <path>
  （后两者读其中 VITE_DEEPSEEK_KEY= 行）

退出码：0 成功或缓存命中；1 失败（无 key/HTTP 错）；2 降级或 --hard-budget 超线停（需人决策）。
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

API = "https://api.deepseek.com"


def resolve_key(args):
    if args.key:
        return args.key.strip(), "--key 参数"
    for name in ("DEEPSEEK_API_KEY", "VITE_DEEPSEEK_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip(), "env " + name
    candidates = []
    if args.env_file:
        candidates.append(pathlib.Path(args.env_file))
    candidates.append(pathlib.Path.cwd() / ".env.local")
    for p in candidates:
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("VITE_DEEPSEEK_KEY="):
                    return line.split("=", 1)[1].strip(), str(p)
    return None, None


def state_path():
    return pathlib.Path(os.environ.get("JUNSHI_STATE", ".junshi_state.json"))


def load_state():
    today = datetime.date.today().isoformat()
    p = state_path()
    s = {}
    if p.is_file():
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            s = {}
    if s.get("date") != today:
        s = {"date": today, "count": 0, "cost_cny": 0.0, "cache": s.get("cache", {})}
    s.setdefault("cache", {})
    return s


def save_state(s):
    cache = s["cache"]
    cache_max = int(os.environ.get("JUNSHI_CACHE_MAX", "50") or 50)
    if len(cache) > cache_max:
        for k in list(cache)[: len(cache) - cache_max]:
            del cache[k]
    state_path().write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def call(path, payload, key):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def print_results(results, min_score):
    if not results:
        print("[空结果] 无命中（官方规则：不计费）")
        return
    for r in results:
        score = float(r.get("rerank_score") or 0)
        flag = "" if score >= min_score else "  <- 低置信（低于采纳线，只作线索）"
        print("[%.2f] %s%s" % (score, (r.get("title") or "")[:110], flag))
        print("      %s  auth=%s date=%s" % (r.get("url"), r.get("authority"), r.get("date")))
        summ = (r.get("summary") or "")[:260]
        if summ:
            print("      " + summ.replace("\n", " "))


def main():
    ap = argparse.ArgumentParser(description="聚鲸军师取证调用器（DeepSeek Tools API）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("search", help="检索")
    ps.add_argument("query")
    ps.add_argument("--count", type=int, default=5)
    ps.add_argument("--min-score", type=float,
                    default=float(os.environ.get("JUNSHI_MIN_SCORE", "1.0") or 1.0))
    ps.add_argument("--ori", default="聚鲸军师取证")
    pf = sub.add_parser("fetch", help="抓全文")
    pf.add_argument("url")
    for p in (ps, pf):
        p.add_argument("--key")
        p.add_argument("--env-file")
        p.add_argument("--hard-budget", action="store_true",
                       help="超观测线时阻停（默认只警告不阻停）")
        p.add_argument("--no-net", action="store_true")
    args = ap.parse_args()

    if args.no_net or os.environ.get("JUNSHI_NO_NET") == "1":
        print("[降级] 禁网生效：本次走纯推理军师，结论须标「未联网验证，置信度降档」。")
        sys.exit(2)

    state = load_state()
    budget = int(os.environ.get("JUNSHI_DAILY_BUDGET", "0") or 0)
    cost_per_call = float(os.environ.get("JUNSHI_COST_PER_CALL", "0.005") or 0.005)

    qhash = None
    if args.cmd == "search":
        qhash = hashlib.sha1(args.query.strip().lower().encode("utf-8")).hexdigest()
        hit = state["cache"].get(qhash)
        if hit is not None:
            print("[cache] 同 query 命中缓存（未计费）")
            print_results(hit, args.min_score)
            return

    if budget > 0 and state["count"] >= budget:
        if args.hard_budget:
            print("[预算] 今日已用 %d 次 ≈ ¥%.3f / 观测线 %d 次。" % (state["count"], state["cost_cny"], budget))
            print("--hard-budget 已开启：超线阻停（显式同意的闸，非默认硬上限）。去掉该参数则为只警告模式。")
            sys.exit(2)
        print("[预算·警告] 今日已用 %d 次 ≈ ¥%.3f，已过观测线 %d 次；仅警告不阻停（无硬上限）。"
              % (state["count"], state["cost_cny"], budget))

    key, src = resolve_key(args)
    if not key:
        print("[key] 未找到。解析链 = --key > env DEEPSEEK_API_KEY > env VITE_DEEPSEEK_KEY > ./.env.local > --env-file")
        sys.exit(1)

    if args.cmd == "search":
        data, err = call("/v1/tools/search", {
            "query": args.query[:128],
            "count": max(1, min(50, args.count)),
            "need_content": False,
            "ori_question": args.ori[:200],
        }, key)
    else:
        data, err = call("/v1/tools/fetch", {"url": args.url}, key)

    if err:
        print("[失败] %s（报错不计费）" % err)
        sys.exit(1)

    billed = True
    if args.cmd == "search":
        results = data.get("results") or []
        if not results:
            billed = False
        state["cache"][qhash] = results
        print_results(results, args.min_score)
    else:
        print("# %s\n" % data.get("title", ""))
        print(data.get("content", ""))

    if billed:
        state["count"] += 1
        state["cost_cny"] = round(state["cost_cny"] + cost_per_call, 4)
    save_state(state)
    budget_label = ("观测线 %d 次（仅警告）" % budget) if budget > 0 else "无预算线（仅观测）"
    print("\n[成本行] 今日累计 %d 次 ≈ ¥%.3f / %s（key 来源：%s）"
          % (state["count"], state["cost_cny"], budget_label, src))


if __name__ == "__main__":
    main()
