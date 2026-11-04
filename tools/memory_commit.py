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
    ap.add_argument("--by", default="bot-team")
    ap.add_argument("--message")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo) if a.repo else repo_path()
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
