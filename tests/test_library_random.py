"""无歌名指令：从本地曲库随机选歌。"""

from __future__ import annotations

import pytest

from music_mcp import app as app_module


def _item(bvid: str, title: str, artist: str = "", duration: int = 240) -> dict:
    return {"bvid": bvid, "title": title, "artist": artist, "duration": duration}


@pytest.fixture
def fake_library(monkeypatch):
    items = [
        _item("BV1", "青花瓷", "周杰伦"),
        _item("BV2", "稻香", "周杰伦"),
        _item("BV3", "声乐老师解析烟花易冷", "某UP主"),
        _item("BV4", "缺少文件的歌", "某人"),
    ]
    existing = {"BV1", "BV2", "BV3"}
    monkeypatch.setattr(app_module.library, "list", lambda limit=50: items[:limit])
    monkeypatch.setattr(
        app_module.library, "has_file", lambda bvid: bvid in existing
    )
    return items


def test_random_skips_missing_files_and_noisy_titles(fake_library):
    songs = app_module._random_library_songs(5)
    ids = {s.song_id for s in songs}
    assert "BV4" not in ids  # 文件不存在
    assert "BV3" not in ids  # 噪声标题
    assert ids == {"BV1", "BV2"}
    assert all(s.source_name == "本地曲库" for s in songs)


def test_random_dedupes_same_song(fake_library, monkeypatch):
    items = [
        _item("BV1", "青花瓷", "周杰伦"),
        _item("BV2", "青花瓷", "周杰伦"),
    ]
    monkeypatch.setattr(app_module.library, "list", lambda limit=50: items)
    songs = app_module._random_library_songs(5)
    assert len(songs) == 1


def test_random_falls_back_when_all_noisy(monkeypatch):
    items = [_item("BV1", "声乐老师解析某歌"), _item("BV2", "某某盘点合集")]
    monkeypatch.setattr(app_module.library, "list", lambda limit=50: items)
    monkeypatch.setattr(app_module.library, "has_file", lambda bvid: True)
    songs = app_module._random_library_songs(2)
    assert {s.song_id for s in songs} == {"BV1", "BV2"}


def test_random_empty_library(monkeypatch):
    monkeypatch.setattr(app_module.library, "list", lambda limit=50: [])
    assert app_module._random_library_songs(1) == []


@pytest.mark.asyncio
async def test_play_song_without_query_uses_library(fake_library, monkeypatch):
    picked = app_module._random_library_songs(1)
    monkeypatch.setattr(app_module, "_random_library_songs", lambda count=1: picked)

    async def fake_resolve(song):
        from music_mcp.models import PlayResult

        return PlayResult(url="http://127.0.0.1:8099/library/BV1.mp3")

    monkeypatch.setattr(app_module.manager, "resolve", fake_resolve)

    async def fake_verify(url):
        return True

    monkeypatch.setattr(app_module, "_verify_url", fake_verify)
    result = await app_module._play_song({})
    assert result["isError"] is False
    assert result["structuredContent"]["playing"] is True
    assert "/library/" in result["structuredContent"]["play_url"]


@pytest.mark.asyncio
async def test_play_playlist_without_query_uses_library(fake_library):
    result = await app_module._play_playlist({"limit": 2})
    assert result["isError"] is False
    songs = result["structuredContent"]["songs"]
    assert 1 <= len(songs) <= 2
    assert all("/library/" in s["play_url"] for s in songs)
