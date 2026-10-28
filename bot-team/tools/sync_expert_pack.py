"""专家包同步：仓库源 ⇄ Octop 运行副本。

仓库是源（可提交、可迁移、换电脑不丢）；`.octop/expert_market/` 是运行时副本
（Octop 平台按约定路径加载）。两边内容不一致时，本工具显式报告差异而不是静默覆盖。

用法：
    python tools/sync_expert_pack.py                 # 只看差异（dry-run）
    python tools/sync_expert_pack.py --to-runtime    # 仓库 → .octop（部署）
    python tools/sync_expert_pack.py --from-runtime  # .octop → 仓库（把线上改动捞回来）
    python tools/sync_expert_pack.py --check         # 有差异则 rc=1（可进机检）
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

BOT_TEAM = Path(__file__).resolve().parents[1]
REPO_SRC = BOT_TEAM / "packs" / "expert_market"
RUNTIME_DST = BOT_TEAM.parents[1] / ".octop" / "expert_market"
PACKS = ("manufacturing-advisory-board", "manufacturing-business-team")
IGNORE = {"inbox", "ingested", "__pycache__", ".git"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if IGNORE & set(path.relative_to(root).parts):
            continue
        out[path.relative_to(root).as_posix()] = sha256(path)
    return out


def diff_pack(pack: str) -> tuple[list[str], list[str], list[str]]:
    src, dst = walk(REPO_SRC / pack), walk(RUNTIME_DST / pack)
    added = sorted(set(dst) - set(src))
    removed = sorted(set(src) - set(dst))
    changed = sorted(k for k in set(src) & set(dst) if src[k] != dst[k])
    return added, removed, changed


def sync(pack: str, direction: str) -> int:
    src_root = REPO_SRC / pack if direction == "to_runtime" else RUNTIME_DST / pack
    dst_root = RUNTIME_DST / pack if direction == "to_runtime" else REPO_SRC / pack
    if not src_root.is_dir():
        print(f"[SKIP] {pack}：源不存在 {src_root}")
        return 0
    count = 0
    for rel in walk(src_root):
        target = dst_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_root / rel, target)
        count += 1
    print(f"[OK] {pack}：{src_root} → {dst_root}，{count} 个文件")
    return count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="专家包同步（仓库源 ⇄ Octop 运行副本）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--to-runtime", action="store_true", help="仓库 → .octop")
    g.add_argument("--from-runtime", action="store_true", help=".octop → 仓库")
    g.add_argument("--check", action="store_true", help="有差异则 rc=1")
    args = ap.parse_args(argv)

    if args.to_runtime:
        for pack in PACKS:
            sync(pack, "to_runtime")
        return 0
    if args.from_runtime:
        for pack in PACKS:
            sync(pack, "from_runtime")
        return 0

    dirty = False
    for pack in PACKS:
        added, removed, changed = diff_pack(pack)
        if not (added or removed or changed):
            print(f"[SAME] {pack}")
            continue
        dirty = True
        print(f"[DIFF] {pack}  只在运行副本 {len(added)} / 只在仓库 {len(removed)} / 内容不同 {len(changed)}")
        for label, items in (("运行副本独有", added), ("仓库独有", removed), ("内容不同", changed)):
            for rel in items[:20]:
                print(f"   - {label}：{rel}")
            if len(items) > 20:
                print(f"   - {label}：…… 还有 {len(items) - 20} 个")
    if args.check:
        return 1 if dirty else 0
    print("\n提示：--to-runtime 部署到运行副本；--from-runtime 把线上改动捞回仓库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
