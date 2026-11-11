#!/usr/bin/env python3
"""整理本地曲库：规范化标题/歌手、识别噪声与重复。

默认只打印差异（dry-run），不会修改任何文件。

用法：
    python scripts/organize_library.py                 # 预览
    python scripts/organize_library.py --apply         # 写回（自动备份 index.json）
    python scripts/organize_library.py --apply --drop-duplicates   # 同时删除重复条目

在容器内运行：
    docker exec -w /app -e PYTHONPATH=/app xiaozhi-music-mcp \
        python3 scripts/organize_library.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from music_mcp.library import library
from music_mcp.noise import (
    clean_artist,
    is_offtopic,
    normalize,
    noise_words,
    offtopic_hits,
    safe_song_name,
)


def _plan(items: List[dict]) -> Tuple[List[dict], List[dict], List[List[dict]]]:
    """返回 (元数据修正, 噪声条目, 重复组)。"""
    fixes: List[dict] = []
    noisy: List[dict] = []
    groups: Dict[tuple, List[dict]] = defaultdict(list)

    for item in items:
        bvid = item.get("bvid") or ""
        title = item.get("title") or ""
        artist = item.get("artist") or ""

        if is_offtopic(title):
            noisy.append({**item, "_hits": offtopic_hits(title)})
            continue

        new_title = safe_song_name(title, artist) or title
        new_artist = clean_artist(artist)

        if new_title != title or new_artist != artist:
            fixes.append(
                {
                    "bvid": bvid,
                    "old_title": title,
                    "new_title": new_title,
                    "old_artist": artist,
                    "new_artist": new_artist,
                }
            )

        # 只把「同一歌手的同一首歌」视为重复；不同歌手的同名曲可能是翻唱/不同作品。
        name_key = normalize(safe_song_name(title, artist) or title)
        artist_key = normalize(clean_artist(artist))
        if name_key:
            groups[(name_key, artist_key)].append(item)

    duplicate_groups = [g for g in groups.values() if len(g) > 1]
    # 每组保留时长最接近单曲区间、体积更大的那个
    duplicate_groups.sort(key=lambda g: (-len(g), normalize(g[0].get("title", ""))))
    return fixes, noisy, duplicate_groups


def _pick_keeper(group: List[dict]) -> dict:
    def score(item: dict) -> tuple:
        duration = int(item.get("duration") or 0)
        in_range = 0 if 90 <= duration <= 480 else 1
        return (in_range, -int(item.get("size") or 0))

    return sorted(group, key=score)[0]


def _find_orphan_files(items: List[dict], directory: Path | None = None) -> List[Path]:
    """索引里没有、但曲库目录存在音频文件的孤儿条目。"""
    known = {item.get("bvid") for item in items if item.get("bvid")}
    directory = Path(directory or library.directory)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.mp3")
        if p.stem not in known and p.stat().st_size > 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="整理本地曲库")
    parser.add_argument("--apply", action="store_true", help="写回修改（默认只预览）")
    parser.add_argument(
        "--drop-duplicates",
        action="store_true",
        help="配合 --apply，删除重复组里未被保留的条目（含音频文件）",
    )
    parser.add_argument(
        "--drop-noise",
        action="store_true",
        help="配合 --apply，删除判定为噪声的条目（含音频文件）",
    )
    parser.add_argument(
        "--drop-orphans",
        action="store_true",
        help="配合 --apply，删除索引里不存在的孤儿音频文件",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出计划")
    args = parser.parse_args()

    items = library.all_items()
    fixes, noisy, duplicates = _plan(items)
    orphans = _find_orphan_files(items)

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(items),
                    "fixes": fixes,
                    "noise": [
                        {"bvid": i.get("bvid"), "title": i.get("title"),
                         "artist": i.get("artist"), "hits": i.get("_hits")}
                        for i in noisy
                    ],
                    "duplicates": [
                        [
                            {"bvid": i.get("bvid"), "title": i.get("title"),
                             "artist": i.get("artist"), "duration": i.get("duration")}
                            for i in grp
                        ]
                        for grp in duplicates
                    ],
                    "orphan_files": [p.name for p in orphans],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"曲库共 {len(items)} 首；噪声词表 {len(noise_words())} 个词。\n")

    if fixes:
        print(f"== 元数据修正 {len(fixes)} 条 ==")
        for f in fixes:
            if f["old_title"] != f["new_title"]:
                print(f"  {f['bvid']}  标题: {f['old_title'][:40]!r} -> {f['new_title'][:40]!r}")
            if f["old_artist"] != f["new_artist"]:
                print(f"  {f['bvid']}  歌手: {f['old_artist']!r} -> {f['new_artist']!r}")
        print()

    if noisy:
        print(f"== 疑似噪声 {len(noisy)} 条 ==")
        for n in noisy:
            print(f"  {n.get('bvid')}  {n.get('title','')[:50]}  命中={','.join(n['_hits'][:3])}")
        print()

    if duplicates:
        print(f"== 重复组 {len(duplicates)} 组 ==")
        for group in duplicates:
            keeper = _pick_keeper(group)
            print(f"  保留 {keeper.get('bvid')} ({keeper.get('duration')}s)")
            for item in group:
                mark = "  ↑" if item is keeper else "  -"
                print(f"{mark} {item.get('bvid')}  {item.get('title','')[:44]}")
        print()

    if orphans:
        print(f"== 无索引的孤儿音频 {len(orphans)} 个 ==")
        for p in orphans:
            print(f"  {p.name}  {p.stat().st_size // 1024}KB")
        print()

    if not (fixes or noisy or duplicates or orphans):
        print("没有需要整理的内容。")
        return 0

    if not args.apply:
        print(
            "预览结束。加 --apply 写回元数据；"
            "删除需显式加 --drop-duplicates / --drop-noise / --drop-orphans。"
        )
        return 0

    # 备份索引
    index_path = Path(library.directory) / "index.json"
    if index_path.exists():
        backup = index_path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(index_path, backup)
        print(f"已备份索引: {backup}")

    changed = 0
    for f in fixes:
        if library.update_meta(
            f["bvid"], title=f["new_title"], artist=f["new_artist"]
        ):
            changed += 1
    print(f"已更新元数据 {changed} 条。")

    removed = 0
    if args.drop_noise:
        for n in noisy:
            if library.remove(n["bvid"]):
                removed += 1
        print(f"已删除噪声 {removed} 条。")

    if args.drop_duplicates:
        dropped = 0
        for group in duplicates:
            keeper = _pick_keeper(group)
            for item in group:
                if item is keeper:
                    continue
                if library.remove(item["bvid"]):
                    dropped += 1
        print(f"已删除重复 {dropped} 条。")

    if args.drop_orphans:
        cleaned = 0
        for p in orphans:
            try:
                p.unlink()
                cleaned += 1
            except OSError as exc:
                print(f"删除失败 {p.name}: {exc}")
        print(f"已删除孤儿音频 {cleaned} 个。")

    print("整理完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
