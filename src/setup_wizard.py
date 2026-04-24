"""Interactive setup wizard: walks the user through filling `.env`.

Invoked via `spotify-sync setup`. Auto-suggested by `main.py` when the
config loader raises ConfigError — so the user never sees a raw traceback.

Each step:
1. Explain what's needed + open any helpful URL.
2. Run the actual bootstrap (OAuth / QR) when possible.
3. Write back to `.env` as soon as a value is captured (never re-ask
   for a key already present in `.env`).
"""

from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from pathlib import Path

_ENV_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_path() -> Path:
    return _project_root() / ".env"


def _example_path() -> Path:
    return _project_root() / ".env.example"


def _read_env() -> dict[str, str]:
    path = _env_path()
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ENV_LINE_RE.match(line)
        if m:
            out[m.group(1)] = line.split("=", 1)[1]
    return out


def _bootstrap_env_from_example() -> None:
    """Ensure `.env` exists so we have something to append to."""
    path = _env_path()
    if path.exists():
        return
    example = _example_path()
    if example.exists():
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        path.write_text("", encoding="utf-8")


def _write_env(updates: dict[str, str]) -> None:
    """Merge updates into `.env`, preserving order + comments."""
    path = _env_path()
    _bootstrap_env_from_example()
    remaining = dict(updates)
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        m = _ENV_LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default:
            return default
        print("  （不能为空 / required）", file=sys.stderr)


