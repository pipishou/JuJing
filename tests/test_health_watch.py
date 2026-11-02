#!/usr/bin/env python3
"""Tests for tools/health_watch.py.

Runs standalone (``python tests/test_health_watch.py``) because the project
venv does not ship pytest, and is also pytest-collectable.

All fixtures are temporary log files; the real ``.octop`` directory is never
touched.
"""

import contextlib
import io
import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import health_watch as hw  # noqa: E402

NOW = datetime(2026, 11, 2, 10, 0, 0)


def _write_log(directory, lines):
    path = Path(directory) / "octop.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _line(ts, message):
    return "%s.123 %s" % (ts.strftime("%Y-%m-%d %H:%M:%S"), message)


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hw.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_normal_window_is_ok():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(
            tmp,
            [
                _line(NOW - timedelta(hours=2), "Gateway booted in 1.2s"),
                _line(NOW - timedelta(hours=1), "proactive scheduler tick"),
            ],
        )
        result = hw.analyze(path, 24, NOW)
        assert result["boots"] == 1, result
        assert result["io_errors"] == 0, result
        assert result["verdict"] == "ok", result
        assert result["last_boot"] == (
            NOW - timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        code, out, _ = _run_main(
            ["--log", str(path), "--hours", "24", "--json", "--now", NOW.isoformat()]
        )
        assert code == 0, code
        payload = json.loads(out)
        assert list(payload) == [
            "window_hours",
            "boots",
            "io_errors",
            "last_boot",
            "verdict",
        ]
        assert payload["verdict"] == "ok"


def test_boot_storm_alerts():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(
            tmp,
            [
                _line(NOW - timedelta(minutes=m), "Gateway booted")
                for m in (50, 35, 20, 5)
            ],
        )
        result = hw.analyze(path, 1, NOW)
        assert result["boots"] == 4, result
        assert result["verdict"] == "alert", result

        code, out, _ = _run_main(
            ["--log", str(path), "--hours", "1", "--json", "--now", NOW.isoformat()]
        )
        assert code == 2, code
        assert json.loads(out)["verdict"] == "alert"


def test_io_error_alerts_even_without_storm():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(
            tmp,
            [
                _line(NOW - timedelta(hours=3), "Gateway booted"),
                _line(
                    NOW - timedelta(hours=2),
                    "memory.maintenance done gc_err=OperationalError: disk I/O error",
                ),
            ],
        )
        result = hw.analyze(path, 24, NOW)
        assert result["boots"] == 1, result
        assert result["io_errors"] == 1, result
        assert result["verdict"] == "alert", result


def test_missing_log_is_warn_and_exit_3():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "does-not-exist.log"
        result = hw.analyze(missing, 24, NOW)
        assert result == {
            "window_hours": 24,
            "boots": 0,
            "io_errors": 0,
            "last_boot": None,
            "verdict": "warn",
        }, result

        code, out, err = _run_main(
            [
                "--log",
                str(missing),
                "--hours",
                "24",
                "--json",
                "--now",
                NOW.isoformat(),
            ]
        )
        assert code == 3, code
        assert json.loads(out)["verdict"] == "warn"
        assert "not found" in err


def test_bad_lines_are_tolerated():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(
            tmp,
            [
                "this line has no timestamp and mentions disk I/O error",
                "2026-13-45 99:99:99 Gateway booted",  # regex passes, datetime fails
                _line(NOW - timedelta(days=3), "Gateway booted"),  # outside window
                _line(NOW - timedelta(minutes=10), "Gateway booted"),
                "    sqlite3.OperationalError: disk I/O error",  # traceback tail
                _line(NOW + timedelta(hours=2), "Gateway booted"),  # future
            ],
        )
        result = hw.analyze(path, 24, NOW)
        assert result["boots"] == 1, result
        assert result["io_errors"] == 0, result
        assert result["verdict"] == "ok", result


if __name__ == "__main__":
    failed = 0
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name, test in tests:
        try:
            test()
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
        else:
            print("ok   %s" % name)
    print("%d passed, %d failed" % (len(tests) - failed, failed))
    sys.exit(1 if failed else 0)
