#!/usr/bin/env python3
"""拉取 RSSHub / 任意 RSS 2.0 或 Atom feed，输出条目 JSON（零依赖）。

用途：内容操盘手的「每日情报」第 1 步——只拉 feed，不做爬虫、不解析网页正文。
用法：
    python -B tools/rss_fetch.py --feed <url> [--feed <url> ...] [--limit 5] [--json]
    python -B tools/rss_fetch.py --feeds tools/rss_feeds.example.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = "bot-team-rss/1.0 (+content-operator)"
NS_ATOM = "{http://www.w3.org/2005/Atom}"


class FeedError(Exception):
    pass


def _txt(node, *names):
    for n in names:
        v = node.find(n)
        if v is not None and v.text:
            return v.text.strip()
    return ""


def _atom_link(node):
    for link in node.findall(f"{NS_ATOM}link"):
        rel = link.get("rel") or "alternate"
        if rel == "alternate" and link.get("href"):
            return link.get("href")
    for link in node.findall(f"{NS_ATOM}link"):
        if link.get("href"):
            return link.get("href")
    return ""


def _when(raw):
    if not raw:
        return ""
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(xml_bytes, source=""):
    """解析 RSS 2.0 / Atom，返回条目列表（不联网，便于测试）。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FeedError(f"不是合法 XML：{exc}") from exc

    items = []
    if root.tag.endswith("rss"):
        channel = root.find("channel")
        if channel is None:
            raise FeedError("RSS 缺少 channel")
        title = _txt(channel, "title")
        for it in channel.findall("item"):
            items.append({
                "title": _txt(it, "title"),
                "link": _txt(it, "link") or _txt(it, "{http://www.w3.org/2005/Atom}link"),
                "published": _when(_txt(it, "pubDate", "published", "date")),
                "summary": _txt(it, "description", "summary"),
                "source": source or title,
            })
    elif root.tag == f"{NS_ATOM}feed":
        title = _txt(root, f"{NS_ATOM}title")
        for it in root.findall(f"{NS_ATOM}entry"):
            items.append({
                "title": _txt(it, f"{NS_ATOM}title"),
                "link": _atom_link(it),
                "published": _when(_txt(it, f"{NS_ATOM}updated", f"{NS_ATOM}published")),
                "summary": _txt(it, f"{NS_ATOM}summary", f"{NS_ATOM}content"),
                "source": source or title,
            })
    else:
        raise FeedError(f"不认识的 feed 根节点：{root.tag}")

    out, seen = [], set()
    for it in items:
        key = it["link"] or it["title"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:  # noqa: BLE001 - 网络错误统一转 FeedError
        raise FeedError(f"拉取失败 {url}：{exc}") from exc


def collect(feeds, limit=5, timeout=20, per_source=None):
    """feeds=[{name,url}] → (items, errors)，全局去重后按时间倒序截断。

    per_source: 单个源最多取几条（None=不限）。不设上限时，更新频繁的源
    （如快讯类）会把整个 limit 占满，日推 10 条会变成"同一家 10 条"。
    """
    items, errors, seen = [], [], set()
    for f in feeds:
        url = (f or {}).get("url", "").strip()
        name = ((f or {}).get("name") or (f or {}).get("source") or "").strip()
        if not url:
            continue
        try:
            got = parse(fetch(url, timeout=timeout), source=name)
        except FeedError as exc:
            errors.append({"url": url, "name": name, "error": str(exc)})
            continue
        for it in got:
            key = it["link"] or it["title"]
            if key in seen:
                continue
            seen.add(key)
            items.append(it)

    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    if per_source:
        kept, used = [], {}
        for it in items:
            src = it.get("source") or ""
            if used.get(src, 0) >= per_source:
                continue
            used[src] = used.get(src, 0) + 1
            kept.append(it)
        items = kept
    return items[:limit], errors


def load_feeds(path):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    feeds = data.get("feeds") if isinstance(data, dict) else data
    if not isinstance(feeds, list):
        raise FeedError("feeds 文件必须是数组或 {\"feeds\": [...]}")
    return [f for f in feeds if isinstance(f, dict)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="拉取 RSS/Atom feed")
    ap.add_argument("--feed", action="append", default=[], help="单个 feed URL，可多次")
    ap.add_argument("--feeds", help="feeds JSON 文件")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--per-source", type=int, default=0,
                    help="单源最多几条（0=不限）；防止快讯类源占满名额")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="输出 JSON（默认人类可读）")
    args = ap.parse_args(argv)

    feeds = [{"name": "", "url": u} for u in args.feed]
    if args.feeds:
        try:
            feeds += load_feeds(args.feeds)
        except (OSError, ValueError, FeedError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
    if not feeds:
        print(json.dumps({"ok": False, "error": "没有 feed：给 --feed 或 --feeds"}, ensure_ascii=False))
        return 2

    items, errors = collect(feeds, limit=args.limit, timeout=args.timeout,
                            per_source=(args.per_source or None))
    if args.json:
        print(json.dumps({"ok": bool(items), "count": len(items), "items": items, "errors": errors},
                         ensure_ascii=False, indent=2))
        return 0 if items else 1
    if not items:
        print(f"没有条目；失败 {len(errors)} 个源")
        for e in errors:
            print(f"  ! {e['name'] or e['url']}: {e['error']}")
        return 1
    for i, it in enumerate(items, 1):
        print(f"{i}) {it['title']}")
        if it.get("source"):
            print(f"   来源：{it['source']}   时间：{it.get('published') or '未知'}")
        if it.get("link"):
            print(f"   {it['link']}")
    if errors:
        print(f"\n[警告] {len(errors)} 个源失败：" + "；".join(e["name"] or e["url"] for e in errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
