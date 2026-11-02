#!/usr/bin/env python3
"""Octop gateway stability watcher (zero dependencies).

Reads the octop application log and counts, inside a sliding time window:

  * ``Gateway booted`` records   -> gateway starts (a restart storm shows up here)
  * ``disk I/O error`` records   -> SQLite I/O failures (octop_memory GC/vacuum)

Only timestamped log records are counted. Traceback continuation lines repeat
the exception text ("disk I/O error") without their own timestamp, so counting
every textual occurrence would inflate the number. The raw occurrence count is
reported separately by ``count_text_occurrences`` for reconciliation.

Verdict rules (per hour inside the window):

  * ``alert`` : io_errors > 0, or boots/hour > 3
  * ``warn``  : boots/hour > 1, or the log file is missing/unreadable
  * ``ok``    : otherwise

CLI::

    python health_watch.py --log PATH --hours 24 --json
    python health_watch.py --hours 2                # human readable

Exit codes: 0 ok, 1 warn, 2 alert, 3 log missing/unreadable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BOOT_MARKER = "Gateway booted"
IO_MARKER = "disk I/O error"
DEFAULT_HOURS = 24.0
BOOTS_PER_HOUR_ALERT = 3.0
BOOTS_PER_HOUR_WARN = 1.0

# Log records look like "2026-11-02 09:50:12.345 ...". A UTF-8 BOM may sit on
# the first line; the optional group keeps that line countable.
_TS_RE = re.compile(
    r"^\ufeff?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})"
)


def _default_log() -> Path:
    """Return the run-root log path, with a cwd fallback.

    tools/health_watch.py -> bot-team -> JuJing -> bot-2 (parents[3]).
    """
    here = Path(__file__).resolve()
    if len(here.parents) > 3:
        candidate = here.parents[3] / ".octop" / "logs" / "octop.log"
        if candidate.exists():
            return candidate
    return Path.cwd() / ".octop" / "logs" / "octop.log"


def parse_timestamp(line: str):
    """Return the record timestamp of *line*, or None if it has none/invalid."""
    match = _TS_RE.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group(1) + " " + match.group(2), "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return None


def count_text_occurrences(path, hours=24.0, now=None) -> int:
    """Count raw ``disk I/O error`` occurrences in timestamped records.

    A timestamped record may mention the phrase more than once (logger message
    plus embedded exception text); this counts every occurrence inside counted
    records only, so it is an upper bound for the human-readable line count.
    """
    path = Path(path)
    if not path.exists():
        return 0
    ref = now or datetime.now()
    cutoff = ref - timedelta(hours=hours)
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            ts = parse_timestamp(line)
            if ts is None or ts < cutoff or ts > ref:
                continue
            if IO_MARKER in line:
                total += line.count(IO_MARKER)
    return total


def analyze(path, hours=DEFAULT_HOURS, now=None) -> dict:
    """Analyze *path* and return the result dict (JSON-ready).

    Missing or unreadable logs yield ``verdict: "warn"`` with zero counters so
    callers still get a valid payload; ``main`` maps that to exit code 3.
    """
    if hours <= 0:
        raise ValueError("hours must be > 0")

    path = Path(path)
    ref = now or datetime.now()
    cutoff = ref - timedelta(hours=hours)

    boots = 0
    io_errors = 0
    last_boot = None

    if path.exists():
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    ts = parse_timestamp(line)
                    if ts is None or ts < cutoff or ts > ref:
                        continue
                    if BOOT_MARKER in line:
                        boots += 1
                        if last_boot is None or ts > last_boot:
                            last_boot = ts
                    if IO_MARKER in line:
                        io_errors += 1
        except OSError:
            boots = 0
            io_errors = 0
            last_boot = None

    if not path.exists():
        verdict = "warn"
    else:
        rate = boots / float(hours)
        if io_errors > 0 or rate > BOOTS_PER_HOUR_ALERT:
            verdict = "alert"
        elif rate > BOOTS_PER_HOUR_WARN:
            verdict = "warn"
        else:
            verdict = "ok"

    window_hours = int(hours) if float(hours).is_integer() else float(hours)
    return {
        "window_hours": window_hours,
        "boots": boots,
        "io_errors": io_errors,
        "last_boot": last_boot.strftime("%Y-%m-%dT%H:%M:%S") if last_boot else None,
        "verdict": verdict,
    }


def render_human(result: dict, log_path: Path) -> str:
    rate = result["boots"] / float(result["window_hours"])
    lines = [
        "octop health watch",
        "  log:         %s" % log_path,
        "  window:      %sh" % result["window_hours"],
        "  boots:       %d (%.2f/h)" % (result["boots"], rate),
        "  io_errors:   %d (timestamped records)" % result["io_errors"],
        "  last_boot:   %s" % (result["last_boot"] or "-"),
        "  verdict:     %s" % result["verdict"].upper(),
    ]
    if not Path(log_path).exists():
        lines.append("  note:        log file not found - nothing to analyze")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch the octop log for gateway restart storms and SQLite I/O errors."
    )
    parser.add_argument(
        "--log",
        default=str(_default_log()),
        help="path to octop.log (default: <run-root>/.octop/logs/octop.log)",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=DEFAULT_HOURS,
        help="sliding window in hours (default: 24)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON only"
    )
    parser.add_argument(
        "--now",
        default=None,
        help="reference time, ISO-8601 (default: current local time; for tests)",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be > 0")

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            parser.error("--now must be ISO-8601, e.g. 2026-11-02T10:00:00")

    log_path = Path(args.log)
    missing = not log_path.exists()

    result = analyze(log_path, args.hours, now)

    if missing:
        print("health_watch: log not found: %s" % log_path, file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(render_human(result, log_path))

    if missing:
        return 3
    return {"ok": 0, "warn": 1, "alert": 2}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
