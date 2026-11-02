#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文索引器（C1）：把白名单目录里的文档切成可检索的块（零依赖，stdlib）。

为什么存在：bot 只会读「工作事项清单」，答不了「上个月这条为什么卡」「去年怎么处理的」。
这里把本地文档（纪要/报表/复盘）落到一个 JSONL 块索引，供 core/context.py 做 BM25 检索，
再由 /api/context/ask 把命中的片段喂给 LLM —— 只依据片段回答，找不到就说没找到。

支持格式：.md .txt .csv .xlsx .docx .pdf .json
  - .xlsx 走 tools/xlsx_lite.py（零依赖；不用 openpyxl：真实 WPS 文件的 styles.xml 会让它崩）
  - .docx 只抽 word/document.xml 里的 <w:t> 正文（不碰图片/批注）
  - .pdf 只在无压缩或可 FlateDecode 时抽 BT/ET 里的文本；抽不出记 skipped，不报错
  - 其余按纯文本读（UTF-8 → GBK → UTF-8 replace 兜底）

切块：按空行/标题分段，目标 600–1200 字；超长段落按句末标点硬切；块不跨文件。
增量：输出里已有同 path 且 sha256+mtime 都没变的文件，直接复用旧块（不重读正文）。

输出：
  runtime/context/index.jsonl   每行 {path, sha256, mtime, chunk_no, text}（覆盖重写）
  runtime/context/meta.json     {generated_at, files_total, files_indexed,
                                 files_skipped:[{path,reason}], chunks, dirs}

用法：
  python -B tools/context_index.py                       # 默认白名单两目录
  python -B tools/context_index.py --dir "D:/work/文档类/质量中心" --json
  python -B tools/context_index.py --dry-run             # 只统计不落盘
  BOT_CONTEXT_DIRS="D:/a;D:/b" python -B tools/context_index.py

退出码：0 成功；2 参数/IO 级错误（单行 JSON 到 stdout）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from xlsx_lite import XlsxError, read_sheets  # noqa: E402  同目录工具，零依赖

TZ = timezone(timedelta(hours=8))
DEFAULT_DIRS = (r"D:\work\文档类", r"C:\Users\kf-41\Desktop\个人内容")
SUFFIXES = (".md", ".txt", ".csv", ".xlsx", ".docx", ".pdf", ".json")
MIN_CHARS = 600
MAX_CHARS = 1200
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS_PER_SHEET = 2000
MAX_TEXT_CHARS = 2_000_000
SKIP_DIR_NAMES = {"__pycache__", "node_modules", ".git", ".svn", "$RECYCLE.BIN",
                  "System Volume Information", ".idea", ".vscode"}
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_BLANK_RE = re.compile(r"\n[ \t]*\n")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExtractError(Exception):
    """单个文件抽不出正文（进 skipped，不让整次索引失败）。"""


# --------------------------------------------------------------------------- #
# 目录 / 文件枚举
# --------------------------------------------------------------------------- #
def resolve_dirs(cli_dirs=None):
    """白名单目录：BOT_CONTEXT_DIRS（分号分隔）覆盖默认，--dir 再追加（去重保序）。"""
    env = os.environ.get("BOT_CONTEXT_DIRS")
    if env:
        base = [d.strip() for d in env.split(";") if d.strip()]
    else:
        base = [str(Path(d)) for d in DEFAULT_DIRS]
    out = []
    for d in list(base) + [str(Path(d)) for d in (cli_dirs or [])]:
        if d and d not in out:
            out.append(d)
    return out


def iter_files(dirs):
    """遍历白名单文件：跳过隐藏目录/依赖目录/Office 临时锁文件（~$）。"""
    seen = set()
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for root, subdirs, files in os.walk(str(base)):
            subdirs[:] = sorted(s for s in subdirs
                                if s not in SKIP_DIR_NAMES and not s.startswith("."))
            for name in sorted(files):
                if name.startswith("~$"):
                    continue
                p = Path(root) / name
                if p.suffix.lower() not in SUFFIXES:
                    continue
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                yield p


# --------------------------------------------------------------------------- #
# 正文抽取
# --------------------------------------------------------------------------- #
def _decode_text(data):
    """UTF-8（含 BOM）→ GBK → UTF-8 replace。WPS/Excel 导出的 CSV 常是 GBK。"""
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", "replace")


