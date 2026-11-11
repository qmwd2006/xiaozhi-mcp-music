#!/usr/bin/env python3
"""按歌手批量收集单曲到本地曲库（B 站音源）。

流程：多关键词搜索 -> 过滤非单曲/非本人作品 -> 按歌名去重 -> 转码入曲库。

用法：
    python scripts/collect_artist.py 单依纯
    python scripts/collect_artist.py 单依纯 --dry-run      # 只列出将收集的歌曲
    python scripts/collect_artist.py 单依纯 --limit 20     # 最多收集 20 首
    python scripts/collect_artist.py 单依纯 --queries "单依纯,单依纯 新歌"

建议在容器内运行（已内置 ffmpeg）：
    docker exec -w /app -e PYTHONPATH=/app xiaozhi-music-mcp \
        python3 scripts/collect_artist.py 单依纯
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from typing import Dict, Iterable, List, Optional

from music_mcp import stream_server
from music_mcp.library import library
from music_mcp.noise import is_noise, normalize, song_name
from music_mcp.sources.bilibili import BilibiliSource

logger = logging.getLogger("collect_artist")

# 单曲时长范围（秒）：太短是切片，太长是合集/直播。
_MIN_DURATION = 90
_MAX_DURATION = 480


def _belongs_to_artist(title: str, artist_field: str, artist: str) -> bool:
    """判断这首歌是否由该歌手演唱（独唱或合作均可）。"""
    key = normalize(artist)
    if not key:
        return False
    return key in normalize(artist_field) or key in normalize(title)


def _iter_queries(artist: str, extra: Optional[str]) -> List[str]:
    if extra:
        return [q.strip() for q in extra.split(",") if q.strip()]
    return [
        f"{artist} 歌曲",
        f"{artist} 单曲",
        f"{artist} 新歌",
        f"{artist} 现场",
        f"{artist} MV",
        f"{artist} 原唱",
        f"{artist} 翻唱",
        artist,
    ]


async def _search_once(query: str, per_query: int) -> List:
    src = BilibiliSource()
    try:
        return await src.search(query, limit=per_query)
    finally:
        await src._client.aclose()


_QUERY_SLEEP = 4.0


async def _search_all(artist: str, queries: Iterable[str], per_query: int) -> List:
    """逐个关键词搜索；空结果视为风控，退避后重试一次。"""
    songs = []
    for query in queries:
        for attempt in range(2):
            try:
                found = await _search_once(query, per_query)
            except Exception as exc:  # 单个查询失败不影响其它查询
                logger.warning("搜索失败 %r: %s", query, exc)
                found = []
            if found:
                logger.info("搜索 %-16r -> %d 条", query, len(found))
                songs.extend(found)
                break
            if attempt == 0:
                wait = _QUERY_SLEEP * 1.5 + random.uniform(0, _QUERY_SLEEP)
                logger.info("搜索 %r 无结果，%.1fs 后重试", query, wait)
                await asyncio.sleep(wait)
            else:
                logger.warning("搜索 %r 重试仍无结果（可能触发风控）", query)
        # 查询间隔带随机抖动，降低风控概率
        await asyncio.sleep(_QUERY_SLEEP + random.uniform(0, _QUERY_SLEEP * 0.75))
    return songs


def _select_unique(
    songs: List,
    artist: str,
    *,
    include: Optional[set] = None,
    exclude: Optional[set] = None,
    min_duration: int = _MIN_DURATION,
    max_duration: int = _MAX_DURATION,
) -> Dict[str, object]:
    """过滤并去重，返回 {归一化歌名: Song}，优先保留时长合理的版本。

    include 非空时只保留这些 bvid；exclude 中的 bvid 一律跳过。
    """
    picked: Dict[str, object] = {}
    explicit = include is not None  # 显式指定 bvid 时跳过启发式过滤
    for song in songs:
        title = song.title or ""
        bvid = song.song_id or ""
        if exclude and bvid in exclude:
            continue
        if include and bvid not in include:
            continue
        if not explicit:
            if not (min_duration <= int(song.duration or 0) <= max_duration):
                continue
            if is_noise(title):
                continue
            if not _belongs_to_artist(title, song.artist or "", artist):
                continue
        name = song_name(title, artist)
        key = normalize(name)
        if not key or len(key) < 2:
            continue
        if len(key) > 20:  # 过长基本是话题/合集标题
            continue
        prev = picked.get(key)
        if prev is None:
            picked[key] = song
            continue
        # 同一首歌多个投稿：优先「歌手字段精确」的，其次时长更接近完整单曲。
        prev_exact = normalize(prev.artist or "") == normalize(artist)
        cur_exact = normalize(song.artist or "") == normalize(artist)
        if cur_exact and not prev_exact:
            picked[key] = song
    return picked


async def _fetch_by_bvids(bvids: Iterable[str]) -> List:
    """按 bvid 直接取元数据（不依赖搜索结果）。"""
    songs = []
    for bvid in bvids:
        src = BilibiliSource()
        try:
            song = await src.get_song(bvid)
        except Exception as exc:
            logger.warning("获取视频信息失败 %s: %s", bvid, exc)
            song = None
        finally:
            await src._client.aclose()
        if song:
            songs.append(song)
            logger.info("指定视频 %s -> %s | %s", bvid, song.title[:40], song.artist)
        else:
            logger.warning("指定视频不可用，已跳过: %s", bvid)
        await asyncio.sleep(1.0)
    return songs


async def _collect_one(song, artist: str) -> bool:
    src = BilibiliSource()
    try:
        upstream = await src.get_audio_source_url(song.song_id)
    except Exception as exc:
        logger.error("解析失败 %s %s: %s", song.song_id, song.title[:40], exc)
        return False
    finally:
        await src._client.aclose()

    name = song_name(song.title, artist)
    try:
        await stream_server._transcode_and_store(
            song.song_id,
            upstream,
            title=name,
            artist=artist,
            duration=song.duration,
        )
    except Exception as exc:
        logger.error("转码入库失败 %s: %s", song.song_id, exc)
        return False
    logger.info("已入库: %s | %s | %ss", name, song.song_id, song.duration)
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="按歌手批量收集单曲到曲库")
    parser.add_argument("artist", help="歌手名，例如 单依纯")
    parser.add_argument("--limit", type=int, default=0, help="最多收集数量（0 表示不限制）")
    parser.add_argument("--per-query", type=int, default=30, help="每个关键词搜索条数")
    parser.add_argument("--queries", default=None, help="自定义搜索关键词，逗号分隔")
    parser.add_argument("--dry-run", action="store_true", help="只列出结果，不下载")
    parser.add_argument("--sleep", type=float, default=4.0, help="搜索间隔基数（秒），风控严时调大")
    parser.add_argument("--include", default="", help="只收集这些 bvid（逗号分隔）")
    parser.add_argument("--exclude", default="", help="跳过这些 bvid（逗号分隔）")
    parser.add_argument("--min-duration", type=int, default=_MIN_DURATION,
                        help=f"最短时长秒，默认 {_MIN_DURATION}")
    parser.add_argument("--max-duration", type=int, default=_MAX_DURATION,
                        help=f"最长时长秒，默认 {_MAX_DURATION}")
    args = parser.parse_args()

    include = {b.strip() for b in args.include.split(",") if b.strip()} or None
    exclude = {b.strip() for b in args.exclude.split(",") if b.strip()} or None

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    global _QUERY_SLEEP
    _QUERY_SLEEP = max(1.0, args.sleep)
    queries = _iter_queries(args.artist, args.queries)
    if include:
        # 指定视频时不走搜索：直接取元数据，避免搜索风控导致漏掉。
        songs = await _fetch_by_bvids(sorted(include))
    else:
        songs = await _search_all(args.artist, queries, args.per_query)
    unique = _select_unique(
        songs,
        args.artist,
        include=include,
        exclude=exclude,
        min_duration=max(1, args.min_duration),
        max_duration=max(args.min_duration, args.max_duration),
    )

    # 曲库里已有同名歌曲则跳过（不同 bvid 的重复投稿）。
    existing = {normalize(song_name(it.get("title", ""), args.artist)) for it in library.list(limit=500)}
    todo = []
    for key, song in unique.items():
        if key in existing:
            continue
        todo.append(song)

    print(f"候选去重后 {len(unique)} 首，曲库已有 {len(unique) - len(todo)} 首，待收集 {len(todo)} 首")
    for song in sorted(todo, key=lambda s: song_name(s.title, args.artist)):
        print(f"  {song.song_id}  {int(song.duration):>4}s  {song_name(song.title, args.artist)}")

    if args.dry_run:
        return 0

    if args.limit > 0:
        todo = todo[: args.limit]

    ok = 0
    for song in todo:
        if await _collect_one(song, args.artist):
            ok += 1
        await asyncio.sleep(1.0)

    print(f"完成：成功入库 {ok}/{len(todo)} 首")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
