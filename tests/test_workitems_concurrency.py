"""P0-5：workitems / 发号的文件级互斥（并发无丢失、id 不重复、超时明确报错）。"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import ids  # noqa: E402
from core.filelock import FileLock, LockTimeout  # noqa: E402
from tools import workitems_store as ws  # noqa: E402


def clean_env():
    env = dict(os.environ)
    env.pop("BOT_WORKITEMS_FILE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class ConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "current.json"
        self._saved = os.environ.pop("BOT_WORKITEMS_FILE", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["BOT_WORKITEMS_FILE"] = self._saved
        else:
            os.environ.pop("BOT_WORKITEMS_FILE", None)
        self.tmp.cleanup()

    def run_threads(self, fn, n=20):
        barrier = threading.Barrier(n)
        results, errors = [None] * n, []

        def worker(i):
            try:
                barrier.wait(timeout=20)
                results[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001 - 测试要把异常带回来
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads), "线程未在超时内结束")
        return results


class TestWorkitemsConcurrency(ConcurrencyCase):
    def test_20_threads_update_no_lost_update(self):
        _, item = ws.add_locked({"title": "并发测试"}, path=self.path)
        item_id = item["id"]

        def update(i):
            store, _ = ws.update_locked(item_id, {"note": f"w{i}"}, path=self.path)
            return store["revision"]

        revisions = self.run_threads(update, 20)
        self.assertEqual(sorted(revisions), list(range(2, 22)), "revision 必须严格递增且不重")
        store = ws.load(self.path)
        self.assertEqual(store["revision"], 21)
        self.assertIn(store["items"][0]["note"], {f"w{i}" for i in range(20)})
        # 没有静默丢更新：每一条 revision 都真实落过盘，最终盘面就是最后一条。
        self.assertEqual(ws.load(self.path)["items"][0]["note"],
                         store["items"][0]["note"])

    def test_20_threads_add_unique_ids(self):
        ids_seen = self.run_threads(
            lambda i: ws.add_locked({"title": f"并发新增 {i}"}, path=self.path)[1]["id"], 20)
        self.assertEqual(len(set(ids_seen)), 20, f"id 重复：{ids_seen}")
        store = ws.load(self.path)
        self.assertEqual(len(store["items"]), 20)
        self.assertEqual(len({it["id"] for it in store["items"]}), 20)
        self.assertEqual(store["revision"], 20)

    def test_8_processes_add_unique_ids(self):
        code = f"""
import json, sys
sys.path.insert(0, {str(ROOT)!r})
from tools import workitems_store as ws
_, item = ws.add_locked({{"title": "进程并发"}}, path={str(self.path)!r}, timeout=30)
print(item["id"])
"""
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code], cwd=str(ROOT),
                                  env=clean_env(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8")
                 for _ in range(8)]
        outs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            outs.append(out.strip().splitlines()[-1])
        self.assertEqual(len(set(outs)), 8, f"跨进程 id 重复：{outs}")
        self.assertEqual(len(ws.load(self.path)["items"]), 8)

    def test_mutate_exception_does_not_persist(self):
        ws.add_locked({"title": "回滚"}, path=self.path)
        before = self.path.read_bytes()

        def boom(store):
            store["items"][0]["title"] = "不应落盘"
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            ws.mutate(boom, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_save_refuses_to_overwrite(self):
        _, item = ws.add_locked({"title": "乐观写"}, path=self.path)
        stale = ws.load(self.path)          # 基于 revision 1
        ws.update_locked(item["id"], {"note": "别人先改了"}, path=self.path)  # revision 2
        stale["items"][0]["note"] = "覆盖"
        with self.assertRaises(ws.StoreLockError) as ctx:
            ws.save(stale, self.path)
        self.assertIn("拒绝覆盖", str(ctx.exception))
        self.assertEqual(ws.load(self.path)["revision"], 2)

    def test_internal_loaded_revision_not_persisted(self):
        ws.add_locked({"title": "字段"}, path=self.path)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("_loaded_revision", doc)

    def test_lock_timeout_is_explicit(self):
        ws.add_locked({"title": "占锁"}, path=self.path)
        lock_file = ws.lock_path(self.path)
        with FileLock(lock_file, timeout=5):
            with self.assertRaises(LockTimeout):
                with FileLock(lock_file, timeout=0.1):
                    pass
            item_id = ws.load(self.path)["items"][0]["id"]
            with self.assertRaises(ws.StoreLockError) as ctx:
                ws.update_locked(item_id, {"note": "超时"}, path=self.path, timeout=0.1)
            self.assertIn("锁超时", str(ctx.exception))


class TestIdAllocation(ConcurrencyCase):
    def test_20_threads_allocate_unique(self):
        self.assertEqual(ids.allocate(self.root, "report").split("-")[0], "DL")
        seen = self.run_threads(lambda i: ids.allocate(self.root, "report"), 20)
        self.assertEqual(len(set(seen)), 20, f"编号重复：{seen}")
        self.assertEqual(len({s.rsplit("-", 1)[1] for s in seen}), 20)

    def test_8_processes_allocate_unique(self):
        code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from core import ids
print(ids.allocate({str(self.root)!r}, "report"))
"""
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code], cwd=str(ROOT),
                                  env=clean_env(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8")
                 for _ in range(8)]
        outs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            outs.append(out.strip().splitlines()[-1])
        self.assertEqual(len(set(outs)), 8, f"跨进程编号重复：{outs}")

    def test_id_lock_timeout_is_id_error(self):
        with FileLock(self.root / "ids.json.lock", timeout=5):
            with self.assertRaises(ids.IdError) as ctx:
                ids.allocate(self.root, "report")
            self.assertIn("超时", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
