# -*- coding: utf-8 -*-
"""同日唯一编号分配（§2.1）。

规则：
  allocate(root, kind, task_id=None) -> str
  kind ∈ task/report/decision/analysis/evidence/cost/closure（closure 为兼容四卡扩展）
  格式：T-YYYYMMDD-001 / DL-YYYYMMDD-001 / D-... / A-... / E-... / C-...
  - task 级序号存 <root>/.task_seq.json；其余存 <root>/ids.json（kind -> next int）
  - 若 task_id 给到且 <root> 不是该 task 目录，则序号文件落到 <root>/<task_id>/ids.json
  - 同日多任务不重复：扫描已存在目录/文件，seq 跳过已占用编号
  - 文件锁：os.open(lock, O_CREAT|O_EXCL)，重试 ≤3s；原子写回 os.replace
只依赖 stdlib。
"""
import json
import os
import re
import time
from datetime import date
from pathlib import Path

from core import fsutil

PREFIXES = {
    "task": "T",
    "report": "DL",
    "decision": "D",
    "analysis": "A",
    "evidence": "E",
    "cost": "C",
    "closure": "C",
}

LOCK_TIMEOUT = 3.0
LOCK_STALE = 30.0
LOCK_POLL = 0.02


class IdError(RuntimeError):
    """编号分配失败（锁超时/参数非法/序号损坏）。"""


def _validate(kind):
    if kind not in PREFIXES:
        raise IdError(f"未知编号类型: {kind}（可选 {sorted(PREFIXES)}）")


def _seq_path(root, kind, task_id):
    root = Path(root)
    if kind == "task":
        return root / ".task_seq.json"
    # 其余编号都落在调用方给的根目录（约定为 runs/<task_id>/ids.json）；
    # task_id 只用于「已占用目录/文件」判定，不再改写根目录（避免造出陌生子目录）。
    return root / "ids.json"


def _read_json(path):
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise IdError(f"序号文件损坏: {path}: {exc}")
    if not isinstance(doc, dict):
        raise IdError(f"序号文件必须是对象: {path}")
    return doc


def _atomic_write_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = None, None
    try:
        fd, tmp = _mkstemp(path.parent, path.name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        fd = None
        fsutil.replace_with_retry(tmp, path)
        tmp = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _mkstemp(directory, stem):
    import tempfile
    return tempfile.mkstemp(prefix=f".{stem}.", suffix=".tmp", dir=str(directory))


class _FileLock:
    """O_CREAT|O_EXCL 锁；超时抛 IdError，陈旧锁自动清理。"""

    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > LOCK_STALE:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() > deadline:
                    raise IdError(f"编号锁超时（{LOCK_TIMEOUT}s）: {self.path}")
                time.sleep(LOCK_POLL)

    def __exit__(self, exc_type, exc, tb):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


def _existing_max(root, prefix, compact):
    """扫描 root 下已占用的 <prefix>-<compact>-NNN（目录或文件），返回最大 N。"""
    pattern = re.compile(rf"^{re.escape(prefix)}-{compact}-(\d{{3}})")
    best = 0
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return 0
    for entry in entries:
        m = pattern.match(entry.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _occupied(path_root, ident):
    """id 是否已被目录/文件占用。"""
    root = Path(path_root)
    for candidate in (root / ident, root / f"{ident}.json", root / f"{ident}.jsonl"):
        if candidate.exists():
            return True
    return False


def allocate(root, kind, task_id=None):
    """分配并持久化下一个编号，返回字符串 id。

    root: 编号根目录（task 级用 runs/，其余用 runs/<task_id>/ 或 loop 目录）
    kind: task|report|decision|analysis|evidence|cost|closure
    task_id: 可选；给到且 root 不是该任务目录时，ids.json 落到 root/<task_id>/
    """
    _validate(kind)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    prefix = PREFIXES[kind]
    compact = date.today().strftime("%Y%m%d")
    seq_path = _seq_path(root, kind, task_id)
    lock_path = seq_path.with_name(seq_path.name + ".lock")
    key = kind

    with _FileLock(lock_path):
        doc = _read_json(seq_path)
        current = int(doc.get(key) or 0)
        # 扫描已占用编号（目录/文件），seq 跳过
        scan_root = root if kind == "task" else Path(root) if not task_id or root.name == str(task_id) else root
        used = _existing_max(scan_root, prefix, compact)
        nxt = max(current, used + 1, 1)
        ident = f"{prefix}-{compact}-{nxt:03d}"
        while _occupied(scan_root, ident):
            nxt += 1
            ident = f"{prefix}-{compact}-{nxt:03d}"
        doc[key] = nxt + 1
        _atomic_write_json(seq_path, doc)
        return ident


def peek(root, kind, task_id=None):
    """只读当前序号（不占用）；主要给测试/诊断用。"""
    _validate(kind)
    seq_path = _seq_path(root, kind, task_id)
    doc = _read_json(seq_path)
    return int(doc.get(kind) or 1)


if __name__ == "__main__":
    import sys
    print(allocate(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