def _extract_plain(data):
    return _decode_text(data)


def _extract_docx(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractError("docx:not_zip:%s" % str(exc)[:60])
    with zf:
        try:
            xml = zf.read("word/document.xml")
        except KeyError:
            raise ExtractError("docx:no_document_xml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ExtractError("docx:bad_xml:%s" % str(exc)[:60])
    paras = []
    for p in root.iter(W_NS + "p"):
        txt = "".join(t.text or "" for t in p.iter(W_NS + "t")).strip()
        if txt:
            paras.append(txt)
    if not paras:
        txt = "".join(t.text or "" for t in root.iter(W_NS + "t")).strip()
        if txt:
            paras = [txt]
    if not paras:
        raise ExtractError("docx:empty")
    return "\n\n".join(paras)


def _pdf_literal_strings(content):
    """扫 PDF content stream 里的 (literal) 与 <hex> 字符串（够用即止，不做完整语法解析）。"""
    out = []
    i, n = 0, len(content)
    escapes = {b"n": 10, b"r": 13, b"t": 9, b"b": 8, b"f": 12}
    while i < n:
        ch = content[i:i + 1]
        if ch == b"(":
            depth, buf = 1, bytearray()
            i += 1
            while i < n and depth:
                c = content[i:i + 1]
                if c == b"\\":
                    nxt = content[i + 1:i + 2]
                    if nxt in escapes:
                        buf.append(escapes[nxt])
                        i += 2
                        continue
                    m = re.match(rb"[0-7]{1,3}", content[i + 1:i + 4])
                    if m:
                        buf.append(int(m.group(0), 8) & 0xFF)
                        i += 1 + len(m.group(0))
                        continue
                    buf += nxt
                    i += 2
                    continue
                if c == b"(":
                    depth += 1
                elif c == b")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf += c
                i += 1
            out.append(bytes(buf))
        elif ch == b"<":
            j = content.find(b">", i)
            if j < 0:
                break
            hexs = re.sub(rb"[^0-9A-Fa-f]", b"", content[i + 1:j])
            if hexs:
                if len(hexs) % 2:
                    hexs += b"0"
                try:
                    out.append(bytes.fromhex(hexs.decode("ascii")))
                except ValueError:
                    pass
            i = j + 1
        else:
            i += 1
    return out


def _pdf_chunk_text(raw):
    if b"BT" not in raw or (b"Tj" not in raw and b"TJ" not in raw):
        return ""
    parts = []
    for b in _pdf_literal_strings(raw):
        if not b:
            continue
        if b[:2] == b"\xfe\xff":
            try:
                parts.append(b[2:].decode("utf-16-be", "ignore"))
                continue
            except (UnicodeDecodeError, ValueError):
                pass
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                parts.append(b.decode(enc))
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
    return "".join(parts)


def _printable_ratio(text):
    if not text:
        return 0.0
    good = sum(1 for ch in text if ch.isprintable() or ch in "\n\t ")
    return good / len(text)


def _extract_pdf(data):
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise ExtractError("pdf:not_pdf")
    texts = []
    pos = 0
    while True:
        start = data.find(b"stream", pos)
        if start < 0:
            break
        body = data.find(b"endstream", start)
        if body < 0:
            break
        raw = data[start + len(b"stream"):body]
        raw = raw.lstrip(b"\r\n")
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        pos = body + len(b"endstream")
        chunk = raw
        if chunk[:1] == b"\x78" or chunk[:2] in (b"\x9c", b"\xda", b"\x01"):
            try:
                chunk = zlib.decompress(chunk)
            except zlib.error:
                chunk = raw  # 无压缩 content stream：直接当明文
        texts.append(_pdf_chunk_text(chunk))
    text = "\n".join(t for t in texts if t.strip()).strip()
    if not text or len(text) < 20 or _printable_ratio(text) < 0.85:
        raise ExtractError("pdf:no_text")
    return text


def _extract_xlsx(path):
    try:
        sheets = read_sheets(str(path), dates=True, max_rows=MAX_ROWS_PER_SHEET)
    except XlsxError as exc:
        raise ExtractError("xlsx:%s" % str(exc)[:120])
    parts = []
    for name, rows in sheets.items():
        parts.append("## %s" % name)
        for row in rows:
            cells = [str(c) for c in row if str(c).strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_text(path):
    """→ 正文；抽不出抛 ExtractError（原因写进 meta.files_skipped）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ExtractError("read_error:%s" % str(exc)[:80])
    if len(data) > MAX_TEXT_CHARS:
        data = data  # 大小上限已在调用方按 --max-bytes 拦过，这里只防极端
    if suffix == ".xlsx":
        text = _extract_xlsx(p)
    elif suffix == ".docx":
        text = _extract_docx(data)
    elif suffix == ".pdf":
        text = _extract_pdf(data)
    else:
        text = _extract_plain(data)
    text = _CTRL_RE.sub("", text or "").strip()
    if not text:
        raise ExtractError("empty")
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractError("text_too_large:%d" % len(text))
    return text


# --------------------------------------------------------------------------- #
# 切块：按空行/标题分段，贪心合并到 600–1200 字
# --------------------------------------------------------------------------- #
def _blocks(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    out = []
    for block in _BLANK_RE.split(text):
        cur = []
        for line in block.split("\n"):
            if _HEADING_RE.match(line) and any(x.strip() for x in cur):
                out.append("\n".join(cur).strip())
                cur = [line]
            else:
                cur.append(line)
        if cur:
            out.append("\n".join(cur).strip())
    return [b for b in out if b]


def _split_long(block, max_chars):
    """单段超长：优先在句末/换行切，切不动再硬切到 max_chars。"""
    if len(block) <= max_chars:
        return [block]
    pieces, rest = [], block
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = max_chars
        for punct in ("\n", "。", "！", "？", "；", ". ", "! ", "? ", "; "):
            idx = window.rfind(punct)
            if idx >= max_chars - 300 and idx >= max_chars // 2:
                cut = idx + len(punct)
                break
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        pieces.append(rest.strip())
    return [p for p in pieces if p]


def chunk_text(text, min_chars=MIN_CHARS, max_chars=MAX_CHARS):
    """返回长度 ≤ max_chars 的块列表；能凑到 min_chars 的块不提前切。"""
    blocks = []
    for b in _blocks(text):
        blocks.extend(_split_long(b, max_chars))
    chunks, cur = [], ""
    for b in blocks:
        if not cur:
            cur = b
            continue
        if len(cur) + 2 + len(b) <= max_chars:
            cur = cur + "\n\n" + b
        else:
            chunks.append(cur)
            cur = b
    if cur:
        if chunks and len(chunks[-1]) + 2 + len(cur) <= max_chars:
            chunks[-1] = chunks[-1] + "\n\n" + cur
        else:
            chunks.append(cur)
    return [c for c in chunks if c.strip()]


# --------------------------------------------------------------------------- #
# 索引构建
# --------------------------------------------------------------------------- #
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def load_previous(out_path):
    """读上一版 index.jsonl → {path: {sha256, mtime, chunks:[record,...]}}（坏行忽略）。"""
    prev = {}
    p = Path(out_path)
    if not p.is_file():
        return prev
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return prev
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not rec.get("path") or "text" not in rec:
            continue
        entry = prev.setdefault(rec["path"], {
            "sha256": rec.get("sha256"), "mtime": rec.get("mtime"), "chunks": [],
        })
        entry["chunks"].append(rec)
    for entry in prev.values():
        entry["chunks"].sort(key=lambda r: r.get("chunk_no") or 0)
    return prev


def _same_file(prev_entry, sha, mtime):
    if not prev_entry or not prev_entry.get("chunks"):
        return False
    if prev_entry.get("sha256") != sha:
        return False
    try:
        return abs(float(prev_entry.get("mtime") or 0) - float(mtime)) < 1e-6
    except (TypeError, ValueError):
        return False


def build(dirs, out, meta_out, max_bytes=DEFAULT_MAX_BYTES, dry_run=False):
    """跑一次索引；返回 (stats, records)。dry_run=True 时声明不写盘。"""
    started = time.time()
    out, meta_out = Path(out), Path(meta_out)
    files = list(iter_files(dirs))
    prev = load_previous(out)
    records = []
    skipped = []
    indexed = 0
    for p in files:
        key = str(p)
        try:
            st = p.stat()
        except OSError as exc:
            skipped.append({"path": key, "reason": "stat_error:%s" % str(exc)[:80]})
            continue
        if st.st_size > max_bytes:
            skipped.append({"path": key, "reason": "too_large:%d" % st.st_size})
            continue
        try:
            data = p.read_bytes()
        except OSError as exc:
            skipped.append({"path": key, "reason": "read_error:%s" % str(exc)[:80]})
            continue
        sha = sha256_bytes(data)
        mtime = st.st_mtime
        old = prev.get(key)
        if _same_file(old, sha, mtime):
            for rec in old["chunks"]:
                records.append({"path": rec["path"], "sha256": rec.get("sha256") or sha,
                                "mtime": rec.get("mtime") or mtime,
                                "chunk_no": rec.get("chunk_no") or 0,
                                "text": rec["text"]})
            skipped.append({"path": key, "reason": "unchanged"})
            continue
        try:
            text = extract_text(p)
        except ExtractError as exc:
            skipped.append({"path": key, "reason": str(exc)[:200]})
            continue
        chunks = chunk_text(text)
        if not chunks:
            skipped.append({"path": key, "reason": "empty"})
            continue
        for no, chunk in enumerate(chunks):
            records.append({"path": key, "sha256": sha, "mtime": mtime,
                            "chunk_no": no, "text": chunk})
        indexed += 1

    meta = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "files_total": len(files),
        "files_indexed": indexed,
        "files_skipped": skipped,
        "chunks": len(records),
        "dirs": list(dirs),
    }
    stats = {
        "ok": True,
        "dry_run": bool(dry_run),
        "files_total": len(files),
        "files_indexed": indexed,
        "files_skipped": len(skipped),
        "chunks": len(records),
        "dirs": list(dirs),
        "out": str(out),
        "meta": str(meta_out),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if not dry_run:
        _write_index(out, records)
        _write_json(meta_out, meta)
    return stats, meta


def _write_index(out_path, records):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(str(tmp), str(out_path))


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def default_paths(root=None):
    root = Path(root) if root else HERE.parent
    return (root / "runtime" / "context" / "index.jsonl",
            root / "runtime" / "context" / "meta.json")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 老控制台不支持时忽略
        pass
    ap = argparse.ArgumentParser(description="上下文索引器（白名单目录 → chunk 索引）")
    ap.add_argument("--dir", action="append", default=[], help="追加白名单目录（可多次）")
    ap.add_argument("--out", help="index.jsonl 路径（默认 runtime/context/index.jsonl）")
    ap.add_argument("--meta", help="meta.json 路径（默认 runtime/context/meta.json）")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                    help="单文件上限（字节，超过记 skipped；默认 8MB）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--json", action="store_true", help="单行 JSON 输出")
    args = ap.parse_args(argv)

    out_default, meta_default = default_paths()
    out = Path(args.out) if args.out else out_default
    meta_out = Path(args.meta) if args.meta else (
        Path(args.out).with_name("meta.json") if args.out else meta_default)
    dirs = resolve_dirs(args.dir)
    try:
        if not any(Path(d).is_dir() for d in dirs):
            raise RuntimeError("白名单目录一个都不存在：%s" % "; ".join(dirs))
        stats, meta = build(dirs, out, meta_out, max_bytes=args.max_bytes, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - CLI 统一单行 JSON + rc=2
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)},
                         ensure_ascii=False))
        return 2

    if args.json:
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    print("上下文索引%s：文件 %d（新索引 %d，跳过 %d），块 %d，耗时 %d ms"
          % ("（dry-run 未落盘）" if args.dry_run else "",
             stats["files_total"], stats["files_indexed"], stats["files_skipped"],
             stats["chunks"], stats["elapsed_ms"]))
    for d in dirs:
        print("  dir: %s" % d)
    reasons = {}
    for row in meta["files_skipped"]:
        kind = row["reason"].split(":", 1)[0]
        reasons[kind] = reasons.get(kind, 0) + 1
    for kind, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  skip: %-14s %d" % (kind, count))
    if not args.dry_run:
        print("  out: %s" % out)
        print("  meta: %s" % meta_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
