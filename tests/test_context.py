#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C1/C2 上下文检索测试（全离线：不联网、不调真 LLM、不读写真实账本）。

覆盖：
  - 中文 2-gram / 英文 `[a-z0-9_]+` 分词
  - BM25 排序（3 篇文档，查询词只命中 1 篇）、k 限制、无命中返回 []
  - 路径/时间轻加权不破坏排序、score<=0 丢弃
  - 切块目标 600–1200 字、块不跨文件
  - 增量索引：sha256+mtime 未变跳过；变了才重建
  - docx/pdf/xlsx 抽取；抽不出的文件进 skipped 且不报错
  - /api/context/ask：no_citation、正常引用映射、空 q 400、无命中不调模型
  - /api/context/stats 回 meta.json

跑法：python -B tests/test_context.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
import zipfile
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "server"), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import context_index as ci  # noqa: E402  tools/context_index.py
from core import context  # noqa: E402

SPEC = importlib.util.spec_from_file_location("bot_team_app", ROOT / "server" / "app.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# --------------------------------------------------------------------------- #
# 测试夹具（现场造 docx/xlsx/pdf，不依赖 assets）
# --------------------------------------------------------------------------- #
def make_docx(path, paragraphs):
    body = "".join(
        '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % escape(p)
        for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (W_NS, body))
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("word/document.xml", xml)
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')


def make_xlsx(path, rows):
    cells = []
    for i, row in enumerate(rows):
        line = []
        for j, value in enumerate(row):
            if not value:
                continue
            ref = "%s%d" % (chr(65 + j), i + 1)
            line.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'
                        % (ref, escape(str(value))))
        cells.append('<row r="%d">%s</row>' % (i + 1, "".join(line)))
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("xl/workbook.xml",
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="清单" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="ws"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData>%s</sheetData></worksheet>' % "".join(cells))


def make_pdf(path, text, compress=False):
    """最小 PDF：无压缩 content stream（或 zlib 压缩），带 BT/Tj 文本操作。"""
    payload = ("BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text).encode("latin-1")
    if compress:
        import zlib
        payload = zlib.compress(payload)
    head = (b"%PDF-1.4" + bytes([10]) + b"1 0 obj << /Length "
            + str(len(payload)).encode("ascii") + b" >>" + bytes([10]) + b"stream" + bytes([10]))
    with open(str(path), "wb") as fh:
        fh.write(head)
        fh.write(payload)
        fh.write(bytes([10]) + b"endstream" + bytes([10]) + b"endobj" + bytes([10])
                 + b"trailer << /Root 1 0 R >>" + bytes([10]) + b"%EOF" + bytes([10]))


def write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        self.index_path = self.tmp / "runtime" / "context" / "index.jsonl"
        self.meta_path = self.tmp / "runtime" / "context" / "meta.json"
        self._env = {k: os.environ.pop(k, None) for k in
                     ("BOT_CONTEXT_DIRS", "BOT_CONTEXT_INDEX", "BOT_CONTEXT_META")}
        # 隔离白名单：测试期间默认目录置空，绝不让 --dir 把真实 4.7GB 目录扫进来
        self._orig_default_dirs = ci.DEFAULT_DIRS
        ci.DEFAULT_DIRS = ()
        self.addCleanup(lambda: setattr(ci, "DEFAULT_DIRS", self._orig_default_dirs))

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def build(self, **kw):
        return ci.build([str(self.docs)], self.index_path, self.meta_path, **kw)


# --------------------------------------------------------------------------- #
# 分词
# --------------------------------------------------------------------------- #
class TestTokenize(unittest.TestCase):
    def test_chinese_bigrams(self):
        self.assertEqual(context.tokenize("为什么卡"),
                         ["为什", "什么", "么卡"])
        self.assertEqual(context.tokenize("质量门"), ["质量", "量门"])
        # 单字成段时保留该字（否则 1 字查询永远无命中）
        self.assertEqual(context.tokenize("卡"), ["卡"])

    def test_english_lowercased_words(self):
        self.assertEqual(context.tokenize("BM25 Retrieval, M1_ok!"),
                         ["bm25", "retrieval", "m1_ok"])

    def test_mixed_drops_punctuation_and_space(self):
        toks = context.tokenize("塑胶厂 M1：为什么卡？")
        self.assertIn("塑胶", toks)
        self.assertIn("m1", toks)
        self.assertIn("为什", toks)
        self.assertNotIn("：", toks)
        self.assertNotIn(" ", toks)


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #
class TestBM25(TmpCase):
    def seed(self):
        write(self.docs / "质量中心" / "复盘.md",
              "上个月这条卡在质量门：供应商来料尺寸超差。" * 6)
        write(self.docs / "营销中心" / "周报.md",
              "本周营销活动复盘：渠道转化率上升。" * 6)
        write(self.docs / "研发中心" / "纪要.md",
              "研发周会纪要：版本排期与测试环境。" * 6)
        self.build()
        return context.load(self.index_path)

    def test_ranks_only_matching_doc_first(self):
        idx = self.seed()
        hits = idx.search("为什么卡在质量门", k=8)
        self.assertTrue(hits)
        self.assertEqual(pathlib.Path(hits[0]["path"]).name, "复盘.md")
        # 只有一篇含「质量/量门」bigram：其余篇 score<=0 必须被丢弃
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0]["score"], 0)
        self.assertIn("质量", hits[0]["snippet"])

    def test_k_limit(self):
        for i in range(3):
            write(self.docs / ("doc%d.md" % i), "同一主题 供应商来料 异常 处理。" * 8)
        self.build()
        idx = context.load(self.index_path)
        self.assertEqual(len(idx.search("供应商来料", k=2)), 2)
        self.assertEqual(len(idx.search("供应商来料", k=10)), 3)

    def test_no_hit_returns_empty(self):
        idx = self.seed()
        self.assertEqual(idx.search("量子纠缠 完全无关的词"), [])
        self.assertEqual(idx.search(""), [])
        self.assertEqual(context.load(self.tmp / "nope.jsonl").search("质量"), [])

    def test_path_boost_keeps_order_and_skips_zero(self):
        write(self.docs / "来料异常" / "a.md", "供应商来料异常处理流程。" * 10)
        write(self.docs / "其他" / "b.md", "供应商来料异常处理流程。" * 10)
        self.build()
        idx = context.load(self.index_path)
        hits = idx.search("来料", k=5)
        # 目录名命中「来料」的 +10%，排在前面；两篇都命中所以都在
        self.assertEqual(len(hits), 2)
        self.assertEqual(pathlib.Path(hits[0]["path"]).parent.name, "来料异常")

    def test_recent_mtime_boost(self):
        old = write(self.docs / "old.md", "来料异常复盘。" * 10)
        new = write(self.docs / "new.md", "来料异常复盘。" * 10)
        past = time.time() - 400 * 86400
        os.utime(str(old), (past, past))
        self.build()
        idx = context.load(self.index_path)
        hits = idx.search("来料异常", k=5)
        self.assertEqual(pathlib.Path(hits[0]["path"]).name, "new.md")
        self.assertAlmostEqual(hits[0]["score"] / hits[1]["score"], 1.05, places=2)


