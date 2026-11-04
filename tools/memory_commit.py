"""记忆固化：定时把 bot-memory 里 Bot 写下的记忆提交成 git 版本，不 push。

设计约束（外挂式，不碰 Tobs）：
- 只对 bot-memory 仓库做 `git add -A` + `git commit`，**永不 push**（推不推由人决定）
- 无变更不提交（幂等）；提交信息带时间与变更文件数，便于 diff 追溯
- 全程 fail-soft：任何异常只返回 (ok=False, reason)，不抛、不影响 8787
- 不碰凭据：尊重 bot-memory/.gitignore；额外拒提交 `*.key/*.pem/.env*`
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MARKER = "memory-commit"
DENY_SUFFIXES = (".key", ".pem", ".token")
DENY_NAMES = (".env", ".env.local", ".credentials")


def repo_path(root: Path | None = None) -> Path:
    env = os.environ.get("BOT_MEMORY_DIR")
    if env:
        return Path(env)
    if root is not None:
        cand = root.parent / "bot-memory"
        if cand.exists():
            return cand
    return Path("D:/work/产品类/个人产品/bot-2/bot-memory")


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace",
    )


def _denied(path: str) -> bool:
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return name.startswith(DENY_NAMES) or name.endswith(DENY_SUFFIXES)


def status(repo: Path) -> dict:
    r = _git(repo, "status", "--porcelain")
    if r.returncode != 0:
        raise RuntimeError(f"git status 失败：{r.stderr.strip()[:200]}")
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    denied = [ln for ln in lines if _denied(ln[3:].strip())]
    return {"changed": len(lines), "denied": denied}


def commit(repo: Path, by: str = "bot-team", message: str | None = None) -> dict:
    """提交一次；无变更返回 skipped，被拒文件跳过并留痕。"""
    if not (repo / ".git").exists():
        return {"ok": False, "reason": f"不是 git 仓库：{repo}"}
    st = status(repo)
    if st["changed"] == 0:
        return {"ok": True, "skipped": True, "reason": "无变更"}
    # 逐文件 add，遇到被拒文件跳过
    denied = st["denied"]
    r = _git(repo, "status", "--porcelain", "-z")
    paths = [p for p in r.stdout.split("\0") if p.strip()]
    added = 0
    for entry in paths:
        path = entry[3:].strip()
        if not path or _denied(path):
            continue
        a = _git(repo, "add", "--", path)
        if a.returncode == 0:
            added += 1
    if added == 0:
        return {"ok": True, "skipped": True, "reason": "只剩被拒文件", "denied": denied}
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    msg = message or f"[{MARKER}] {stamp} 记忆固化（{added} 个文件，by {by}）"
    c = _git(repo, "commit", "-q", "-m", msg)
    if c.returncode != 0:
        tail = (c.stderr or c.stdout).strip().splitlines()[-1:] or [""]
        return {"ok": False, "reason": f"git commit 失败：{tail[0][:200]}"}
    rev = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"ok": True, "commit": rev, "files": added, "message": msg, "denied": denied}


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="memory_commit")
    ap.add_argument("--repo")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--by", default="bot-team")
    ap.add_argument("--message")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo) if a.repo else repo_path()
    if a.snapshot:
        try:
            res = snapshot_nested(repo, by=a.by)
        except Exception as exc:  # noqa: BLE001
            print(f"[memory-commit] 镜像失败：{exc}", file=sys.stderr)
            return 1
        print(res)
        return 0
    if a.status:
        try:
            print(status(repo))
        except Exception as exc:  # noqa: BLE001
            print(f"[memory-commit] 失败：{exc}", file=sys.stderr)
            return 1
        return 0
    try:
        res = commit(repo, by=a.by, message=a.message)
    except Exception as exc:  # noqa: BLE001
        print(f"[memory-commit] 失败：{exc}", file=sys.stderr)
        return 1
    if a.json:
        print(res)
    elif res.get("skipped"):
        print(f"[memory-commit] 跳过：{res.get('reason')}")
    elif res.get("ok"):
        print(f"[memory-commit] {res['commit']} 已提交 {res['files']} 个文件")
    else:
        print(f"[memory-commit] 失败：{res.get('reason')}", file=sys.stderr)
        return 1
    return 0



MIRROR_DIR = "_mirror"
NESTED_SKIP = {".git", "node_modules", "__pycache__", ".botharness"}


def nested_repos(repo: Path) -> list[Path]:
    """Tobs 给每个 Bot 建的是独立 git 仓，父仓看不见它们的提交 → 需要镜像。"""
    out = []
    for cand in sorted(repo.glob("*/bot-*")):
        if (cand / ".git").exists():
            out.append(cand)
    return out


def snapshot_nested(repo: Path, by: str = "bot-team") -> dict:
    """把每个嵌套仓的工作文件镜像到父仓 _mirror/<角色>/<bot>/ 并提交父仓。"""
    repos = nested_repos(repo)
    if not repos:
        return {"ok": True, "skipped": True, "reason": "没有嵌套仓"}
    copied = 0
    for sub in repos:
        rel = sub.relative_to(repo)
        dst_root = repo / MIRROR_DIR / rel
        r = _git(sub, "-c", "core.quotepath=false", "ls-files")
        for name in [x for x in r.stdout.splitlines() if x.strip()]:
            src, dst = sub / name, dst_root / name
            if any(part in NESTED_SKIP for part in Path(name).parts):
                continue
            if _denied(name):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes() if src.exists() else b""
            if not dst.exists() or dst.read_bytes() != data:
                dst.write_bytes(data)
                copied += 1
    if copied == 0:
        head = commit(repo, by=by)
        return {"ok": True, "mirrored": 0, "parent": head}
    readme = repo / MIRROR_DIR / "README.md"
    readme.write_text(
        "# 记忆镜像" + chr(10) + chr(10)
        + "Tobs 给每个 Bot 建独立 git 仓，父仓看不到它们的提交；本目录是这些仓工作文件的只读镜像，"
        + "由 tools/memory_commit.py --snapshot 生成。" + chr(10),
        encoding="utf-8",
    )
    head = commit(
        repo,
        by=by,
        message="[" + MARKER + "] " + datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        + " 记忆镜像（" + str(copied) + " 个文件，by " + by + "）",
    )
    return {"ok": True, "mirrored": copied, "parent": head}


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="memory_commit")
    ap.add_argument("--repo")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--by", default="bot-team")
    ap.add_argument("--message")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo) if a.repo else repo_path()
    if a.snapshot:
        try:
            res = snapshot_nested(repo, by=a.by)
        except Exception as exc:  # noqa: BLE001
            print(f"[memory-commit] 镜像失败：{exc}", file=sys.stderr)
            return 1
        print(res)
        return 0
    if a.status:
        try:
            print(status(repo))
        except Exception as exc:  # noqa: BLE001
            print(f"[memory-commit] 失败：{exc}", file=sys.stderr)
            return 1
        return 0
    try:
        res = commit(repo, by=a.by, message=a.message)
    except Exception as exc:  # noqa: BLE001
        print(f"[memory-commit] 失败：{exc}", file=sys.stderr)
        return 1
    if a.json:
        print(res)
    elif res.get("skipped"):
        print(f"[memory-commit] 跳过：{res.get('reason')}")
    elif res.get("ok"):
        print(f"[memory-commit] {res['commit']} 已提交 {res['files']} 个文件")
    else:
        print(f"[memory-commit] 失败：{res.get('reason')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
