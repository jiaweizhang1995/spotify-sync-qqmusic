"""Environment-driven configuration with fail-fast validation."""

from __future__ import annotations

import os
from dataclasses import dataclass


_REQUIRED = (
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REFRESH_TOKEN",
    "SPOTIFY_PLAYLIST_NAME",
    "QQ_PLAYLIST_NAME",
    "QQ_CREDENTIAL_JSON",
)


@dataclass(frozen=True)
class Config:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    spotify_playlist_name: str
    qq_playlist_name: str
    qq_credential_json: str
    gh_pat_secrets_write: str | None = None
    mirror_delete_threshold: float = 0.2
    db_path: str = "data/sync.db"
    log_path: str = "data/sync.log"
    unmatched_path: str = "data/unmatched.txt"


def _load_dotenv_if_present() -> None:
    """Attempt to load `.env` via python-dotenv; silently skip if unavailable."""
    if not os.path.exists(".env"):
        return
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    load_dotenv(".env", override=False)


def load_config() -> Config:
    _load_dotenv_if_present()

    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    threshold_raw = os.environ.get("MIRROR_DELETE_THRESHOLD", "0.2")
    try:
        threshold = float(threshold_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"MIRROR_DELETE_THRESHOLD must be a float, got {threshold_raw!r}"
        ) from exc

    return Config(
        spotify_client_id=os.environ["SPOTIFY_CLIENT_ID"],
        spotify_client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        spotify_refresh_token=os.environ["SPOTIFY_REFRESH_TOKEN"],
        spotify_playlist_name=os.environ["SPOTIFY_PLAYLIST_NAME"],
        qq_playlist_name=os.environ["QQ_PLAYLIST_NAME"],
        qq_credential_json=os.environ["QQ_CREDENTIAL_JSON"],
        gh_pat_secrets_write=os.environ.get("GH_PAT_SECRETS_WRITE") or None,
        mirror_delete_threshold=threshold,
        db_path=os.environ.get("DB_PATH", "data/sync.db"),
        log_path=os.environ.get("LOG_PATH", "data/sync.log"),
        unmatched_path=os.environ.get("UNMATCHED_PATH", "data/unmatched.txt"),
    )
