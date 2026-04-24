"""Human-readable run outputs: unmatched.txt, sync.log, stdout summary."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_unmatched_txt(path: str, items: Iterable[dict[str, Any]]) -> int:
    """Overwrite file with one `artist 《title》` per line. Returns row count."""
    _ensure_parent(path)
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            title = (item.get("title") or "").strip()
            artist = (item.get("artist") or "").strip()
            f.write(f"{artist} 《{title}》\n")
            count += 1
    return count


def append_sync_log(path: str, summary: dict[str, Any]) -> None:
    """Append one readable block per run, ending in a blank separator line."""
    _ensure_parent(path)
    ts = summary.get("timestamp") or _now_iso()
    lines = [f"=== sync run @ {ts} ==="]
    for key in (
        "run_id",
        "status",
        "added_count",
        "removed_count",
        "skipped_count",
        "failed_count",
        "notes",
    ):
        if key in summary:
            lines.append(f"{key}: {summary[key]}")
    # Include any extra keys (e.g., spotify_count, qq_count) after known ones.
    known = {
        "timestamp",
        "run_id",
        "status",
        "added_count",
        "removed_count",
        "skipped_count",
        "failed_count",
        "notes",
    }
    for key, value in summary.items():
        if key not in known:
            lines.append(f"{key}: {value}")
    lines.append("")  # trailing blank
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_summary(summary: dict[str, Any]) -> None:
    """Pretty stdout output suitable for CI logs."""
    ts = summary.get("timestamp") or _now_iso()
    status = summary.get("status", "unknown")
    print(f"[sync] {ts} status={status}")
    for key in (
        "run_id",
        "added_count",
        "removed_count",
        "skipped_count",
        "failed_count",
    ):
        if key in summary:
            print(f"[sync]   {key}={summary[key]}")
    notes = summary.get("notes")
    if notes:
        print(f"[sync]   notes={notes}")
