"""CLI entrypoint: `python -m src.main {sync,playlists,bootstrap-spotify,bootstrap-qq}`."""

from __future__ import annotations

import argparse
import os
import re
import runpy
import sys


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_path() -> str:
    return os.path.join(_repo_root(), ".env")


def _cmd_sync(args: argparse.Namespace) -> int:
    from .config import load_config
    from .sync_service import run_sync

    cfg = load_config()
    return run_sync(cfg, dry_run=bool(args.dry_run), full=bool(args.full))


def _run_script(rel_path: str) -> int:
    script = os.path.join(_repo_root(), rel_path)
    if not os.path.exists(script):
        print(f"ERROR: script not found: {script}", file=sys.stderr)
        return 1
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _cmd_bootstrap_spotify(_args: argparse.Namespace) -> int:
    return _run_script("scripts/bootstrap_spotify.py")


def _cmd_bootstrap_qq(_args: argparse.Namespace) -> int:
    return _run_script("scripts/bootstrap_qq_login.py")


_ENV_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=")


def _update_env_vars(path: str, updates: dict[str, str]) -> None:
    """Rewrite .env preserving comments and unrelated vars."""
    if not os.path.exists(path):
        raise RuntimeError(f".env not found at {path} — run bootstrap first.")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _ENV_LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}\n")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)


def _prompt(label: str, current: str | None) -> str:
    suffix = f" [{current}]" if current else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if current:
            return current
        print("  (required)", file=sys.stderr)


def _read_current_playlist_names() -> tuple[str | None, str | None]:
    path = _env_path()
    if not os.path.exists(path):
        return (None, None)
    sp = qq = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _ENV_LINE_RE.match(line)
            if not m:
                continue
            key = m.group(1)
            val = line.split("=", 1)[1].rstrip("\n")
            if key == "SPOTIFY_PLAYLIST_NAME":
                sp = val
            elif key == "QQ_PLAYLIST_NAME":
                qq = val
    return (sp, qq)


def _cmd_playlists(args: argparse.Namespace) -> int:
    path = _env_path()
    if not os.path.exists(path):
        print(
            f"ERROR: {path} does not exist. Create it first (cp .env.example .env).",
            file=sys.stderr,
        )
        return 1

    cur_sp, cur_qq = _read_current_playlist_names()
    sp = args.spotify or _prompt("Spotify playlist name", cur_sp)
    qq = args.qq or _prompt("QQ Music playlist name", cur_qq)

    _update_env_vars(
        path,
        {"SPOTIFY_PLAYLIST_NAME": sp, "QQ_PLAYLIST_NAME": qq},
    )
    print(f"Updated {path}:")
    print(f"  SPOTIFY_PLAYLIST_NAME={sp}")
    print(f"  QQ_PLAYLIST_NAME={qq}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify-sync",
        description="One-way daily sync from a Spotify playlist to a QQ Music playlist.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Run the sync (use --dry-run to preview).")
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute diff but do not mutate the QQ playlist.",
    )
    p_sync.add_argument(
        "--full",
        action="store_true",
        help="Full re-sync: ignore incremental snapshot and search every track.",
    )
    p_sync.set_defaults(func=_cmd_sync)

    p_pl = sub.add_parser(
        "playlists",
        help="Set Spotify + QQ playlist names in .env (interactive if omitted).",
    )
    p_pl.add_argument("-s", "--spotify", help="Spotify playlist name")
    p_pl.add_argument("-q", "--qq", help="QQ Music playlist name")
    p_pl.set_defaults(func=_cmd_playlists)

    p_sp = sub.add_parser(
        "bootstrap-spotify",
        help="Local-only: OAuth flow to obtain SPOTIFY_REFRESH_TOKEN.",
    )
    p_sp.set_defaults(func=_cmd_bootstrap_spotify)

    p_qq = sub.add_parser(
        "bootstrap-qq",
        help="Local-only: QR login flow to obtain QQ_CREDENTIAL_JSON.",
    )
    p_qq.set_defaults(func=_cmd_bootstrap_qq)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
