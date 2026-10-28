# -*- coding: utf-8 -*-
"""主动发现 · 存量异常复检：判据/去重/埋点/推送的机检。

重点守三件容易坏的事：
1) 判据来自行业包与数据实算（改包阈值必须改结论）；
2) 同一份数据只建一单（指纹去重），数据变了才允许再建；
3) 推送失败不落完成标记，下次续推同一个任务，不重复建任务。
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import discover_recheck  # noqa: E402
from core import telemetry  # noqa: E402

PACK = ROOT / "packs" / "manufacturing" / "pack.json"
T_START = date(2026, 10, 5)
B_START = date(2026, 9, 5)


def _write_dataset(folder, name, rates, start, line="B"):
    recs = [{"date": (start + timedelta(days=i)).isoformat(),
             "production_line": line, "refill_rate": v}
            for i, v in enumerate(rates)]
    doc = {"meta": {"dataset": name, "production_line": line}, "records": recs}
    path = folder / "data" / (name + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _baseline_rates():
    # 与生产 T30 同形：μ≈5.01、σ≈0.17（30 天）
    return [round(5.01 + 0.17 * ((i % 5) - 2) / 2, 2) for i in range(30)]


def _anomaly_rates():
    # 与 T-20261020-027 同形：均值 6.019，7 天里 5 天越过 5.40
    return [5.10, 5.28, 5.54, 5.90, 6.26, 6.85, 7.20]


def _layout(tmp_path, t_rates=None, b_rates=None):
    root = tmp_path / "bot-team"
    runs = root / "runs" / "T-20261020-027"
    _write_dataset(runs, "material_refill_T", t_rates or _anomaly_rates(), T_START)
    _write_dataset(runs, "material_refill_T30", b_rates or _baseline_rates(), B_START)
    return root, runs


def _run(root, *extra):
    args = ["--root", str(root), "--runs", str(root / "runs"),
            "--pack", str(PACK)] + list(extra)
    return discover_recheck.main(args)


def _task_dirs(root):
    """只数「本工具建出来的任务」：源数据目录也长得像 T-*，但它没有 task.json。"""
    return sorted(p.name for p in (root / "runs").glob("T-*")
                  if p.is_dir() and (p / "task.json").is_file())


def _ledger(name, task_id=None):
    rows = [r for r in telemetry.read() if r.get("name") == name]
    if task_id:
        rows = [r for r in rows if r.get("task_id") == task_id]
    return rows


def test_dry_run_reports_hit_but_writes_nothing(tmp_path, capsys):
    root, _ = _layout(tmp_path)
    assert _run(root) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(out["hits"]) == 1
    assert out["hits"][0]["t_mean"] == pytest.approx(6.019)
    assert out["hits"][0]["days_above"] == 5
    assert out["created"] == []
    assert _task_dirs(root) == []
    assert not (root / ".telemetry" / "recheck-state.json").exists()


def test_apply_creates_task_with_data_and_discovered_event(tmp_path, capsys):
    root, _ = _layout(tmp_path)
    assert _run(root, "--apply", "--no-push") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    task_id = out["created"][0]
    tdir = root / "runs" / task_id
    task = json.loads((tdir / "task.json").read_text(encoding="utf-8"))
    assert task["runner"] == "external"
    assert task["scene_id"] == "scene-refill-rate-first"
    assert task["window"] == {"start": "2026-10-05", "end": "2026-10-11"}
    assert task["source"]["kind"] == "discovered"
    assert (tdir / "data" / "material_refill_T.json").is_file()
    assert (tdir / "data" / "material_refill_T30.json").is_file()
    status = json.loads((tdir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "created"
    assert status["data_ready"] is True
    assert (tdir / "loop" / "decision.json").is_file()
    assert (tdir / "discovery.json").is_file()
    rows = _ledger("task_created", task_id)
    assert rows and rows[0]["source"] == "discovered"
    assert rows[0]["scene_id"] == "scene-refill-rate-first"


def test_same_data_never_creates_second_task(tmp_path, capsys):
    root, _ = _layout(tmp_path)
    assert _run(root, "--apply", "--no-push") == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["created"]
    assert _run(root, "--apply", "--no-push") == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["created"] == []
    assert len(_task_dirs(root)) == 1
    assert first


def test_changed_metric_values_create_new_task(tmp_path, capsys):
    root, runs = _layout(tmp_path)
    assert _run(root, "--apply", "--no-push") == 0
    capsys.readouterr()
    _write_dataset(runs, "material_refill_T", [5.1, 5.2, 5.6, 6.0, 6.4, 7.0, 7.6], T_START)
    assert _run(root, "--apply", "--no-push") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(out["created"]) == 1
    assert len(_task_dirs(root)) == 2


def test_within_band_creates_nothing(tmp_path, capsys):
    root, _ = _layout(tmp_path, t_rates=[4.9, 5.0, 5.1, 4.95, 5.05, 5.0, 5.02])
    assert _run(root, "--apply", "--no-push") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["hits"] == []
    assert _task_dirs(root) == []


def test_pack_threshold_drives_judgement(tmp_path, capsys):
    """把包阈值抬到 T 均值之上：同数据必须不再命中（证明阈值不是代码常量）。"""
    root, _ = _layout(tmp_path)
    pack = json.loads(PACK.read_text(encoding="utf-8-sig"))
    pack["thresholds"]["refill_baseline_pct"] = 7.5
    pack_path = tmp_path / "pack-high.json"
    pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    assert discover_recheck.main(["--root", str(root), "--runs", str(root / "runs"),
                                  "--pack", str(pack_path), "--apply", "--no-push"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["hits"] == []
    assert _task_dirs(root) == []


def _fake_gateway(tmp_path):
    gw = tmp_path / "fake_lark_send.py"
    gw.write_text(
        "import json, os, pathlib, sys\n"
        "log = pathlib.Path(os.environ['FAKE_GW_LOG'])\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "log.write_text(json.dumps({'args': sys.argv[1:]}, ensure_ascii=False), encoding='utf-8')\n"
        "if os.environ.get('FAKE_GW_FAIL') == '1':\n"
        "    print('{\"code\": 1, \"msg\": \"boom\"}')\n"
        "    sys.exit(1)\n"
        "print('{\"code\": 0, \"message_id\": \"om_test_1\"}')\n",
        encoding="utf-8")
    return gw


def test_push_failure_then_retry_same_task(tmp_path, capsys, monkeypatch):
    root, _ = _layout(tmp_path)
    gw = _fake_gateway(tmp_path)
    log = tmp_path / "gw.json"
    monkeypatch.setenv("LARK_SEND", str(gw))
    monkeypatch.setenv("FAKE_GW_LOG", str(log))
    monkeypatch.setenv("FAKE_GW_FAIL", "1")

    assert _run(root, "--apply") == 3
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    task_id = out["created"][0]
    tdir = root / "runs" / task_id
    assert not (tdir / discover_recheck.MARK_FILE).exists()
    state = json.loads((root / ".telemetry" / "recheck-state.json").read_text(encoding="utf-8"))
    assert list(state["entries"].values())[0]["push_ok"] is False
    assert _ledger("push_sent", task_id)[0]["ok"] is False

    # 同一份数据第二次跑：不新建任务，只续推
    monkeypatch.delenv("FAKE_GW_FAIL")
    assert _run(root, "--apply") == 0
    out2 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out2["created"] == []
    assert out2["pushed"] == [task_id]
    assert len(_task_dirs(root)) == 1
    assert (tdir / discover_recheck.MARK_FILE).is_file()
    assert _ledger("push_sent", task_id)[-1]["ok"] is True
    sent = json.loads(log.read_text(encoding="utf-8"))["args"]
    assert "--card" in sent and ("discover-%s" % task_id) in sent
    card = json.loads(sent[sent.index("--card") + 1])
    letters = [b["behaviors"][0]["value"]["letter"]
               for col in card["body"]["elements"][-1]["columns"]
               for b in col["elements"]]
    assert letters == ["a", "b", "c"]
    assert all("task_id" in b["behaviors"][0]["value"]
               for col in card["body"]["elements"][-1]["columns"]
               for b in col["elements"])
