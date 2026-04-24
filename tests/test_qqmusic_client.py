"""Unit tests for qqmusic_client sync facade.

Mocks `qqmusic_api.Client` + `Credential` at the `src.qqmusic_client` import
site so no real network calls are ever made.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import qqmusic_client as qc  # noqa: E402


def _make_credential(musickey: str = "K_OLD", musicid: int = 42) -> MagicMock:
    cred = MagicMock()
    cred.musickey = musickey
    cred.musicid = musicid
    cred.model_dump.return_value = {"musicid": musicid, "musickey": musickey}
    return cred


def _make_client_mock(**overrides) -> MagicMock:
    """Build a mock `Client` whose async methods are pre-wired."""
    client = MagicMock()
    client.close = AsyncMock()
    client.login.check_expired = AsyncMock(return_value=False)
    client.login.refresh_credential = AsyncMock()
    client.user.get_created_songlist = AsyncMock()
    client.songlist.create = AsyncMock()
    client.songlist.get_detail = AsyncMock()
    client.songlist.add_songs = AsyncMock(return_value=True)
    client.songlist.del_songs = AsyncMock(return_value=True)
    client.search.search_by_type = AsyncMock()
    for key, value in overrides.items():
        attr = client
        parts = key.split(".")
        for p in parts[:-1]:
            attr = getattr(attr, p)
        setattr(attr, parts[-1], value)
    return client


def test_load_and_dump_credential_roundtrip():
    blob = json.dumps({"musicid": 1, "musickey": "K_A", "openid": "oid"})
    with patch.object(qc, "Credential") as cred_cls:
        fake = MagicMock()
        fake.model_dump.return_value = {"musicid": 1, "musickey": "K_A", "openid": "oid"}
        cred_cls.model_validate.return_value = fake
        cred = qc.load_credential(blob)
        cred_cls.model_validate.assert_called_once()
        out = qc.dump_credential(cred)
        assert json.loads(out) == {"musicid": 1, "musickey": "K_A", "openid": "oid"}


def test_ensure_fresh_no_rotation_when_not_expired():
    cred = _make_credential(musickey="K_SAME")
    client = _make_client_mock()
    client.login.check_expired = AsyncMock(return_value=False)

    with patch.object(qc, "Client", return_value=client):
        result, rotated = qc.ensure_fresh(cred)

    assert result is cred
    assert rotated is False
    client.login.check_expired.assert_awaited_once()
    client.login.refresh_credential.assert_not_awaited()
    client.close.assert_awaited_once()


def test_ensure_fresh_rotates_when_expired_and_key_changes():
    old = _make_credential(musickey="K_OLD")
    new = _make_credential(musickey="K_NEW")
    client = _make_client_mock()
    client.login.check_expired = AsyncMock(return_value=True)
    client.login.refresh_credential = AsyncMock(return_value=new)

    with patch.object(qc, "Client", return_value=client):
        result, rotated = qc.ensure_fresh(old)

    assert result is new
    assert rotated is True
    client.login.refresh_credential.assert_awaited_once()


def test_ensure_fresh_no_rotation_flag_when_refresh_returns_same_key():
    old = _make_credential(musickey="K_X")
    same = _make_credential(musickey="K_X")
    client = _make_client_mock()
    client.login.check_expired = AsyncMock(return_value=True)
    client.login.refresh_credential = AsyncMock(return_value=same)

    with patch.object(qc, "Client", return_value=client):
        _, rotated = qc.ensure_fresh(old)

    assert rotated is False


def test_find_or_create_playlist_hits_existing():
    cred = _make_credential()
    playlist = SimpleNamespace(dirid=7, title="SyncTarget", songnum=3)
    resp = SimpleNamespace(playlists=[playlist])
    client = _make_client_mock()
    client.user.get_created_songlist = AsyncMock(return_value=resp)

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        result = qq.find_or_create_playlist("SyncTarget")

    assert result == {"dirid": 7, "dirname": "SyncTarget"}
    client.songlist.create.assert_not_awaited()


def test_find_or_create_playlist_miss_creates():
    cred = _make_credential()
    list_resp = SimpleNamespace(playlists=[])
    create_resp = SimpleNamespace(dirid=99, id=1234, name="NewList", retCode=0)
    client = _make_client_mock()
    client.user.get_created_songlist = AsyncMock(return_value=list_resp)
    client.songlist.create = AsyncMock(return_value=create_resp)

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        result = qq.find_or_create_playlist("NewList")

    assert result == {"dirid": 99, "dirname": "NewList"}
    client.songlist.create.assert_awaited_once()


def test_list_user_songlists_normalizes_fields():
    cred = _make_credential()
    resp = SimpleNamespace(
        playlists=[
            SimpleNamespace(dirid=1, title="A", songnum=10),
            SimpleNamespace(dirid=2, title="B", songnum=0),
        ]
    )
    client = _make_client_mock()
    client.user.get_created_songlist = AsyncMock(return_value=resp)

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        out = qq.list_user_songlists()

    assert out == [
        {"dirid": 1, "dirname": "A", "song_count": 10},
        {"dirid": 2, "dirname": "B", "song_count": 0},
    ]


def test_search_song_normalizes_and_passes_song_type():
    cred = _make_credential()
    song = SimpleNamespace(
        id=100,
        mid="m100",
        title="Hello",
        name="Hello",
        singer=[SimpleNamespace(name="Adele"), SimpleNamespace(name="Feat")],
        album=SimpleNamespace(name="25"),
        interval=295,
        type=1,
    )
    resp = SimpleNamespace(song=[song])
    client = _make_client_mock()
    client.search.search_by_type = AsyncMock(return_value=resp)

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        out = qq.search_song("hello adele", num=5)

    assert out == [
        {
            "id": 100,
            "mid": "m100",
            "title": "Hello",
            "artists": ["Adele", "Feat"],
            "album": "25",
            "duration": 295,
            "type": 1,
        }
    ]
    _, kwargs = client.search.search_by_type.call_args
    assert kwargs["keyword"] == "hello adele"
    assert kwargs["num"] == 5


def test_add_songs_batches_in_30s():
    cred = _make_credential()
    client = _make_client_mock()
    items = [(i, 0) for i in range(65)]  # 65 songs → 3 batches (30, 30, 5)

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        assert qq.add_songs(42, items) is True

    assert client.songlist.add_songs.await_count == 3
    # verify batch sizes
    sizes = [
        len(call.kwargs["song_info"])
        for call in client.songlist.add_songs.await_args_list
    ]
    assert sizes == [30, 30, 5]


def test_del_songs_empty_returns_true_no_call():
    cred = _make_credential()
    client = _make_client_mock()

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        assert qq.del_songs(42, []) is True

    client.songlist.del_songs.assert_not_awaited()


def test_add_songs_retries_once_on_exception_then_succeeds():
    cred = _make_credential()
    client = _make_client_mock()
    # First call raises, second returns True
    client.songlist.add_songs = AsyncMock(side_effect=[RuntimeError("boom"), True])

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        assert qq.add_songs(42, [(1, 0)]) is True

    assert client.songlist.add_songs.await_count == 2


def test_add_songs_raises_after_second_failure():
    cred = _make_credential()
    client = _make_client_mock()
    client.songlist.add_songs = AsyncMock(
        side_effect=[RuntimeError("boom1"), RuntimeError("boom2")]
    )

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        with pytest.raises(RuntimeError, match="boom2"):
            qq.add_songs(42, [(1, 0)])


def test_get_playlist_songs_paginates_until_hasmore_false():
    cred = _make_credential()

    def _song(sid):
        return SimpleNamespace(
            id=sid,
            mid=f"m{sid}",
            title=f"S{sid}",
            name=f"S{sid}",
            singer=[SimpleNamespace(name="X")],
            interval=200,
            type=1,
        )

    page1 = SimpleNamespace(songs=[_song(1), _song(2)], hasmore=1)
    page2 = SimpleNamespace(songs=[_song(3)], hasmore=0)
    client = _make_client_mock()
    client.songlist.get_detail = AsyncMock(side_effect=[page1, page2])

    with patch.object(qc, "Client", return_value=client):
        qq = qc.QQClient(cred)
        out = qq.get_playlist_songs(dirid=5)

    assert [s["id"] for s in out] == [1, 2, 3]
    assert client.songlist.get_detail.await_count == 2
