"""Log retention: stale research logs are purged on startup, fresh ones kept."""

import os
from datetime import datetime, timedelta

from execution_agent.cleanup import purge_old_logs


def _touch(path, age_days, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    ts = (now - timedelta(days=age_days)).timestamp()
    os.utime(path, (ts, ts))


def test_purge_removes_only_files_older_than_a_week(tmp_path):
    now = datetime(2026, 11, 14, 12, 0, 0)
    results = tmp_path / "logs"

    stale = results / "INTC" / "TradingAgentsStrategy_logs" / "full_states_log_2026-11-06.json"
    fresh = results / "NVDA" / "TradingAgentsStrategy_logs" / "full_states_log_2026-11-13.json"
    _touch(stale, age_days=8, now=now)
    _touch(fresh, age_days=2, now=now)

    removed = purge_old_logs(results, now=now)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


def test_purge_prunes_empty_directories(tmp_path):
    now = datetime(2026, 11, 14, 12, 0, 0)
    results = tmp_path / "logs"
    stale = results / "WDC" / "2026-11-05" / "message_tool.log"
    _touch(stale, age_days=10, now=now)

    purge_old_logs(results, now=now)

    assert not (results / "WDC").exists()  # empty parent chain pruned too


def test_purge_keeps_dir_that_still_holds_a_fresh_file(tmp_path):
    now = datetime(2026, 11, 14, 12, 0, 0)
    results = tmp_path / "logs"
    stale = results / "T" / "old.json"
    fresh = results / "T" / "new.json"
    _touch(stale, age_days=9, now=now)
    _touch(fresh, age_days=1, now=now)

    removed = purge_old_logs(results, now=now)

    assert removed == 1
    assert fresh.exists()  # the directory survives because a fresh file remains


def test_purge_respects_custom_retention(tmp_path):
    now = datetime(2026, 11, 14, 12, 0, 0)
    results = tmp_path / "logs"
    oldish = results / "A" / "log.json"
    _touch(oldish, age_days=5, now=now)

    assert purge_old_logs(results, now=now, max_age_days=7) == 0  # 5 days < a week
    assert purge_old_logs(results, now=now, max_age_days=3) == 1  # 5 days > 3


def test_purge_missing_dir_is_noop(tmp_path):
    assert purge_old_logs(tmp_path / "nope", now=datetime(2026, 11, 14)) == 0
