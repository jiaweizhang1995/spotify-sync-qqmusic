"""Mirror-mode diff + safety threshold. Pure set math."""

from __future__ import annotations

from typing import Iterable


def compute_mirror_diff(
    target_qq_ids: Iterable, current_qq_ids: Iterable
) -> dict[str, set]:
    target = set(target_qq_ids)
    current = set(current_qq_ids)
    return {
        "to_add": target - current,
        "to_remove": current - target,
    }


def safety_check(
    diff: dict, current_count: int, threshold: float
) -> tuple[bool, str]:
    """Abort if mass-delete would exceed threshold of current playlist.

    First sync (current_count == 0) always passes — there's nothing to lose.
    """
    to_remove = diff.get("to_remove") or set()
    remove_count = len(to_remove)

    if current_count <= 0:
        return True, "first-sync bypass"

    ratio = remove_count / max(1, current_count)
    if ratio > threshold:
        return (
            False,
            f"threshold exceeded: would remove {remove_count}/{current_count} "
            f"({ratio:.2%} > {threshold:.2%})",
        )
    return True, f"ok: remove {remove_count}/{current_count} ({ratio:.2%})"
