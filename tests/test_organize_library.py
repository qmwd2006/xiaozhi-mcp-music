"""曲库整理：元数据修正、噪声、重复与孤儿文件。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import organize_library as org  # noqa: E402


def _item(bvid, title, artist, duration=240, size=1000):
    return {"bvid": bvid, "title": title, "artist": artist,
            "duration": duration, "size": size}


def test_plan_normalizes_titles_and_artists():
    items = [
        _item("BV1", "【单依纯《李白》】如何呢", "单依纯的"),
        _item("BV2", "【4K】正常标题", "周深"),
    ]
    fixes, noisy, dups = org._plan(items)
    by_bvid = {f["bvid"]: f for f in fixes}
    assert by_bvid["BV1"]["new_title"] == "李白"
    assert by_bvid["BV1"]["new_artist"] == "单依纯"
    assert "BV2" not in by_bvid  # 无书名号标题保持原样
    assert not noisy
    assert not dups


def test_plan_detects_noise_and_duplicates():
    items = [
        _item("BV1", "单依纯《有趣》", "单依纯", 229, 2000),
        _item("BV2", "单依纯《有趣》", "单依纯", 240, 1000),
        _item("BV3", "当翻唱遇上原唱，谁更权威", "排行君", 220, 900),
    ]
    _, noisy, dups = org._plan(items)
    assert [n["bvid"] for n in noisy] == ["BV3"]
    assert len(dups) == 1
    keeper = org._pick_keeper(dups[0])
    assert keeper["bvid"] == "BV1"  # 体积更大者保留


def test_plan_keeps_same_title_different_artists_apart():
    items = [
        _item("BV1", "单依纯《烟花易冷》", "单依纯"),
        _item("BV2", "林志炫《烟花易冷》", "林志炫"),
    ]
    _, _, dups = org._plan(items)
    assert not dups  # 不同歌手的同名曲不算重复


def test_orphan_files(tmp_path):
    (tmp_path / "BV1.mp3").write_bytes(b"a")
    (tmp_path / "BV_orphan.mp3").write_bytes(b"b")
    items = [_item("BV1", "歌", "人")]
    orphans = org._find_orphan_files(items, tmp_path)
    assert [p.name for p in orphans] == ["BV_orphan.mp3"]
