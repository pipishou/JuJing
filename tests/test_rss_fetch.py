#!/usr/bin/env python3
"""tools/rss_fetch.py 的离线测试（不打网络）。"""
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import rss_fetch as rf  # noqa: E402

OK = []


def check(name, cond, detail=""):
    OK.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ← {detail}"))


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>B线情报</title>
<item><title>换模周期缩短 30%</title><link>https://a.example/1</link>
<pubDate>Tue, 01 Nov 2026 08:00:00 +0800</pubDate><description>某厂把换模从90分钟压到60</description></item>
<item><title>重复条目</title><link>https://a.example/1</link><pubDate>Tue, 01 Nov 2026 09:00:00 +0800</pubDate></item>
<item><title>无链接条目</title><description>只有标题</description></item>
</channel></rss>""".encode("utf-8")

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>海关数据</title>
<entry><title>出口退税率调整</title><link rel="alternate" href="https://b.example/2"/>
<updated>2026-11-02T10:00:00Z</updated><summary>涉及塑料制品</summary></entry>
</feed>""".encode("utf-8")


def main():
    items = rf.parse(RSS, source="B线情报")
    check("RSS 解析出 2 条（去重 + 无链接无标题丢弃）", len(items) == 2, len(items))
    check("RSS 标题正确", items[0]["title"] == "换模周期缩短 30%", items[0]["title"])
    check("RSS 时间转 ISO", items[0]["published"].startswith("2026-11-01T00:00:00"), items[0]["published"])
    check("RSS 来源名回填", items[0]["source"] == "B线情报", items[0]["source"])

    atom = rf.parse(ATOM, source="海关数据")
    check("Atom 解析出 1 条", len(atom) == 1, len(atom))
    check("Atom link href 取到", atom[0]["link"] == "https://b.example/2", atom[0]["link"])
    check("Atom updated 转 ISO", atom[0]["published"].startswith("2026-11-02T10:00:00"), atom[0]["published"])

    try:
        rf.parse(b"<html><body>not rss</body></html>")
        check("非 feed 抛 FeedError", False, "没抛")
    except rf.FeedError:
        check("非 feed 抛 FeedError", True)

    try:
        rf.parse(b"<rss><channel>")
        check("坏 XML 抛 FeedError", False, "没抛")
    except rf.FeedError:
        check("坏 XML 抛 FeedError", True)

    # collect：单源失败不影响其他源，全局去重 + 按时间倒序 + limit
    def fake_fetch(url, timeout=20):
        if "bad" in url:
            raise rf.FeedError("拉取失败 bad：mock")
        return RSS if "rss" in url else ATOM

    orig = rf.fetch
    rf.fetch = fake_fetch
    try:
        feeds = [{"name": "R", "url": "http://rss/1"}, {"name": "A", "url": "http://atom/1"},
                 {"name": "B", "url": "http://bad/1"}, {"name": "R2", "url": "http://rss/2"}]
        got, errs = rf.collect(feeds, limit=10)
        check("collect 失败源进 errors", len(errs) == 1 and errs[0]["name"] == "B", errs)
        check("collect 跨源去重（重复 link 只留一条）", len(got) == 3, len(got))
        check("collect 按时间倒序", got[0]["published"] >= got[-1]["published"], [g["published"] for g in got])
        limited, _ = rf.collect(feeds, limit=2)
        check("collect limit 生效", len(limited) == 2, len(limited))
    finally:
        rf.fetch = orig

    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "feeds.json"
        p.write_text(json.dumps({"feeds": [{"name": "x", "url": "http://rss/1"}]}, ensure_ascii=False), encoding="utf-8")
        check("load_feeds 读 dict 形式", rf.load_feeds(p)[0]["url"] == "http://rss/1")
        p.write_text(json.dumps([{"name": "y", "url": "http://rss/2"}]), encoding="utf-8")
        check("load_feeds 读数组形式", rf.load_feeds(p)[0]["name"] == "y")
        p.write_text(json.dumps({"feeds": "nope"}), encoding="utf-8")
        try:
            rf.load_feeds(p)
            check("坏 feeds 文件抛 FeedError", False, "没抛")
        except rf.FeedError:
            check("坏 feeds 文件抛 FeedError", True)

        rc = rf.main(["--feed", "http://x/1", "--json"])
        check("无源可拉时 CLI 返回 1（全失败）", rc == 1, rc)
        rc = rf.main([])
        check("无参数 CLI 返回 2", rc == 2, rc)
        rc = rf.main(["--feeds", str(p)])
        check("坏 feeds 文件 CLI 返回 2", rc == 2, rc)

    total, passed = len(OK), sum(OK)
    print(f"\nRESULT: {'PASS' if passed == total else 'FAIL'}（{passed}/{total}）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
