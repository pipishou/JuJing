# -*- coding: utf-8 -*-
"""路径白名单（M1.5 §2.4）：上传文件名 / 任务 id 必须先过这一层。

唯一入口：
  - safe_relative(name) -> str   上传数据文件名（允许单层子目录，如 "data/a.json"）
  - safe_task_id(tid)   -> str   任务 id 形状（T-YYYYMMDD-<slug>）

设计：白名单而非黑名单——只放行 [\\w.\\-]+ 组成的、最多两段（单层子目录）的相对路径。
绝对路径 / 盘符 / "../" / 反斜杠 / 控制字符 / Windows 保留设备名 / 结尾点号空格 一律 PathError。
只依赖 stdlib，不抛裸 ValueError（调用方按 PathError 统一处理）。
"""
import re

# \w 在 Python 3 str 正则里等价于 Unicode 单词字符（含中文），正好覆盖"塑胶厂-数据.json"
_SEGMENT_RE = re.compile(r"^[\w.\-]+$", re.UNICODE)
_TASK_ID_RE = re.compile(r"^T-\d{8}-[A-Za-z0-9_\-]+$")
# Windows 保留设备名（不区分大小写，含 COM1-9 / LPT1-9）
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
MAX_NAME_LEN = 200
MAX_TASK_ID_LEN = 120


class PathError(ValueError):
    """路径 / 任务 id 不安全。"""


def _reject_controls(value, kind):
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise PathError(f"{kind} 含控制字符: {value!r}")


def _check_segment(seg, original):
    if not seg:
        raise PathError(f"文件名含空路径段: {original!r}")
    if seg in (".", ".."):
        raise PathError(f"文件名不允许 . / .. 路径段: {original!r}")
    if not _SEGMENT_RE.match(seg):
        raise PathError(f"文件名只允许字母/数字/中文/下划线/点/连字符: {original!r}")
    if len(seg) > MAX_NAME_LEN:
        raise PathError(f"文件名过长（>{MAX_NAME_LEN}）: {original[:40]!r}…")
    stem = seg.split(".", 1)[0].lower()
    if stem in _RESERVED:
        raise PathError(f"文件名是 Windows 保留设备名: {original!r}")
    if seg.endswith(".") or seg.endswith(" "):
        raise PathError(f"文件名不允许以点号/空格结尾（Windows 会静默改名）: {original!r}")


def safe_relative(name):
    """把上传文件名校验成安全的相对路径（允许单层子目录）；不安全抛 PathError。

    >>> safe_relative("material_refill_T.json")
    'material_refill_T.json'
    >>> safe_relative("data/a.json")
    'data/a.json'
    """
    if isinstance(name, bytes):
        try:
            name = name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PathError(f"文件名不是合法 UTF-8: {name!r}") from exc
    if not isinstance(name, str):
        raise PathError(f"文件名必须是字符串: {name!r}")
    if not name:
        raise PathError("文件名为空")
    _reject_controls(name, "文件名")
    raw = name
    # 统一分隔符前先拒绝反斜杠（Windows 穿越常写成 ..\\）
    if "\\" in name:
        raise PathError(f"文件名不允许反斜杠（拒绝 ..\\ 穿越写法）: {raw!r}")
    if name.startswith("/") or name.startswith("\\"):
        raise PathError(f"文件名不允许绝对路径: {raw!r}")
    if re.match(r"^[A-Za-z]:", name):
        raise PathError(f"文件名不允许盘符: {raw!r}")
    if "\x00" in name:
        raise PathError("文件名含 NUL 字节")
    # UNC / 协议前缀（file:、//server）
    if raw.startswith("~") or "://" in raw:
        raise PathError(f"文件名不允许协议/家目录前缀: {raw!r}")

    parts = name.split("/")
    if len(parts) > 2:
        raise PathError(f"只允许单层子目录（最多两段）: {raw!r}")
    for seg in parts:
        _check_segment(seg, raw)
    return "/".join(parts)


def safe_task_id(tid):
    """校验任务 id 形状 T-YYYYMMDD-<slug>；不合法抛 PathError。"""
    if isinstance(tid, bytes):
        try:
            tid = tid.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PathError(f"task_id 不是合法 UTF-8: {tid!r}") from exc
    if not isinstance(tid, str):
        raise PathError(f"task_id 必须是字符串: {tid!r}")
    if not tid:
        raise PathError("task_id 为空")
    _reject_controls(tid, "task_id")
    if len(tid) > MAX_TASK_ID_LEN:
        raise PathError(f"task_id 过长（>{MAX_TASK_ID_LEN}）: {tid[:40]!r}…")
    if not _TASK_ID_RE.match(tid):
        raise PathError(f"task_id 形状非法（应匹配 T-YYYYMMDD-<slug>）: {tid!r}")
    return tid


if __name__ == "__main__":  # 自测：打印若干合法/非法样例
    import sys

    for value in ("ok.json", "data/ok.json", "../x", "C:\\x", "a\\b", "/abs", "", ".", "..", "con", "a..b"):
        try:
            print("OK  ", repr(value), "->", safe_relative(value))
        except PathError as exc:
            print("REJ ", repr(value), "->", exc)
    sys.exit(0)
