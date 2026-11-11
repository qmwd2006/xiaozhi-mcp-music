"""B 站标题里的歌手名提取。"""

from __future__ import annotations

import pytest

from music_mcp.sources.bilibili import _extract_artist_from_title


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "周深神作《达拉崩吧》，百变音色，分饰五角！被称为史上最难唱的歌曲之一！！！",
            "周深",
        ),
        ("单依纯《想你时风起》百万豪装录音棚大声听【Hi-res】", "单依纯"),
        ("【4K修复】林志炫《烟花易冷》", "林志炫"),
        ("【周深《大鱼》】封神现场", "周深"),
        ("【4K60FPS】周杰伦《七里香》封神之作！华语乐坛最美的一首歌", "周杰伦"),
        ("周深封神之作《大鱼》", "周深"),
    ],
)
def test_extract_artist_strips_title_noise(title: str, expected: str) -> None:
    assert _extract_artist_from_title(title) == expected


def test_extract_artist_returns_empty_when_absent() -> None:
    assert _extract_artist_from_title("纯音乐合集 100 首") == ""
