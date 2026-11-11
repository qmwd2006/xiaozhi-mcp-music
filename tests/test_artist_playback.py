"""按歌手播放：从曲库挑选指定歌手的单曲。"""

from __future__ import annotations

import pytest

from music_mcp import app as app_module


def _item(bvid: str, title: str, artist: str, duration: int = 240) -> dict:
    return {"bvid": bvid, "title": title, "artist": artist, "duration": duration}


@pytest.fixture
def fake_library(monkeypatch):
    items = [
        _item("BV1", "贝加尔湖畔", "李健"),
        _item("BV2", "风吹麦浪", "李健", 230),
        _item("BV3", "传奇", "李健", 250),
        _item("BV4", "声乐老师解析李健", "某UP主", 200),
        _item("BV5", "青花瓷", "周杰伦"),
        _item("BV6", "李健《假如爱有天意》", "", 260),
    ]
    monkeypatch.setattr(app_module.library, "list", lambda limit=500: items[:limit])
    monkeypatch.setattr(app_module.library, "has_file", lambda bvid: True)
    return items


def test_library_songs_by_artist_filters_noise(fake_library):
    songs = app_module._library_songs_by_artist("李健", limit=10)
    ids = [s.song_id for s in songs]
    assert "BV4" not in ids  # 解说类，不是本人单曲
    assert "BV5" not in ids  # 别的歌手
    assert set(ids) == {"BV1", "BV2", "BV3", "BV6"}
    assert all(s.artist or "李健" in s.title for s in songs)


def test_library_songs_by_artist_dedupes_titles(fake_library, monkeypatch):
    items = [
        _item("BV1", "传奇", "李健"),
        _item("BV2", "传奇", "李健", 245),  # 同名不同投稿
    ]
    monkeypatch.setattr(app_module.library, "list", lambda limit=500: items[:limit])
    songs = app_module._library_songs_by_artist("李健", limit=10)
    assert len(songs) == 1


def test_query_artist_name(fake_library):
    assert app_module._query_artist_name("播放李健的歌") == "李健"
    assert app_module._query_artist_name("李健") == "李健"
    # 歌名不是歌手名
    assert app_module._query_artist_name("青花瓷") == ""
    # 多个词是「歌手 + 歌名」，不是纯歌手指令
    assert app_module._query_artist_name("周杰伦 稻香") == ""
