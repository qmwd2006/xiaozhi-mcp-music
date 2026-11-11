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
import re
import sys
import unicodedata
from typing import Dict, Iterable, List, Optional

from music_mcp import stream_server
from music_mcp.library import library
from music_mcp.sources.bilibili import (
    BilibiliSource,
    _NON_SONG_KEYWORDS,
    _extract_artist_from_title,
    _strip_tags,
)

logger = logging.getLogger("collect_artist")

# 明显不是「本人单曲」的标题关键词（叠加在音源自带过滤之上）。
_EXTRA_NOISE = (
    "盘点", "排名", "排行", "翻唱PK", "PK", "对比", "混剪", "合集", "精选",
    "循环", "歌单", "串烧", "教学", "教程", "解析", "reaction", "反应",
    "采访", "综艺", "八卦", "爆料", "争议", "抄袭", "道歉", "回应",
    "清唱", "练唱", "伴奏", "纯音乐", "钢琴", "吉他", "戏腔", "舞蹈",
    "练习室", "直拍", "饭拍", "路透", "机场", "出发", "抵达", "街拍",
    "原唱", "谁是", "谁更", "能否", "变声", "模仿", "分集", "带歌词",
    "合唱版", "群星", "跨年", "晚会", "音乐节", "巡演", "演唱会",
    "首唱", "现场版", "live版", "混音", "remix", "dj", "DJ",
    # 话题/吐槽/榜单类
    "吐槽", "啥东西", "能听", "有多难唱", "难唱", "好好听", "半夜爬起来",
    "耳麦", "甜歌", "臻品", "母带", "音质", "完整", "无损", "hi-res",
    # 画质/活动/剧集等非歌名内容
    "1080p", "光流", "60帧", "4k", "8k", "mv", "官方", "好奇夜", "晚会",
    "盛典", "跨年", "第二季", "第一季", "主题曲", "插曲", "片尾曲", "片头曲",
)

# 单曲时长范围（秒）：太短是切片，太长是合集/直播。
_MIN_DURATION = 90
_MAX_DURATION = 480

_BOOK_TITLE = re.compile(r"《([^》]{1,40})》")


def _norm(text: str) -> str:
    """归一化：全角转半角、小写、去掉标点/空白，仅保留字母数字汉字。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


_BLOCK = re.compile(r"[【\[]([^】\]]{2,20})[】\]]")


def _song_name(title: str, artist_hint: str = "") -> str:
    """从标题里提取歌名：优先书名号，其次短方括号块，最后清理后的短标题。"""
    title = _strip_tags(title)
    m = _BOOK_TITLE.search(title)
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if _is_noise(name) or (artist_hint and _norm(name) == _norm(artist_hint)):
            return ""
        return name
    # 「【爱错】 王力宏&单依纯 ...」这类：取非噪声的短方括号内容。
    for block in _BLOCK.findall(title):
        candidate = block.strip()
        if len(_norm(candidate)) < 2 or _is_noise(candidate):
            continue
        # 「【单依纯】永不失联的爱」这类块只是歌手名，跳过。
        if _norm(candidate) == _norm(artist_hint):
            continue
        return candidate
    # 去掉【】[]()（）块与常见后缀
    text = re.sub(r"[【\[][^】\]]*[】\]]", " ", title)
    text = re.sub(r"[（(][^）)]*[）)]", " ", text)
    # 「下雨天_单依纯」「如果爱忘了 汪苏泷/单依纯」这类：去掉歌手名残留
    if artist_hint:
        text = re.sub(rf"[\s_\-—|·]*{re.escape(artist_hint)}\s*$", " ", text)
        text = re.sub(r"[\s_\-—|·]+[A-Za-z0-9\u4e00-\u9fff&/、·\s]{1,20}$", " ", text)
    text = re.sub(r"[-—_|·]+", " ", text)
    text = re.sub(r"\bbgm\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    # 「李健」「李健 - 向往 MV」这类：去掉歌手名后剩余才是歌名
    if artist_hint:
        text = re.sub(rf"^{re.escape(artist_hint)}\s*[-—–|·]?\s*", "", text).strip()
    # 只剩歌手自己、或含画质/MV 等修饰，视为无效歌名
    if artist_hint and _norm(text) == _norm(artist_hint):
        return ""
    if re.search(r"(\d{3,4}p|mv\b|光流|帧|官方|高清|高清版)", text, re.I):
        return ""
    return text


def _is_noise(title: str) -> bool:
    low = (title or "").lower()
    return any(k.lower() in low for k in _NON_SONG_KEYWORDS) or any(
        k.lower() in low for k in _EXTRA_NOISE
    )


def _belongs_to_artist(title: str, artist_field: str, artist: str) -> bool:
    """判断这首歌是否由该歌手演唱（独唱或合作均可）。"""
    key = _norm(artist)
    if not key:
        return False
    return key in _norm(artist_field) or key in _norm(title)


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


def _select_unique(songs: List, artist: str) -> Dict[str, object]:
    """过滤并去重，返回 {归一化歌名: Song}，优先保留时长合理的版本。"""
    picked: Dict[str, object] = {}
    for song in songs:
        title = song.title or ""
        if not (_MIN_DURATION <= int(song.duration or 0) <= _MAX_DURATION):
            continue
        if _is_noise(title):
            continue
        if not _belongs_to_artist(title, song.artist or "", artist):
            continue
        name = _song_name(title, artist)
        key = _norm(name)
        if not key or len(key) < 2:
            continue
        if len(key) > 20:  # 过长基本是话题/合集标题
            continue
        prev = picked.get(key)
        if prev is None:
            picked[key] = song
            continue
        # 同一首歌多个投稿：优先「歌手字段精确」的，其次时长更接近完整单曲。
        prev_exact = _norm(prev.artist or "") == _norm(artist)
        cur_exact = _norm(song.artist or "") == _norm(artist)
        if cur_exact and not prev_exact:
            picked[key] = song
    return picked


async def _collect_one(song, artist: str) -> bool:
    src = BilibiliSource()
    try:
        upstream = await src.get_audio_source_url(song.song_id)
    except Exception as exc:
        logger.error("解析失败 %s %s: %s", song.song_id, song.title[:40], exc)
        return False
    finally:
        await src._client.aclose()

    name = _song_name(song.title, artist)
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    global _QUERY_SLEEP
    _QUERY_SLEEP = max(1.0, args.sleep)
    queries = _iter_queries(args.artist, args.queries)
    songs = await _search_all(args.artist, queries, args.per_query)
    unique = _select_unique(songs, args.artist)

    # 曲库里已有同名歌曲则跳过（不同 bvid 的重复投稿）。
    existing = {_norm(_song_name(it.get("title", ""), args.artist)) for it in library.list(limit=500)}
    todo = []
    for key, song in unique.items():
        if key in existing:
            continue
        todo.append(song)

    print(f"候选去重后 {len(unique)} 首，曲库已有 {len(unique) - len(todo)} 首，待收集 {len(todo)} 首")
    for song in sorted(todo, key=lambda s: _song_name(s.title, args.artist)):
        print(f"  {song.song_id}  {int(song.duration):>4}s  {_song_name(song.title, args.artist)}")

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
