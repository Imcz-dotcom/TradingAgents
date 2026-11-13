"""Delete stale research logs so the results directory doesn't grow forever.

Every research run writes a JSON snapshot (and, via the CLI, a message log and
markdown reports) under ``results_dir`` — and nothing prunes them. On startup the
serve bot calls :func:`purge_old_logs` to drop anything older than a week. The
reports are only useful for a few days, and a stale ticker's report should never
be mistaken for a fresh one.

Scope is deliberately ``results_dir`` only. The memory log (``memory/``) and the
execution audit (``execution/audit.jsonl``) live elsewhere and are never touched
here — deleting those would erase the agent's long-term lessons and its trade
history, which is not what "log cleanup" means.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def purge_old_logs(
    results_dir, *, max_age_days: int = 7, now: datetime | None = None
) -> int:
    """Delete files under ``results_dir`` older than ``max_age_days``, then prune
    directories that became empty. Returns the number of files removed.

    Age is measured by each file's last-modified time, so every artifact — the
    JSON snapshots, CLI ``message_tool.log`` transcripts, and report markdown —
    is covered regardless of how it is named. ``now`` is injectable for tests and
    must be a naive ``datetime`` in local time (as ``datetime.now()`` is); it is
    compared to ``st_mtime`` as epoch seconds, so naive/aware never mixes.
    """
    results_dir = Path(results_dir).expanduser()
    if not results_dir.is_dir():
        return 0
    now = now or datetime.now()
    cutoff = (now - timedelta(days=max_age_days)).timestamp()

    removed = 0
    for path in results_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue

    # Remove directories that are now empty, deepest first. rmdir only succeeds
    # on an empty directory, so anything still holding a fresh file is kept.
    dirs = sorted(
        (p for p in results_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for path in dirs:
        try:
            path.rmdir()
        except OSError:
            pass
    return removed
