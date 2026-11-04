"""memory_commit 自测（stdlib，零依赖，全部在临时目录里跑）。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import memory_commit as mc  # noqa: E402

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"[FAIL] {name}")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def make_repo() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="memc-"))
    git(tmp, "init", "-q", "-b", "main")
    git(tmp, "config", "user.name", "test")
    git(tmp, "config", "user.email", "test@example.com")
    (tmp / "README.md").write_text("mem\n", encoding="utf-8")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-q", "-m", "init")
    return tmp


def main() -> int:
    repo = make_repo()

    # 1 无变更 → skipped
    r = mc.commit(repo, by="tester")
    check("无变更 skipped", r.get("ok") is True and r.get("skipped") is True)

    # 2 有变更 → 提交，且不 push（未配置 remote 也能成功）
    (repo / "assistant" / "MEMORY.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / "assistant" / "MEMORY.md").write_text("- 事实\n", encoding="utf-8")
    r = mc.commit(repo, by="tester")
    check("有变更提交成功", r.get("ok") is True and bool(r.get("commit")))
    check("提交信息带标记", mc.MARKER in (r.get("message") or ""))
    log = git(repo, "log", "--oneline", "-1").stdout
    check("HEAD 是本次提交", "记忆固化" in log)

    # 3 再跑一次 → skipped（幂等）
    r2 = mc.commit(repo, by="tester")
    check("幂等 skipped", r2.get("skipped") is True)

    # 4 凭据文件不入库
    (repo / "secret.key").write_text("k\n", encoding="utf-8")
    (repo / "notes.txt").write_text("n\n", encoding="utf-8")
    r3 = mc.commit(repo, by="tester")
    tracked = git(repo, "ls-files").stdout
    check("secret.key 未入库", "secret.key" not in tracked)
    check("notes.txt 已入库", "notes.txt" in tracked)
    check("denied 有留痕", any("secret.key" in d for d in (r3.get("denied") or [])))

    # 5 非 git 目录 → 明确失败
    plain = Path(tempfile.mkdtemp(prefix="plain-"))
    r4 = mc.commit(plain)
    check("非仓库失败", r4.get("ok") is False and "不是 git 仓库" in (r4.get("reason") or ""))

    # 6 status 计数
    (repo / "another.txt").write_text("a\n", encoding="utf-8")
    st = mc.status(repo)
    check("status 计数含新文件", st["changed"] >= 1)

    print()
    print(f"RESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    for f in FAILS:
        print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
