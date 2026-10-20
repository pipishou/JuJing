# -*- coding: utf-8 -*-
"""原子写 / 追加写工具（stdlib）。

所有落盘（报告、任务、状态、签名、四卡）都走 tempfile + fsync + os.replace，
保证「失败不落半成品」；run.log 采用追加写（每行一条 JSON）。
"""
import json
import os
import tempfile
import time
from pathlib import Path


def replace_with_retry(tmp, path, attempts=10, delay=0.05):
    """os.replace 在 Windows 上会因目标文件被并发读句柄占用而报 WinError 5。

    server 轮询 status.json 与本工具写 status.json 撞上时，目标文件不带
    FILE_SHARE_DELETE，替换被系统拒绝；重试即可稳定落盘。
    """
    last = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last


def atomic_write_text(path, text):
    """同目录 temp 写 UTF-8 文本 + os.replace；失败自动清理 temp。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def atomic_write_json(path, doc, indent=2):
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=indent) + "\n")


def append_jsonl(path, record):
    """追加一行 JSON（run.log 用）；父目录自动创建。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_json(path):
    """读 JSON（容忍 BOM）；失败抛 OSError/ValueError，由调用方决定语义。"""
    with open(Path(path), encoding="utf-8-sig") as fh:
        return json.load(fh)
