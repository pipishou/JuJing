# -*- coding: utf-8 -*-
"""数据溯源（§2.2）：报告的 data_refs 必须能证明「这份报告读的是哪份文件、内容有没有被换」。

API：
  sha256_file(path) -> str
  refs_record(paths, base) -> [{path,sha256,size}]   缺失文件 → ProvenanceError
  verify_refs(refs, base, require_sha=True) -> None  缺失/哈希不符/sha 缺失 → ProvenanceError
只依赖 stdlib。
"""
import hashlib
import json
from pathlib import Path


class ProvenanceError(RuntimeError):
    """数据引用不可溯源。"""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve(path, base):
    p = Path(str(path))
    if p.is_absolute():
        return p
    return Path(base) / p


def _display(path, base):
    """报告里存的相对路径：base 内用相对 POSIX 路径，否则用绝对路径。"""
    p = Path(str(path))
    try:
        return p.resolve().relative_to(Path(base).resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return str(p)


def refs_record(paths, base):
    """把路径列表记为 [{path,sha256,size}]；任一文件缺失 → ProvenanceError（不写半截）。"""
    if not isinstance(paths, (list, tuple)):
        raise ProvenanceError(f"paths 必须是列表，收到 {type(paths)}")
    out = []
    errors = []
    for raw in paths:
        if not raw:
            errors.append("空路径")
            continue
        p = _resolve(raw, base)
        if not p.is_file():
            errors.append(f"文件不存在: {raw}（解析为 {p}）")
            continue
        try:
            out.append({"path": _display(raw, base), "sha256": sha256_file(p), "size": p.stat().st_size})
        except OSError as exc:
            errors.append(f"读取失败: {raw}: {exc}")
    if errors:
        raise ProvenanceError("；".join(errors))
    if not out:
        raise ProvenanceError("data_refs 不能为空")
    return out


def verify_refs(refs, base, require_sha=True):
    """校验 refs（dict 或 str）：文件存在、hash 一致（require_sha 时还要求带 sha256）。"""
    if not isinstance(refs, (list, tuple)) or not refs:
        raise ProvenanceError("data_refs 为空")
    errors = []
    for ref in refs:
        if isinstance(ref, dict):
            raw = ref.get("path")
            if not raw:
                errors.append(f"引用缺少 path: {ref!r}")
                continue
            p = _resolve(raw, base)
            if not p.is_file():
                errors.append(f"引用的数据文件不存在: {raw}")
                continue
            sha = ref.get("sha256")
            if require_sha and not sha:
                errors.append(f"引用缺少 sha256（须由 assemble 生成）: {raw}")
                continue
            if sha:
                actual = sha256_file(p)
                if actual != sha:
                    errors.append(f"数据文件已被篡改（sha256 不符）: {raw} expected={sha} actual={actual}")
            elif "size" in ref:
                size = ref.get("size")
                if isinstance(size, int) and size != p.stat().st_size:
                    errors.append(f"数据文件大小不符: {raw}")
        elif isinstance(ref, str):
            if not ref.strip():
                errors.append("空引用")
                continue
            p = _resolve(ref, base)
            if not p.is_file():
                errors.append(f"引用的数据文件不存在: {ref}")
            elif require_sha:
                errors.append(f"引用是裸路径、缺少 sha256 溯源: {ref}")
        else:
            errors.append(f"不支持的引用类型 {type(ref)}: {ref!r}")
    if errors:
        raise ProvenanceError("；".join(errors))


if __name__ == "__main__":
    import sys
    print(json.dumps(refs_record(sys.argv[1:], Path.cwd()), ensure_ascii=False, indent=2))
