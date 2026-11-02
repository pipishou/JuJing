#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文检索（C2）：纯 stdlib BM25，读 tools/context_index.py 产出的块索引。

分词口径（刻意简单、可解释，不做词表/模型）：
  - 中文：连续汉字按 2-gram 切（「为什么卡」→ 为什/什么/么卡）；单字成段时保留该字
  - 英文/数字：`[a-z0-9_]+` 小写切词（bm25、m1、wi_01）
  - 其余字符（标点/空白/emoji）丢弃

排序：BM25（k1=1.5, b=0.75）→ 路径轻加权（查询词命中目录名 +10%）
      → 时间轻加权（mtime 在近 90 天内 +5%）→ score 降序取前 k。
score<=0 的块丢弃；无命中返回 []，绝不编造。

用法：
  from core import context
  idx = context.load("runtime/context/index.jsonl")
  idx.search("上个月为什么卡", k=5)   # [{path, chunk_no, snippet, score}]
"""
from __future__ import annotations

import json
import math
import re
import time
from array import array
from pathlib import Path

K1 = 1.5
B = 0.75
PATH_BOOST = 1.10
RECENT_BOOST = 1.05
RECENT_DAYS = 90
SNIPPET_CHARS = 200

_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[a-z0-9_]+")


def tokenize(text):
    """中文 2-gram + 英文小写词。返回 list（保留重复，供 tf 统计）。

    用 finditer 扫「整段中文/整段英文」而不是逐字符匹配：40k 块的索引
    加载量下这一步就是全部耗时（逐字符版本 load ≈13s，此版 ≈3s）。
    """
    s = str(text or "").lower()
    out = []
    for m in _TOKEN_RE.finditer(s):
        run = m.group(0)
        if run[0].isascii():
            out.append(run)
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[k:k + 2] for k in range(len(run) - 1))
    return out


def _unique(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _dir_segments(path):
    """路径里的目录名（小写），用于「同名目录命中 +10%」。"""
    try:
        parts = Path(str(path)).parts
    except (TypeError, ValueError):
        return ()
    return tuple(p.lower() for p in parts[:-1] if p)


class ContextIndex:
    """内存索引：docs[{path,chunk_no,text,mtime,dirs}] + 倒排 postings(term → array('I'))。

    刻意存倒排而不是每块一个 tf dict：真实 文档类 索引 4 万块时，
    tf dict 版本常驻约 590MB，倒排版本约 150MB。
    """

    def __init__(self, index_path=None, docs=None, postings=None, dl=None, df=None):
        self.index_path = str(index_path) if index_path else None
        self.docs = list(docs or [])
        self.exists = bool(self.docs)
        self.n = len(self.docs)
        self.postings = postings if postings is not None else {}
        self.dl = dl if dl is not None else array("I")
        self.df = df if df is not None else {}
        total = sum(self.dl) if len(self.dl) else 0
        self.avgdl = (total / self.n) if self.n else 0.0

    @classmethod
    def load(cls, index_path):
        """读 index.jsonl；文件缺失/坏行不抛错（返回空索引，检索即 []）。"""
        docs, postings, dl, df = [], {}, array("I"), {}
        seen_paths = {}
        p = Path(str(index_path))
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                text = rec.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                toks = tokenize(text)
                if not toks:
                    continue
                doc_id = len(docs)
                path = str(rec.get("path") or "")
                path = seen_paths.setdefault(path, path)  # 同一文件的块共用 path 字符串
                docs.append({
                    "path": path,
                    "chunk_no": int(rec.get("chunk_no") or 0),
                    "text": text,
                    "mtime": _to_float(rec.get("mtime")),
                    "dirs": _dir_segments(path),
                })
                dl.append(len(toks))
                tf = {}
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                for term, count in tf.items():
                    arr = postings.get(term)
                    if arr is None:
                        arr = postings[term] = array("I")
                    if count == 1:
                        arr.append(doc_id)
                    else:
                        arr.extend([doc_id] * count)
                    df[term] = df.get(term, 0) + 1
        return cls(index_path, docs, postings, dl, df)

    # -- 检索 ------------------------------------------------------------- #
    def search(self, q, k=8, now=None):
        """BM25 检索 → [{path, chunk_no, snippet, score}]，score 降序，无命中 []。"""
        try:
            k = int(k)
        except (TypeError, ValueError):
            k = 8
        if k <= 0 or not self.docs:
            return []
        terms = _unique(tokenize(q))
        if not terms:
            return []
        now = time.time() if now is None else float(now)
        recent_cut = now - RECENT_DAYS * 86400
        avgdl = self.avgdl or 1.0
        scores = {}
        for term in terms:
            post = self.postings.get(term)
            if not post:
                continue
            df = self.df.get(term, 0)
            idf = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
            counts = {}
            for doc_id in post:
                counts[doc_id] = counts.get(doc_id, 0) + 1
            for doc_id, tf in counts.items():
                denom = tf + K1 * (1.0 - B + B * (self.dl[doc_id] / avgdl))
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (K1 + 1.0)) / denom
        hits = []
        for doc_id, score in scores.items():
            if score <= 0:
                continue
            doc = self.docs[doc_id]
            if self._path_hit(doc, terms):
                score *= PATH_BOOST
            if doc["mtime"] and doc["mtime"] >= recent_cut:
                score *= RECENT_BOOST
            hits.append({
                "path": doc["path"],
                "chunk_no": doc["chunk_no"],
                "snippet": _snippet(doc["text"], terms),
                "score": round(score, 6),
            })
        hits.sort(key=lambda h: (-h["score"], h["path"], h["chunk_no"]))
        return hits[:k]

    @staticmethod
    def _path_hit(doc, terms):
        dirs = doc.get("dirs") or ()
        if not dirs:
            return False
        for term in terms:
            if len(term) < 2:
                continue
            for seg in dirs:
                if term in seg:
                    return True
        return False

    def stats(self):
        return {"index": self.index_path, "exists": self.exists,
                "chunks": self.n, "terms": len(self.df),
                "avg_tokens": round(self.avgdl, 2)}


def _to_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0  # NaN 兜底


def _snippet(text, terms, width=SNIPPET_CHARS):
    """以最先命中的查询词为中心截一段（尽量在字边界收尾）。"""
    low = text.lower()
    pos = -1
    best_len = 0
    for term in terms:
        if not term:
            continue
        idx = low.find(term)
        if idx < 0:
            continue
        if pos < 0 or idx < pos or (idx == pos and len(term) > best_len):
            pos, best_len = idx, len(term)
    if pos < 0:
        body = text[:width]
        return body + ("…" if len(text) > width else "")
    start = max(0, pos - 40)
    end = min(len(text), start + width)
    body = text[start:end].strip()
    if start > 0:
        body = "…" + body
    if end < len(text):
        body = body + "…"
    return body


_CACHE = {}
_CACHE_MAX = 4


def load(index_path):
    """加载索引 → ContextIndex（缺失即空索引，search 返回 []）。

    带进程内缓存（键=绝对路径+mtime_ns+size）：真实 文档类 索引 4 万块、
    load≈6s，而 search 只有 0.1s —— 不缓存就等于每问一次重付 6s。
    索引文件被重写（mtime/size 变）自动换新；测试里小索引同理。
    """
    p = Path(str(index_path))
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return ContextIndex.load(p)
    idx = _CACHE.get(key)
    if idx is not None:
        return idx
    idx = ContextIndex.load(p)
    if idx.docs:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = idx
    return idx


def search(index, q, k=8, now=None):
    """便捷入口：index 可以是路径或已加载的 ContextIndex。"""
    idx = load(index) if isinstance(index, (str, Path)) else index
    return idx.search(q, k=k, now=now)
