# -*- coding: utf-8 -*-
"""文件级跨进程互斥（stdlib only）——bot-team 唯一的锁原语。

为什么需要它：
  `ThreadingHTTPServer` 下每个请求一个线程，`load → 改 → save` 不是原子操作；
  两个进程（server + CLI）同时写同一个账本时更糟。进程内的 `threading.Lock`
  只能挡住同进程线程，挡不住 CLI/server 两个进程，所以锁必须落在文件系统上。

实现（按平台选原生锁，不自己发明轮询协议）：
  - Windows：`msvcrt.locking(fd, LK_NBLCK, 1)` 锁锁文件第 1 个字节；
  - POSIX：`fcntl.flock(fd, LOCK_EX | LOCK_NB)`。
  两者都是**按文件句柄**的排他锁：进程崩溃/被杀时由内核回收，不会留下
  「死锁文件」——这比 `O_CREAT|O_EXCL` 标记文件更安全（后者崩溃即永久卡死，
  只能靠 mtime 猜陈旧）。

用法：
    from core.filelock import FileLock, LockTimeout
    with FileLock(path.with_name(path.name + ".lock"), timeout=5.0):
        ...  # 读-改-写 全在这段里

约定：
  - 锁文件本身**不删**（删掉会与等待者产生 inode 竞争）；锁状态由内核持有，
    文件里只有一个占位字节，长期存在无害。
  - 不可重入/不可嵌套同一把锁（会自己等自己直到超时）。
  - 超时抛 `LockTimeout`（RuntimeError 子类），调用方必须转成自己的领域错误，
    禁止静默跳过锁继续写。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

DEFAULT_TIMEOUT = 5.0
DEFAULT_POLL = 0.01
IS_WINDOWS = os.name == "nt"


class LockTimeout(RuntimeError):
    """在 timeout 秒内没拿到锁（说明有别的进程/线程长期占用）。"""


class FileLock:
    """排他文件锁上下文管理器；超时抛 LockTimeout。"""

    def __init__(self, path, *, timeout: float = DEFAULT_TIMEOUT,
                 poll: float = DEFAULT_POLL):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.poll = max(0.001, float(poll))
        self._fd: int | None = None

    # -------------------------------------------------- 平台原语
    def _try_lock(self) -> None:
        assert self._fd is not None
        if IS_WINDOWS:
            import msvcrt

            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self._fd is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            # 关句柄时内核也会释放；解锁失败不应掩盖业务异常。
            pass

    # -------------------------------------------------- 上下文协议
    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        if IS_WINDOWS and os.fstat(fd).st_size == 0:
            # msvcrt.locking 需要锁一个真实存在的字节
            os.write(fd, b"\0")
        self._fd = fd
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    finally:
                        self._fd = None
                    raise LockTimeout(
                        "文件锁超时（%.1fs）：%s（另一进程/线程正在写，或上次异常未释放）"
                        % (self.timeout, self.path)
                    ) from None
                time.sleep(self.poll)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._unlock()
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return False


def lock_path(target) -> Path:
    """账本/序号文件对应的锁文件路径：`<name>.lock`（同目录）。"""
    p = Path(target)
    return p.with_name(p.name + ".lock")


def locked(target, *, timeout: float = DEFAULT_TIMEOUT) -> FileLock:
    """便捷入口：`with locked(store_path): ...`。"""
    return FileLock(lock_path(target), timeout=timeout)