# --------------------------------------------------------------------------- #
# 索引器
# --------------------------------------------------------------------------- #
class TestIndexer(TmpCase):
    def test_chunk_sizes_and_no_cross_file(self):
        paras = ["第%d段：塑胶厂来料异常复盘与处理结论。" % i + "细节" * 44 for i in range(24)]
        write(self.docs / "big.md", "\n\n".join(paras))
        write(self.docs / "other.md", "另一份文件：营销渠道复盘。" * 60)
        stats, meta = self.build()
        self.assertEqual(meta["files_indexed"], 2)
        rows = [json.loads(ln) for ln in
                self.index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(len(r["text"]) <= 1200 for r in rows))
        self.assertEqual(stats["chunks"], len(rows))
        # 块不跨文件：每块只能来自一个文件
        by_file = {}
        for r in rows:
            by_file.setdefault(r["path"], []).append(r)
        self.assertEqual(len(by_file), 2)
        for path, chunk_rows in by_file.items():
            self.assertEqual([r["chunk_no"] for r in chunk_rows],
                             list(range(len(chunk_rows))))
            # 只有每个文件的最后一块允许不足 600（尾部余量）
            self.assertTrue(all(len(r["text"]) >= 600 for r in chunk_rows[:-1]),
                            [len(r["text"]) for r in chunk_rows])
            joined = "".join(r["text"] for r in chunk_rows)
            if pathlib.Path(path).name == "big.md":
                self.assertNotIn("营销", joined)
            else:
                self.assertNotIn("塑胶厂", joined)

    def test_incremental_skips_unchanged(self):
        write(self.docs / "a.md", "第一次索引的内容：来料异常。" * 20)
        stats1, meta1 = self.build()
        self.assertEqual(meta1["files_indexed"], 1)
        chunks1 = meta1["chunks"]
        stats2, meta2 = self.build()
        self.assertEqual(meta2["files_indexed"], 0)
        self.assertEqual(meta2["chunks"], chunks1)
        self.assertIn({"path": str(self.docs / "a.md"), "reason": "unchanged"},
                      meta2["files_skipped"])
        # 只动 mtime（sha 不变）也算变：重建该文件，其它文件仍复用
        write(self.docs / "b.md", "第二份文件：营销渠道。" * 20)
        self.build()
        future = time.time() + 60
        os.utime(str(self.docs / "a.md"), (future, future))
        stats3, meta3 = self.build()
        self.assertEqual(meta3["files_indexed"], 1)
        self.assertEqual(meta3["chunks"], chunks1 + 1 + meta2["chunks"] - chunks1)

    def test_docx_extraction(self):
        make_docx(self.docs / "复盘.docx",
                  ["塑胶厂来料异常复盘", "根因：供应商模具磨损，尺寸 CPK 掉到 0.9。"])
        stats, meta = self.build()
        self.assertEqual(meta["files_indexed"], 1, meta["files_skipped"])
        rows = [json.loads(ln) for ln in
                self.index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        text = "\n".join(r["text"] for r in rows)
        self.assertIn("供应商模具磨损", text)
        self.assertIn("CPK", text)

    def test_xlsx_extraction(self):
        make_xlsx(self.docs / "台账.xlsx",
                  [["事项", "状态"], ["模具台账更新", "卡住"], ["供应商复验", "已完成"]])
        stats, meta = self.build()
        self.assertEqual(meta["files_indexed"], 1, meta["files_skipped"])
        rows = [json.loads(ln) for ln in
                self.index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        text = "\n".join(r["text"] for r in rows)
        self.assertIn("模具台账更新", text)
        self.assertIn("卡住", text)

    def test_pdf_text_and_unreadable_pdf(self):
        make_pdf(self.docs / "ok.pdf", "Why the ticket stuck at the quality gate")
        make_pdf(self.docs / "flate.pdf", "Compressed quality gate notes", compress=True)
        (self.docs / "scan.pdf").write_bytes(b"%PDF-1.4\nstream\n" + bytes(range(1, 40)) * 8
                                             + b"\nendstream\n%%EOF\n")
        (self.docs / "bad.xlsx").write_bytes(b"not a zip at all")
        stats, meta = self.build()
        self.assertEqual(stats["ok"], True)
        self.assertEqual(meta["files_indexed"], 2, meta["files_skipped"])
        reasons = {pathlib.Path(r["path"]).name: r["reason"] for r in meta["files_skipped"]}
        self.assertTrue(reasons["scan.pdf"].startswith("pdf:"))
        self.assertTrue(reasons["bad.xlsx"].startswith("xlsx:"))
        # 抽不出不报错：CLI 仍是 rc=0
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            rc = ci.main(["--dir", str(self.docs), "--out", str(self.index_path),
                          "--meta", str(self.meta_path), "--json"])
        self.assertEqual(rc, 0)

    def test_gbk_csv_decoded(self):
        (self.docs / "数据.csv").write_bytes("事项,原因\n来料异常,供应商模具磨损\n".encode("gb18030"))
        self.build()
        rows = [json.loads(ln) for ln in
                self.index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertIn("供应商模具磨损", "\n".join(r["text"] for r in rows))

    def test_dry_run_writes_nothing(self):
        write(self.docs / "a.md", "内容" * 400)
        stats, meta = self.build(dry_run=True)
        self.assertTrue(stats["dry_run"])
        self.assertEqual(meta["files_indexed"], 1)
        self.assertFalse(self.index_path.exists())
        self.assertFalse(self.meta_path.exists())

    def test_too_large_is_skipped(self):
        write(self.docs / "huge.md", "字" * 5000)
        stats, meta = self.build(max_bytes=1000)
        self.assertEqual(meta["files_indexed"], 0)
        self.assertTrue(meta["files_skipped"][0]["reason"].startswith("too_large"))

    def test_cli_bad_dir_is_single_line_json_rc2(self):
        env = os.environ.get("BOT_CONTEXT_DIRS")
        os.environ["BOT_CONTEXT_DIRS"] = str(self.tmp / "missing")
        try:
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ci.main(["--json"])
        finally:
            if env is None:
                os.environ.pop("BOT_CONTEXT_DIRS", None)
            else:
                os.environ["BOT_CONTEXT_DIRS"] = env
        self.assertEqual(rc, 2)
        out = buf.getvalue().strip()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertFalse(json.loads(out)["ok"])


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return {"content": self.content}


class TestContextApi(TmpCase):
    def setUp(self):
        super().setUp()
        write(self.docs / "质量中心" / "复盘.md",
              "上个月这条卡在质量门：供应商来料尺寸超差。" * 8)
        write(self.docs / "营销中心" / "周报.md", "营销渠道复盘：转化率上升。" * 8)
        self.build()
        self.cfg = APP.Config(self.tmp)
        self.fake = FakeLLM("卡在来料尺寸超差 [1]。")
        self._orig_llm = APP.context_llm
        APP.context_llm = lambda cfg: self.fake
        self.addCleanup(self._restore)

    def _restore(self):
        APP.context_llm = self._orig_llm

    def test_no_citation_branch(self):
        self.fake.content = "我觉得是管理问题，没有引用。"
        status, body = APP.route_api(self.cfg, "POST", "/api/context/ask", {},
                                     {"q": "为什么卡在质量门"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": False, "reason": "no_citation"})

    def test_citation_out_of_range_is_no_citation(self):
        self.fake.content = "参考 [9] 的结论。"
        status, body = APP.route_api(self.cfg, "POST", "/api/context/ask", {},
                                     {"q": "为什么卡在质量门"})
        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], "no_citation")

    def test_ok_maps_citations(self):
        self.fake.content = "主因是来料尺寸超差 [1]，已让供应商整改 [1]。"
        status, body = APP.route_api(self.cfg, "POST", "/api/context/ask", {},
                                     {"q": "为什么卡在质量门", "k": 3})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual([c["n"] for c in body["citations"]], [1])
        self.assertEqual(pathlib.Path(body["citations"][0]["path"]).name, "复盘.md")
        self.assertIn("质量", body["citations"][0]["snippet"])
        self.assertEqual(len(self.fake.calls), 1)
        prompt = self.fake.calls[0][-1]["content"]
        self.assertIn("[1] 来源：", prompt)
        self.assertIn("为什么卡在质量门", prompt)

    def test_fullwidth_bracket_citation_accepted(self):
        self.fake.content = "来料尺寸超差［1］。"
        body = APP.route_api(self.cfg, "POST", "/api/context/ask", {},
                             {"q": "为什么卡在质量门"})[1]
        self.assertTrue(body["ok"])
        self.assertEqual(body["citations"][0]["n"], 1)

    def test_empty_q_is_400(self):
        with self.assertRaises(APP.ApiError) as ctx:
            APP.route_api(self.cfg, "POST", "/api/context/ask", {}, {"q": "   "})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(APP.ApiError):
            APP.route_api(self.cfg, "POST", "/api/context/ask", {}, {})

    def test_no_hits_does_not_call_llm(self):
        class Boom:
            def chat(self, *a, **kw):
                raise AssertionError("无命中时不允许调用 LLM")

        APP.context_llm = lambda cfg: Boom()
        status, body = APP.route_api(self.cfg, "POST", "/api/context/ask", {},
                                     {"q": "量子纠缠 完全无关"})
        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], "no_hits")
        self.assertEqual(body["citations"], [])

    def test_stats_returns_meta(self):
        status, body = APP.route_api(self.cfg, "GET", "/api/context/stats", {}, None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["exists"])
        self.assertEqual(body["files_total"], 2)
        self.assertEqual(body["chunks"], 2)
        self.assertEqual(body["dirs"], [str(self.docs)])

    def test_stats_without_index_is_200(self):
        empty = self.tmp / "empty-root"
        (empty / "runs").mkdir(parents=True)
        cfg = APP.Config(empty)
        status, body = APP.route_api(cfg, "GET", "/api/context/stats", {}, None)
        self.assertEqual(status, 200)
        self.assertFalse(body["exists"])
        self.assertEqual(body["files_total"], 0) if "files_total" in body else None

    def test_wrong_method_405(self):
        with self.assertRaises(APP.ApiError) as ctx:
            APP.route_api(self.cfg, "GET", "/api/context/ask", {}, None)
        self.assertEqual(ctx.exception.status, 405)


def main():
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    if result.wasSuccessful():
        print("tests/test_context.py: %d 项通过" % total)
        return 0
    print("tests/test_context.py: %d 项运行，%d 项失败"
          % (total, len(result.failures) + len(result.errors)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
