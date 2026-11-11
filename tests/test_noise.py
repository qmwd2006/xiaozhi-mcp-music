"""标题清洗与噪声判定。"""

from __future__ import annotations

import pytest

from music_mcp import noise


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("【单依纯《李白》】如何呢，又能怎？", "李白"),
        ("单依纯《想你时风起》百万豪装录音棚大声听【Hi-res】", "想你时风起"),
        ("【4K修复】林志炫《烟花易冷》", "烟花易冷"),
        # 无书名号时保守保持原样（方括号可能只是频道名）
        ("【单依纯】永不失联的爱", "【单依纯】永不失联的爱"),
        ("周杰伦-烟花易冷", "周杰伦-烟花易冷"),
        ("【某幻/中国BOY】电子羊", "【某幻/中国BOY】电子羊"),
    ],
)
def test_safe_song_name_only_rewrites_book_titles(title: str, expected: str) -> None:
    assert noise.safe_song_name(title) == expected


def test_safe_song_name_keeps_unknown_titles_intact() -> None:
    # 没有书名号时保持原样，避免把频道名当歌名
    assert noise.safe_song_name("【频道】随便一个标题") == "【频道】随便一个标题"


@pytest.mark.parametrize(
    ("artist", "expected"),
    [
        ("单依纯的", "单依纯"),
        ("周深神作", "周深"),
        ("周深演唱", "周深"),
        ("汪苏泷&单依纯", "汪苏泷&单依纯"),
    ],
)
def test_clean_artist(artist: str, expected: str) -> None:
    assert noise.clean_artist(artist) == expected


@pytest.mark.parametrize(
    "title",
    [
        "单依纯首尔仁川出发2026.11.5",
        "当翻唱遇上原唱，究竟谁更权威！",
        "【单依纯《李白》】休息室练唱版",
    ],
)
def test_offtopic_detected(title: str) -> None:
    assert noise.is_offtopic(title)


@pytest.mark.parametrize(
    "title",
    [
        "【单依纯《珠玉》歌手舞台】谁去明天遇见我",
        "【4K Hi-Res】单依纯《永不失联的爱》好声音版本",
        "单依纯《想你时风起》百万豪装录音棚大声听【Hi-res】",
    ],
)
def test_quality_markers_are_not_offtopic(title: str) -> None:
    # 音质/画质标记不应把正常歌曲判为噪声
    assert not noise.is_offtopic(title)


def test_external_word_file(monkeypatch, tmp_path) -> None:
    words = tmp_path / "noise.txt"
    words.write_text("# comment\n自定义噪声词\n", encoding="utf-8")
    monkeypatch.setenv("NOISE_WORDS_FILE", str(words))
    loaded = noise.reload_noise_words()
    assert "自定义噪声词" in loaded
    assert noise.is_noise("某个自定义噪声词标题")
    # 恢复内置词表，避免影响其它测试
    monkeypatch.delenv("NOISE_WORDS_FILE")
    noise.reload_noise_words()


def test_lyrics_word_is_not_offtopic() -> None:
    # 「最难唱」这类歌词式描述不应把正常歌曲判为噪声
    title = "周深神作《达拉崩吧》，百变音色，分饰五角！被称为史上最难唱的歌曲之一"
    assert not noise.is_offtopic(title)
    assert noise.safe_song_name(title) == "达拉崩吧"
