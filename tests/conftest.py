# -*- coding: utf-8 -*-
"""pytest 会话隔离：账本同 runs 一样不许写真实仓库。

背景（2026-10-27 实证）：测试把 runs 根指到临时目录，账本却仍写仓库根，
导致 3 个合成任务 33 行 step_run/task_created 混进真实账本，指标被污染。
这里把 BOT_LEDGER_ROOT 指向会话临时目录；子进程测试若继承 os.environ 即自动生效。
"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_ledger(tmp_path_factory):
    os.environ["BOT_LEDGER_ROOT"] = str(tmp_path_factory.mktemp("ledger-root"))
