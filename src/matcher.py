"""Normalization + scoring to match Spotify tracks against QQ Music candidates.

Pure logic, stdlib only. See plan §Normalization and §Match loop.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_SUFFIX_TOKEN = (
    r"remaster(?:ed)?(?:\s*\d{2,4})?"
    r"|deluxe(?:\s+edition)?"
    r"|live(?:\s+(?:version|at|from|in)[^)\]）】]*)?"
    r"|radio\s*edit"
    r"|\d{2,4}\s*(?:re\-?)?(?:mix|edit|version|remaster(?:ed)?)"
    r"|(?:re\-?)?mix"
    r"|single\s*version"
    r"|acoustic(?:\s+version)?"
    r"|explicit"
    r"|clean(?:\s+version)?"
    r"|mono|stereo"
    r"|bonus\s*track"
    r"|feat\.?[^)\]）】]*"
    r"|ft\.?[^)\]）】]*"
)

# Bracketed suffix: (remastered 2011), [Live at X], （feat. X）, 【Deluxe】
_BRACKET_SUFFIX_RE = re.compile(
    r"\s*[\(\[（【]\s*(?:" + _SUFFIX_TOKEN + r")\s*[\)\]）】]\s*",
    re.IGNORECASE,
)

# Dash suffix: " - Remastered 2011", " - Live at ...", " - 2019 Mix", " - feat. X"
_DASH_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(?:" + _SUFFIX_TOKEN + r").*$",
    re.IGNORECASE,
)

# Bare trailing "feat./ft. X" without brackets or dash
_BARE_FEAT_RE = re.compile(
    r"\s*(?:feat\.?|ft\.?)\s+[^()\[\]（）【】]*$",
    re.IGNORECASE,
)

_WS_RE = re.compile(r"\s+")

# Artist split delimiters per plan.
_ARTIST_SPLIT_RE = re.compile(r"[,&、/]|\s+feat\.?\s+|\s+ft\.?\s+", re.IGNORECASE)


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def normalize_title(s: str) -> str:
    if not s:
        return ""
    s = _nfkc(s)
    # Strip bracketed suffixes repeatedly — tracks can have multiple: "Song (Remaster) (Live)".
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_SUFFIX_RE.sub(" ", s)
    s = _DASH_SUFFIX_RE.sub("", s)
    s = _BARE_FEAT_RE.sub("", s)
    s = s.lower()
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_artist(s: str) -> str:
    if not s:
        return ""
    s = _nfkc(s)
    parts = _ARTIST_SPLIT_RE.split(s)
    primary = parts[0] if parts else s
    return _WS_RE.sub(" ", primary).strip().lower()


def _artist_names(entry: Any) -> list[str]:
    """Accept either a list[str] or list[{name: ...}] or a single string."""
    if entry is None:
        return []
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        name = entry.get("name")
        return [name] if name else []
    if isinstance(entry, Iterable):
        out: list[str] = []
        for a in entry:
            if isinstance(a, str):
                out.append(a)
            elif isinstance(a, dict) and a.get("name"):
                out.append(a["name"])
        return out
    return []


def _get_isrc(track: Any) -> str | None:
    if not isinstance(track, dict):
        return None
    isrc = track.get("isrc")
    if isrc:
        return str(isrc).strip().upper() or None
    ext = track.get("external_ids")
    if isinstance(ext, dict):
        v = ext.get("isrc")
        if v:
            return str(v).strip().upper() or None
    return None


def _get_duration_ms(track: Any) -> int | None:
    if not isinstance(track, dict):
        return None
    for k in ("duration_ms", "duration", "interval"):
        if k in track and track[k] is not None:
            try:
                v = int(track[k])
            except (TypeError, ValueError):
                continue
            # QQ exposes seconds under `interval` (raw API) and also under
            # `duration` (our normalized surface via qqmusic_client). Anything
            # under 10_000 is assumed to be seconds and converted to ms.
            if k in ("interval", "duration") and v < 10000:
                v *= 1000
            return v
    return None


def score_candidate(sp_track: dict, qq_cand: dict) -> tuple[float, str]:
    """Return (score, method) in [0.0, 1.0]. ISRC equality forces 1.0/'isrc'."""
    sp_isrc = _get_isrc(sp_track)
    qq_isrc = _get_isrc(qq_cand)
    if sp_isrc and qq_isrc and sp_isrc == qq_isrc:
        return 1.0, "isrc"

    score = 0.0
    reasons: list[str] = []

    sp_title_n = normalize_title(sp_track.get("name") or sp_track.get("title") or "")
    qq_title_n = normalize_title(qq_cand.get("name") or qq_cand.get("title") or "")
    if sp_title_n and sp_title_n == qq_title_n:
        score += 0.4
        reasons.append("title")

    sp_artists = {
        normalize_artist(a)
        for a in _artist_names(sp_track.get("artists") or sp_track.get("artist"))
        if a
    }
    qq_artists = {
        normalize_artist(a)
        for a in _artist_names(qq_cand.get("artists") or qq_cand.get("singer") or qq_cand.get("artist"))
        if a
    }
    sp_artists.discard("")
    qq_artists.discard("")
    if sp_artists and qq_artists and (sp_artists & qq_artists):
        score += 0.2
        reasons.append("artist")

    sp_dur = _get_duration_ms(sp_track)
    qq_dur = _get_duration_ms(qq_cand)
    if sp_dur is not None and qq_dur is not None and abs(sp_dur - qq_dur) <= 3000:
        score += 0.4
        reasons.append("duration")

    method = "+".join(reasons) if reasons else "none"
    return score, method


def pick_best(
    sp_track: dict, candidates: list[dict], threshold: float = 0.8
) -> tuple[dict | None, float, str]:
    """Pick best candidate; return (None, score, method) if below threshold."""
    best_cand: dict | None = None
    best_score = 0.0
    best_method = "none"
    for cand in candidates or []:
        score, method = score_candidate(sp_track, cand)
        if score > best_score:
            best_score = score
            best_method = method
            best_cand = cand
            if score >= 1.0:
                break
    if best_cand is None or best_score < threshold:
        return None, best_score, best_method
    return best_cand, best_score, best_method