def _prompt_yesno(label: str, default: bool = True) -> bool:
    dtxt = "Y/n" if default else "y/N"
    raw = input(f"{label} ({dtxt}): ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _section(num: int, total: int, title: str) -> None:
    bar = "═" * 60
    print(f"\n{bar}\n  第 {num}/{total} 步  ·  {title}\n{bar}")


def _spotify_app_step(env: dict[str, str]) -> None:
    have_id = bool(env.get("SPOTIFY_CLIENT_ID"))
    have_secret = bool(env.get("SPOTIFY_CLIENT_SECRET"))
    if have_id and have_secret:
        return

    _section(1, 4, "Spotify 应用凭证")
    print(
        """
去 https://developer.spotify.com/dashboard 登录 → 点 Create app：

    App name          : 随便填，比如 spotify-sync
    Redirect URIs     : http://127.0.0.1:8765/callback    ← 必须一字不差
    Which API/SDKs    : 勾 Web API
    保存 → 点进应用 → 左上角 Settings → 复制 Client ID 和 Client secret
"""
    )
    if _prompt_yesno("帮你在浏览器打开 Spotify dashboard 吗？", True):
        webbrowser.open("https://developer.spotify.com/dashboard")

    if not have_id:
        env["SPOTIFY_CLIENT_ID"] = _prompt("粘贴 Client ID")
    if not have_secret:
        env["SPOTIFY_CLIENT_SECRET"] = _prompt("粘贴 Client secret")
    _write_env(
        {
            "SPOTIFY_CLIENT_ID": env["SPOTIFY_CLIENT_ID"],
            "SPOTIFY_CLIENT_SECRET": env["SPOTIFY_CLIENT_SECRET"],
        }
    )
    print("\n✓ 写入 .env")


def _spotify_oauth_step(env: dict[str, str]) -> None:
    if env.get("SPOTIFY_REFRESH_TOKEN"):
        return

    _section(2, 4, "Spotify 授权")
    print(
        """
接下来浏览器会打开 Spotify 授权页。
直接点 Agree 即可 — 终端会自动接住 refresh token。
"""
    )
    input("按回车开始... ")
    from .spotify_oauth import fetch_refresh_token

    try:
        token = fetch_refresh_token(
            env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_CLIENT_SECRET"]
        )
    except Exception as exc:
        raise RuntimeError(
            f"OAuth 失败: {exc}\n"
            "检查 Redirect URI 是否正好是 http://127.0.0.1:8765/callback"
        ) from exc

    env["SPOTIFY_REFRESH_TOKEN"] = token
    _write_env({"SPOTIFY_REFRESH_TOKEN": token})
    print("\n✓ refresh token 拿到，写入 .env")


def _qq_login_step(env: dict[str, str]) -> None:
    if env.get("QQ_CREDENTIAL_JSON"):
        return

    _section(3, 4, "QQ 音乐登录")
    print(
        """
接下来终端会出一个二维码。
用 **手机 QQ**（聊天那个，不是 QQ 音乐）扫码 → 点确认。
QQ 账号即 QQ 音乐账号。
"""
    )
    input("按回车启动 QR 登录... ")
    from .qq_qr_login import fetch_credential

    try:
        cred = fetch_credential()
    except Exception as exc:
        raise RuntimeError(f"QQ QR 登录失败: {exc}") from exc

    blob = json.dumps(cred, ensure_ascii=False)
    env["QQ_CREDENTIAL_JSON"] = blob
    _write_env({"QQ_CREDENTIAL_JSON": blob})
    print("\n✓ QQ 凭证拿到，写入 .env")


def _playlist_names_step(env: dict[str, str]) -> None:
    if env.get("SPOTIFY_PLAYLIST_NAME") and env.get("QQ_PLAYLIST_NAME"):
        return

    _section(4, 4, "选歌单")

    # Spotify side: list user playlists + let them pick by number
    if not env.get("SPOTIFY_PLAYLIST_NAME"):
        print("拉取你的 Spotify 歌单...")
        from .spotify_client import SpotifyClient

        try:
            sp = SpotifyClient(
                env["SPOTIFY_CLIENT_ID"],
                env["SPOTIFY_CLIENT_SECRET"],
                env["SPOTIFY_REFRESH_TOKEN"],
            )
            playlists = sp.list_playlists()
        except Exception as exc:
            print(f"(Spotify 列表拉取失败: {exc})", file=sys.stderr)
            playlists = []

        if playlists:
            print("\n你的 Spotify 歌单：")
            for i, p in enumerate(playlists, 1):
                tracks_total = (p.get("tracks") or {}).get("total", "?")
                print(f"  [{i:>2}]  {p.get('name')}   ({tracks_total} tracks)")
            while True:
                pick = input("\n选 Spotify 歌单 — 输入编号或直接输歌单名: ").strip()
                if pick.isdigit():
                    idx = int(pick) - 1
                    if 0 <= idx < len(playlists):
                        env["SPOTIFY_PLAYLIST_NAME"] = playlists[idx]["name"]
                        break
                elif pick:
                    env["SPOTIFY_PLAYLIST_NAME"] = pick
                    break
                print("  请再选一次。")
        else:
            env["SPOTIFY_PLAYLIST_NAME"] = _prompt("Spotify 源歌单名")

    # QQ side: just prompt (auto-create if missing)
    if not env.get("QQ_PLAYLIST_NAME"):
        default_qq = env.get("SPOTIFY_PLAYLIST_NAME") or None
        env["QQ_PLAYLIST_NAME"] = _prompt(
            "QQ 音乐目标歌单名（不存在会自动新建）", default_qq
        )

    _write_env(
        {
            "SPOTIFY_PLAYLIST_NAME": env["SPOTIFY_PLAYLIST_NAME"],
            "QQ_PLAYLIST_NAME": env["QQ_PLAYLIST_NAME"],
        }
    )
    print("\n✓ 歌单名写入 .env")


def run() -> int:
    print(
        "\n🧩 setup 向导 — 一步步带你配好 .env。"
        "\n   已经填过的项会自动跳过；想重配哪项，先去 .env 里删掉那行再跑。\n"
    )
    env = _read_env()
    # Re-check env after each step so subsequent steps see fresh values.
    try:
        _spotify_app_step(env)
        env = _read_env()
        _spotify_oauth_step(env)
        env = _read_env()
        _qq_login_step(env)
        env = _read_env()
        _playlist_names_step(env)
    except RuntimeError as exc:
        print(f"\n❌ 向导中断: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n\n向导中断（Ctrl-C）。已填的部分已保存在 .env，可以再跑一次继续。\n")
        return 130

    print(
        "\n✅ 全部配置就位！\n"
        "   下一步：\n"
        "     spotify-sync sync --dry-run    # 预演一次\n"
        "     spotify-sync sync               # 真同步\n"
    )
    return 0
