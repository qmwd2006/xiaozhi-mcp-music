"""MCP 工具装配：搜索、播放、音源管理。"""

from __future__ import annotations

import logging
import random
import re
from typing import Any, Dict, List, Optional

from music_mcp.config import settings
from music_mcp.local_player import player
from music_mcp.mcp_server import MCPServer, MCPError
from music_mcp.models import Song
from music_mcp.resource_proxy import proxy
from music_mcp.sources import manager
from music_mcp.utils import compact_json, truncate_text
from music_mcp import stream_server
from music_mcp.library import library

logger = logging.getLogger("music_mcp.app")

SOURCE_CHOICES = ["auto", *manager.names]


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _resource_link(song: Song, url: str, mime_type: str) -> dict:
    return {
        "type": "resource_link",
        "uri": proxy.URI,
        "name": f"{song.title} - {song.artist}" if song.artist else song.title,
        "description": url,
        "mimeType": mime_type,
    }


def _song_public(song: Song) -> dict:
    return song.to_dict()


async def _verify_url(url: str) -> bool:
    """快速验证播放地址可用（Range 0-1）。"""
    try:
        ok = await proxy.verify_url(url)
        return ok
    except Exception as exc:
        logger.debug("播放地址验证失败 %s: %s", url, exc)
        return False


def _tool_result(
    texts: List[str],
    structured: Optional[dict] = None,
    is_error: bool = False,
    resource_link: Optional[dict] = None,
) -> dict:
    content = [_text(t) for t in texts]
    if resource_link:
        content.append(resource_link)
    result: Dict[str, Any] = {"content": content, "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


async def _search_song(args: dict) -> dict:
    query = str(args.get("query", "") or "").strip()
    limit = int(args.get("limit", 8) or 8)
    limit = max(1, min(limit, 20))
    source_raw = str(args.get("source", "auto") or "auto").strip()
    source = source_raw if source_raw in manager.names else None
    if not query:
        return _tool_result(["请提供搜索关键词 query"], {"songs": []}, is_error=True)
    songs = await manager.search(query, limit=limit, source=source)
    payload = [_song_public(s) for s in songs]
    if not payload:
        return _tool_result(
            [f"没有搜索到与「{query}」相关的歌曲，请换个关键词试试。"],
            {"songs": []},
        )
    text = compact_json(payload)
    summary = f"共找到 {len(payload)} 首与「{query}」相关的歌曲："
    for i, s in enumerate(payload[:6], 1):
        summary += f"\n{i}. {s['title']} - {s['artist']}（{s['source_name']}）"
    return _tool_result([summary, text], {"songs": payload})


def _search_query(query: str) -> str:
    """把口语化查询清洗成音源友好的关键词组合。

    例：
      '播放单依纯的有趣' -> '单依纯 有趣'
      '我想听周杰伦的稻香' -> '周杰伦 稻香'
      '来一首青花瓷' -> '青花瓷'
    音源（尤其酷我）对整句口语的检索效果很差，会返回大量无关结果。
    """
    text = query or ""
    for filler in _FILLER_WORDS:
        text = text.replace(filler, " ")
    parts = re.split(r"[\s的,，、/|·:：]+", text)
    tokens = [p.strip() for p in parts if len(_normalize(p)) >= 1]
    cleaned = " ".join(tokens).strip()
    return cleaned or query


def _library_candidates(query: str, limit: int = 5) -> List[Song]:
    """在本地曲库中查找候选（同音字容错）。

    曲库文件是本地持久音频，命中即可离线播放，且不受音源风控影响。
    """
    try:
        items = library.search(query, limit=limit)
    except Exception as exc:
        logger.debug("曲库查询失败: %s", exc)
        return []
    songs: List[Song] = []
    for item in items:
        bvid = item.get("bvid") or ""
        if not bvid or not library.has_file(bvid):
            continue
        songs.append(
            Song(
                source="bilibili",
                source_name="本地曲库",
                song_id=bvid,
                title=item.get("title") or bvid,
                artist=item.get("artist") or "",
                duration=int(item.get("duration") or 0),
                mime_type="audio/mpeg",
            )
        )
    return songs


# 常见同音字替换表（针对语音识别高频错字）。
_HOMOPHONE_HINTS = {
    "朱": ["珠", "竹", "主"],
    "珠": ["朱", "竹"],
    "竹": ["朱", "珠"],
    "青": ["清", "轻"],
    "清": ["青", "轻"],
    "晴": ["青", "情"],
    "情": ["晴", "青"],
    "依": ["一", "衣", "伊"],
    "易": ["意", "亦", "亿"],
    "冷": ["铃", "玲"],
    "花": ["华", "画"],
    "华": ["花", "画"],
    "夜": ["叶", "业"],
    "叶": ["夜", "业"],
}


def _pinyin_variants(query: str, max_variants: int = 4) -> List[str]:
    """生成同音字替换后的查询变体，用于音源检索兜底。

    只替换查询中的单个汉字，且限制数量，避免请求量爆炸。
    """
    variants: List[str] = []
    seen: set = set()
    for idx, ch in enumerate(query):
        for repl in _HOMOPHONE_HINTS.get(ch, ()):
            candidate = query[:idx] + repl + query[idx + 1:]
            if candidate != query and candidate not in seen:
                seen.add(candidate)
                variants.append(candidate)
                if len(variants) >= max_variants:
                    return variants
    return variants


async def _resolve_candidates(query: str, song_id: str, source: Optional[str]) -> List[Song]:
    """根据参数构造候选歌曲列表。"""
    if song_id and source:
        src = manager.get(source)
        return [
            Song(
                source=source,
                source_name=src.display_name if src else source,
                song_id=song_id,
                title=query or song_id,
                artist="",
            )
        ]
    search_text = _search_query(query)
    if search_text != query:
        logger.info("搜索词清洗: %r -> %r", query, search_text)

    # 先查本地曲库（同音字容错、不受音源风控影响）。
    local: List[Song] = []
    if not song_id:
        local = _library_candidates(search_text, limit=5)
        if local:
            logger.info("曲库命中 %d 首，优先使用", len(local))

    songs = await manager.search(search_text, limit=12, source=source)

    # 音源无结果时，退回复用曲库结果（常见于 ASR 同音字错、或音源限流）。
    if not songs and not song_id:
        if local:
            return local
        # 逐字替换同音字重试（例如「朱玉」->「珠玉」）
        for variant in _pinyin_variants(search_text):
            logger.info("同音字重试: %r", variant)
            songs = await manager.search(variant, limit=12, source=source)
            if songs:
                break

    if song_id:
        matched = [s for s in songs if s.song_id == song_id]
        if matched:
            return matched
    if local:
        # 曲库结果与在线结果合并去重（曲库优先，可离线播放）。
        seen = {(x.source, x.song_id) for x in local}
        songs = local + [x for x in songs if (x.source, x.song_id) not in seen]
    return songs


# 音源优先级：能提供完整曲的音源排前面。
# 酷我等平台对部分热门歌曲只返回试听片段，B站通常有完整音轨。
_SOURCE_PRIORITY = {
    "bilibili": 0,
    "netease": 1,
    "kuwo": 2,
    "kugou": 3,
    "migu": 4,
}
# 明显是片段/试听的最短时长（秒）；低于此值排到后面。
_MIN_FULL_DURATION = 60
# 超过此长度基本是歌单/合集/循环串烧，不是单曲（秒，15 分钟）。
_MAX_SONG_DURATION = 15 * 60

# 查询里的口语填充词，切词前先去掉，避免影响相关性判断。
_FILLER_WORDS = (
    "我想听", "想听", "帮我", "给我", "请", "播放", "来一首", "来首",
    "放一首", "放首", "一首", "歌曲", "的歌", "歌", "音乐", "听听",
)
# 明显不是单曲的标题关键词（B 站视频标题尤其常见）。
_NOISE_KEYWORDS = (
    "琴谱", "光遇", "教程", "教学", "讲解", "指弹", "吉他谱", "钢琴课",
    "鼓谱", "谱", "vlog", "巡演", "演唱会", "音乐节", "现场直拍", "饭拍",
    "reaction", "翻唱合集", "合集", "串烧", "歌单", "循环", "纯音乐",
    "伴奏", "dj版", "remix", "0.8x", "加速版", "慢速版",
    "竹笛", "二胡", "古筝", "琵琶", "笛箫", "钢琴版", "吉他版",
    "改编", "变奏", "翻自", "cover", "r&b", "rb版", "1.0x", "1.1x", "1.2x",
    "抖音", "热播", "女声版", "男声版", "童声", "ai翻唱", "ai孙燕姿",
    "直拍", "现场版", "音高测量", "乐评", "reaction视频", "夺冠", "首唱",
    "ktv", "伴奏版", "纯伴奏", "消音", "和声版",
    "解析", "声乐老师", "乐感课", "对比", "盘点", "混剪", "纯享版",
    "听力", "测试", "挑战", "盲听", "排名", "抄袭", "又双叒叕", "摊上事",
    "热搜", "争议", "回应", "道歉", "爆料", "八卦",
    "出发", "抵达", "机场", "仁川", "快闪", "偶遇", "街拍", "生图",
    "当翻唱遇上原唱", "谁更", "歌曲精选", "100首", "大全",
    "可视化", "原出处", "练唱", "清唱", "幕后", "花絮", "采访",
    # 话题/解说类标题（避免误伤正常歌名，尽量用短语而非单个常用词）。
    "是不是把这歌忘了", "这歌忘了", "不让你唱", "玩成游戏", "单曲胶囊",
    "史上最强", "封神现场", "纯属娱乐", "维密", "倒嗓", "群像",
    "同人群像", "出道曲", "原著", "出品", "休息室", "无锡站",
    "首尔", "mashup", "核爆", "献唱",
)


def _normalize(text: str) -> str:
    """归一化：转小写并去掉标点、空白与装饰性符号，仅保留字母/数字/汉字。"""
    text = (text or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _query_terms(query: str) -> tuple:
    """把用户查询解析为 (关键词列表, 去填充词后的整串)。

    例：'播放单依纯的永不失联的爱' -> (['单依纯', '永不失联'], '单依纯永不失联的爱')
    """
    text = query or ""
    for filler in _FILLER_WORDS:
        text = text.replace(filler, " ")
    parts = re.split(r"[\s的,，、/|·:：]+", text)
    tokens: List[str] = []
    for part in parts:
        token = _normalize(part)
        if len(token) >= 2 and token not in tokens:
            tokens.append(token)
    whole = _normalize(text)
    return tokens, whole


def _relevance(song: Song, tokens: List[str], whole: str) -> float:
    """候选与查询的匹配度（0~1）。

    评分由两部分组成：
    - 完整查询串是否出现在标题/歌手中（强信号，权重 0.6）
    - 各关键词的命中率（弱信号，权重 0.4）
    """
    if not tokens:
        return 0.5
    title = _normalize(song.title)
    artist = _normalize(song.artist)
    haystack = f"{title}{artist}"
    if not haystack:
        return 0.0

    token_hits = sum(1 for token in tokens if token in haystack) / len(tokens)
    whole_hit = 1.0 if whole and whole in haystack else 0.0
    # 标题里直接包含完整查询串是最强信号
    title_hit = 1.0 if whole and whole in title else 0.0
    return 0.4 * token_hits + 0.4 * whole_hit + 0.2 * title_hit


def _artist_match(song: Song, tokens: List[str]) -> int:
    """查询中的关键词是否指向演唱者。

    - 结构化音源（酷我/网易云/酷狗）：artist 字段是真实歌手。
    - B 站：artist 是 UP 主，真正歌手通常写在标题里（如「周杰伦《稻香》」），
      因此标题命中同样计入。
    """
    artist = _normalize(song.artist)
    title = _normalize(song.title)
    for token in tokens:
        if len(token) < 2:
            continue
        if token in artist or token in title:
            return 1
    return 0


def _artist_count(song: Song) -> int:
    """歌手字段里的人数（按常见分隔符估算）。"""
    raw = song.artist or ""
    parts = re.split(r"[&、,，/]|feat\.?|ft\.?", raw)
    return max(1, len([p for p in parts if p.strip()]))


def _is_noisy_title(song: Song) -> bool:
    """标题是否命中「非单曲」关键词。"""
    title = (song.title or "").lower()
    return any(word in title for word in _NOISE_KEYWORDS)


def _artist_exact(song: Song, artist_query: str) -> int:
    """歌手字段是否精确包含查询的歌手名（用于纯歌手查询）。"""
    if not artist_query:
        return 0
    return 1 if artist_query in _normalize(song.artist) else 0


def _detect_artist_query(tokens: List[str], candidates: List[Song]) -> str:
    """判断查询中的哪个词是「歌手名」。

    思路：候选的 artist 字段是音源返回的真实演唱者。若某个查询词在
    artist 字段中出现 ≥2 次，就认为它是歌手名。

    同时支持多词查询（如「林志炫的烟花易冷」）：此时歌名 token 只会
    在标题里出现，而歌手 token 会出现在 artist 字段里，据此区分。
    """
    if not candidates:
        return ""
    stats: List[tuple] = []
    for token in tokens:
        if len(token) < 2:
            continue
        artist_hits = sum(
            1 for song in candidates if token in _normalize(song.artist)
        )
        stats.append((artist_hits, token))
    if not stats:
        return ""
    stats.sort(reverse=True)
    best_hits, best_token = stats[0]
    if best_hits < 2:
        return ""
    # 多个词都命中 artist 时（B 站 artist 常写成标题片段），
    # 只有第一名明显领先才认定，避免把歌名误判成歌手名。
    if len(stats) > 1 and stats[1][0] == best_hits:
        # 平手时优先较短、且不是整句的子串（歌手名通常更短）
        return min((best_token, stats[1][1]), key=len)
    return best_token


def _candidate_rank(song: Song, tokens: List[str], whole: str, artist_query: str = "") -> tuple:
    """候选排序。

    优先级：匹配度（高者优先）-> 歌手命中 -> 独唱优先（合唱降权）
    -> 可疑度（片段/超长/非单曲标题）-> 音源可靠度 -> 时长（长者优先）。

    artist_query 非空时表示这是「纯歌手查询」（如「单依纯」），
    此时歌手字段精确命中远比标题提到更重要。
    """
    relevance = _relevance(song, tokens, whole)
    artist_hit = _artist_match(song, tokens)
    # 多人合唱通常不是用户想点的「某歌手的歌」，排在同歌手独唱之后。
    chorus = 1 if _artist_count(song) > 1 else 0
    suspicious = 0
    if song.duration and (
        song.duration < _MIN_FULL_DURATION or song.duration > _MAX_SONG_DURATION
    ):
        suspicious = 1
    # 常见歌曲长度 90~480 秒；明显偏离的（超长访谈/合集）额外降权
    if song.duration and not (90 <= song.duration <= 480):
        suspicious += 1
    if _is_noisy_title(song):
        suspicious += 1
    # 本地曲库（已下载、可离线播放）优先于在线音源。
    if song.source_name == "本地曲库":
        prio = -1
    else:
        prio = _SOURCE_PRIORITY.get(song.source, 99)
    if artist_query:
        # 纯歌手查询：歌手字段命中 > 标题命中 > 其它
        exact = _artist_exact(song, artist_query)
        return (-exact, -artist_hit, suspicious, chorus, prio, -song.duration)
    return (-relevance, -artist_hit, suspicious, chorus, prio, -song.duration)


def _library_item_song(item: dict) -> Song:
    """把曲库索引项转成 Song（指向本地持久文件）。"""
    return Song(
        source="bilibili",
        source_name="本地曲库",
        song_id=item.get("bvid", ""),
        title=item.get("title") or item.get("bvid", ""),
        artist=item.get("artist") or "",
        duration=int(item.get("duration") or 0),
        mime_type="audio/mpeg",
    )


def _looks_like_song_title(title: str) -> bool:
    """标题是否更像「歌名」而不是视频话题：含书名号《》或总长度较短。"""
    text = (title or "").strip()
    if not text:
        return False
    if "《" in text and "》" in text:
        return True
    # 去掉常见装饰性前缀后仍然很短，通常就是歌名本身。
    core = re.sub(r"[\[\]【】()（）\s\-—_|]+", "", text)
    return len(core) <= 24


def _random_library_songs(count: int = 1) -> List[Song]:
    """从本地曲库随机取若干首（用于「播放音乐」这类无歌名指令）。

    分档挑选，尽量避开解说、合集、超长现场：
    1. 标题非噪声、像歌名且时长合理（90~480 秒）；
    2. 标题非噪声且时长合理；
    3. 标题非噪声；
    4. 所有可播放条目。
    同一首歌（歌名+歌手归一化后相同）只取一次。
    """
    try:
        items = library.list(limit=200)
    except Exception as exc:
        logger.debug("读取曲库失败: %s", exc)
        return []

    playable = [i for i in items if library.has_file(i.get("bvid", ""))]
    clean = [i for i in playable if not _is_noisy_title(_library_item_song(i))]
    well_formed = [
        i for i in clean
        if 90 <= int(i.get("duration") or 0) <= 480
    ]
    song_like = [i for i in well_formed if _looks_like_song_title(i.get("title", ""))]
    candidates = song_like or well_formed or clean or playable
    if not candidates:
        return []

    random.shuffle(candidates)
    picked: List[Song] = []
    seen: set = set()
    for item in candidates:
        song = _library_item_song(item)
        key = (_normalize(song.title), _normalize(song.artist.split("/")[0]))
        if key in seen:
            continue
        seen.add(key)
        picked.append(song)
        if len(picked) >= count:
            break
    return picked


async def _play_song(args: dict) -> dict:
    query = str(args.get("query", "") or "").strip()
    song_id = str(args.get("song_id", "") or "").strip()
    source_raw = str(args.get("source", "auto") or "auto").strip()
    source = source_raw if source_raw in manager.names else None

    picked: Optional[Song] = None
    if not query and not song_id:
        # 「播放音乐」这类没给歌名的指令：从曲库随机挑一首。
        picks = _random_library_songs(1)
        if not picks:
            return _tool_result(
                ["曲库还是空的。请告诉我歌名（例如：播放青花瓷）。"],
                {"playing": False},
                is_error=True,
            )
        picked = picks[0]
        query = picked.title
        song_id = picked.song_id
        logger.info("无歌名指令，曲库随机播放: %s", picked.title)

    try:
        if picked is not None:
            candidates = [picked]
        else:
            candidates = await _resolve_candidates(query, song_id, source)
    except Exception as exc:
        logger.exception("搜索歌曲失败")
        return _tool_result([f"搜索失败: {exc}"], {"playing": False}, is_error=True)

    # 按「查询匹配度 -> 完整度 -> 音源可靠度」排序，优先尝试真正的目标歌曲。
    tokens, whole = _query_terms(query)
    artist_query = "" if song_id else _detect_artist_query(tokens, candidates)
    candidates = sorted(
        candidates, key=lambda s: _candidate_rank(s, tokens, whole, artist_query)
    )

    if not candidates:
        return _tool_result(
            [f"没有找到「{query or song_id}」对应的歌曲，请换一个说法或歌名。"],
            {"playing": False},
        )

    verify = settings.local_player == "none"  # 本地播放时无需网络验证
    last_error = "未知错误"
    for song in candidates[: settings.max_resolve_tries]:
        try:
            play = await manager.resolve(song)
            if not play.url:
                continue
            if verify and not await _verify_url(play.url):
                last_error = f"「{song.title}」的播放地址无法访问（可能已下架或需要会员）"
                continue
            structured = {
                "playing": True,
                "song": _song_public(song),
                "play_url": play.url,
                "mime_type": play.mime_type,
                "source": song.source_name,
            }
            texts = [
                compact_json(
                    {
                        "playing": True,
                        "title": song.title,
                        "artist": song.artist,
                        "source": song.source_name,
                        "play_url": play.url,
                        "message": f"正在播放：{song.title} - {song.artist}",
                    }
                )
            ]
            if settings.auto_play_local and player.available:
                msg = player.play(play.url, f"{song.title} - {song.artist}")
                texts.append(msg)
                structured["local_player"] = msg
            return _tool_result(
                texts,
                structured,
                resource_link=_resource_link(song, play.url, play.mime_type),
            )
        except Exception as exc:
            last_error = str(exc)
            logger.warning("解析 %s 播放地址失败: %s", song.title, exc)

    return _tool_result(
        [
            f"无法播放「{query or song_id}」：{last_error}。"
            "已尝试多个音源仍失败，可能是版权限制，请换一首歌试试。"
        ],
        {"playing": False, "reason": last_error},
        is_error=True,
    )


async def _play_playlist(args: dict) -> dict:
    """解析多首歌曲的播放直链，供设备端连播。

    模型应按返回顺序逐首调用 self.music.enqueue_url（入队，不打断当前曲目）。
    不要用 self.music.play_url——那是「立即播放」语义，会清空队列。
    """
    query = str(args.get("query", "") or "").strip()
    limit = int(args.get("limit", 5) or 5)
    limit = max(1, min(limit, 10))
    source_raw = str(args.get("source", "auto") or "auto").strip()
    source = source_raw if source_raw in manager.names else None

    if not query:
        # 「来几首歌」「随便放点」这类无关键词指令：从曲库随机取。
        picks = _random_library_songs(limit)
        if not picks:
            return _tool_result(
                ["曲库还是空的。请告诉我歌手或歌名，例如：周杰伦。"],
                {"playing": False, "songs": []},
                is_error=True,
            )
        logger.info("无关键词连播，曲库随机取 %d 首", len(picks))
        lines = [
            f"已从曲库随机挑选 {len(picks)} 首：",
            "请按顺序逐首调用 self.music.enqueue_url，url 参数必须原样复制下面的地址，"
            "不要改写、不要拼接：",
        ]
        for i, s2 in enumerate(picks, 1):
            lines.append(f"{i}. {s2.title} - {s2.artist}")
            lines.append(f"   url={stream_server.build_library_url(s2.song_id)}")
        payload = [
            {
                "title": s2.title,
                "artist": s2.artist,
                "source": s2.source_name,
                "duration": s2.duration,
                "play_url": stream_server.build_library_url(s2.song_id),
                "mime_type": "audio/mpeg",
            }
            for s2 in picks
        ]
        return _tool_result(
            ["\n".join(lines), compact_json({"count": len(payload), "songs": payload})],
            {"playing": False, "songs": payload},
        )

    try:
        # 连播需要更多候选，扩大搜索量后再筛选。
        search_text = _search_query(query)
        candidates = await manager.search(search_text, limit=max(40, limit * 8), source=source)
    except Exception as exc:
        logger.exception("搜索歌曲失败")
        return _tool_result([f"搜索失败: {exc}"], {"playing": False}, is_error=True)

    tokens, whole = _query_terms(query)
    artist_query = _detect_artist_query(tokens, candidates)
    candidates = sorted(
        candidates, key=lambda s: _candidate_rank(s, tokens, whole, artist_query)
    )

    # 连播时更严格：优先「歌手字段命中 + 独唱 + 非噪声标题」，
    # 过滤 UP 主视频、合唱、伴奏/KTV/翻唱等。
    if artist_query:
        strict = [
            s for s in candidates
            if _artist_exact(s, artist_query)
            and _artist_count(s) <= 1
            and not _is_noisy_title(s)
        ]
        if len(strict) >= limit:
            candidates = strict
        else:
            # 不够时放宽标题限制，但仍排除合唱与非目标歌手。
            relaxed = [
                s for s in candidates
                if _artist_exact(s, artist_query) and _artist_count(s) <= 1
            ]
            if len(relaxed) >= limit:
                candidates = relaxed

    # 去重：同一首歌可能在多个音源出现，按「歌名+歌手」归一化后只保留首个。
    picked: List[dict] = []
    seen_titles: set = set()
    verify = settings.local_player == "none"
    for song in candidates:
        if len(picked) >= limit:
            break
        key = (_normalize(song.title), _normalize(song.artist.split("/")[0]))
        if key in seen_titles:
            continue
        try:
            play = await manager.resolve(song)
            if not play.url:
                continue
            if verify and not await _verify_url(play.url):
                continue
        except Exception as exc:
            logger.debug("连播解析失败 %s: %s", song.title, exc)
            continue
        seen_titles.add(key)
        picked.append(
            {
                "title": song.title,
                "artist": song.artist,
                "source": song.source_name,
                "duration": song.duration,
                "play_url": play.url,
                "mime_type": play.mime_type,
            }
        )

    if not picked:
        return _tool_result(
            [f"没有找到「{query}」可播放的歌曲，请换个关键词。"],
            {"playing": False, "songs": []},
        )

    # 文本里直接给出「序号. 歌名 - 歌手 -> url」，
    # 强调 url 必须原样复制，避免模型自行拼接/臆造地址。
    lines = [
        f"已找到 {len(picked)} 首可播放歌曲。",
        "请按顺序逐首调用 self.music.enqueue_url，url 参数必须原样复制下面的地址，"
        "不要改写、不要拼接、不要使用其他域名：",
    ]
    for i, item in enumerate(picked, 1):
        lines.append(f"{i}. {item['title']} - {item['artist']}")
        lines.append(f"   url={item['play_url']}")
    summary = "\n".join(lines)

    urls_text = compact_json({"count": len(picked), "songs": picked})
    return _tool_result([summary, urls_text], {"playing": False, "songs": picked})


async def _get_play_url(args: dict) -> dict:
    query = str(args.get("query", "") or "").strip()
    song_id = str(args.get("song_id", "") or "").strip()
    source_raw = str(args.get("source", "auto") or "auto").strip()
    source = source_raw if source_raw in manager.names else None
    if not query and not song_id:
        return _tool_result(["需要 query 或 song_id"], {"play_url": None}, is_error=True)
    candidates = await _resolve_candidates(query, song_id, source)
    if not candidates:
        return _tool_result(["未找到歌曲"], {"play_url": None})
    for song in candidates[: settings.max_resolve_tries]:
        try:
            play = await manager.resolve(song)
            if play.url:
                return _tool_result(
                    [compact_json({"title": song.title, "artist": song.artist, "play_url": play.url})],
                    {"song": _song_public(song), "play_url": play.url},
                )
        except Exception as exc:
            logger.debug("get_play_url 失败: %s", exc)
    return _tool_result(["未能解析到播放地址"], {"play_url": None}, is_error=True)


async def _list_sources(args: dict) -> dict:
    health = await manager.health()
    payload = [
        {
            "name": name,
            "display_name": info["name"],
            "ok": info["ok"],
        }
        for name, info in health.items()
    ]
    text = compact_json(payload)
    return _tool_result([f"可用音源 {len(payload)} 个：\n" + text], {"sources": payload})


def _library_item_public(item: dict) -> dict:
    return {
        "bvid": item.get("bvid", ""),
        "title": item.get("title", ""),
        "artist": item.get("artist", ""),
        "duration": item.get("duration", 0),
        "size_mb": round(int(item.get("size") or 0) / 1024 / 1024, 1),
        "play_count": item.get("play_count", 0),
        "play_url": stream_server.build_library_url(item.get("bvid", "")),
    }


async def _library_list(args: dict) -> dict:
    limit = int(args.get("limit", 20) or 20)
    limit = max(1, min(limit, 100))
    items = library.list(limit=limit)
    stats = library.stats()
    if not items:
        return _tool_result(
            ["曲库还是空的。播放过的歌曲会自动保存到曲库，之后可离线重播。"],
            {"count": 0, "songs": [], **stats},
        )
    payload = [_library_item_public(i) for i in items]
    lines = [f"曲库共 {stats['count']} 首，占用 {stats['total_mb']} MB。最近保存的 {len(payload)} 首："]
    for i, it in enumerate(payload, 1):
        lines.append(f"{i}. {it['title']} - {it['artist']}")
        lines.append(f"   url={it['play_url']}")
    text = compact_json({"count": len(payload), "songs": payload})
    return _tool_result(["\n".join(lines), text], {"count": stats["count"], "songs": payload, **stats})


async def _library_search(args: dict) -> dict:
    query = str(args.get("query", "") or "").strip()
    if not query:
        return _tool_result(["请提供查询关键词，例如：单依纯。"], {"songs": []}, is_error=True)
    items = library.search(query, limit=20)
    if not items:
        return _tool_result(
            [f"曲库里没有与「{query}」匹配的歌曲。"], {"songs": []}
        )
    payload = [_library_item_public(i) for i in items]
    lines = [f"曲库中找到 {len(payload)} 首与「{query}」相关的歌曲："]
    for i, it in enumerate(payload, 1):
        lines.append(f"{i}. {it['title']} - {it['artist']}")
        lines.append(f"   url={it['play_url']}")
    text = compact_json({"count": len(payload), "songs": payload})
    return _tool_result(["\n".join(lines), text], {"songs": payload})


async def _library_delete(args: dict) -> dict:
    bvid = str(args.get("bvid", "") or "").strip()
    query = str(args.get("query", "") or "").strip()
    if not bvid and not query:
        return _tool_result(
            ["需要 bvid 或 query 才能删除。"], {"deleted": 0}, is_error=True
        )
    if bvid:
        ok = library.remove(bvid)
        return _tool_result(
            ["已从曲库删除。" if ok else "曲库里没有这首歌。"],
            {"deleted": 1 if ok else 0},
            is_error=not ok,
        )
    # 按关键词删除所有匹配项
    items = library.search(query, limit=100)
    if not items:
        return _tool_result([f"曲库里没有与「{query}」匹配的歌曲。"], {"deleted": 0})
    deleted = 0
    for it in items:
        if library.remove(it["bvid"]):
            deleted += 1
    return _tool_result(
        [f"已从曲库删除 {deleted} 首与「{query}」匹配的歌曲。"], {"deleted": deleted}
    )


async def _library_clear(args: dict) -> dict:
    count = library.clear()
    return _tool_result([f"曲库已清空，共删除 {count} 首。"], {"deleted": count})


async def _library_stats(args: dict) -> dict:
    stats = library.stats()
    return _tool_result(
        [f"曲库共 {stats['count']} 首，占用 {stats['total_mb']} MB（{stats['dir']}）。"],
        stats,
    )


async def _local_player_control(args: dict) -> dict:
    action = str(args.get("action", "status") or "status").strip()
    if action == "play":
        url = str(args.get("url", "") or "").strip()
        if not url:
            return _tool_result(["需要 url 参数"], {"local_player": player.status()}, is_error=True)
        msg = player.play(url)
        return _tool_result([msg], {"local_player": player.status()})
    if action == "stop":
        msg = player.stop()
        return _tool_result([msg], {"local_player": player.status()})
    if action == "status":
        return _tool_result([compact_json(player.status())], {"local_player": player.status()})
    return _tool_result(
        [f"不支持的 action: {action}（支持 play/stop/status）"],
        {"local_player": player.status()},
        is_error=True,
    )


def build_server() -> MCPServer:
    server = MCPServer(server_name="xiaozhi-music-mcp", version="0.1.0")

    server.add_tool(
        "search_song",
        (
            "在免费音乐源中搜索歌曲。当用户要求找歌、点歌、搜索歌曲或询问某首歌是否存在时使用。"
            "支持按歌名、歌手名或“歌名 歌手”搜索，返回歌曲列表（含音源、歌名、歌手、专辑、时长）。"
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，例如：青花瓷 或 周杰伦 或 周杰伦 青花瓷"},
                "source": {
                    "type": "string",
                    "enum": SOURCE_CHOICES,
                    "description": "指定音源；auto 表示在所有可用音源中搜索",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回结果数量，默认 8"},
            },
            "required": ["query"],
        },
        _search_song,
    )

    server.add_tool(
        "play_song",
        (
            "播放歌曲。用户说“播放xxx”“来一首xxx”“放点xxx”时使用。"
            "传入歌名/歌手后会自动搜索并解析可播放地址；也可传入 search_song 返回的 song_id 和 source 精确播放。"
            "用户只说“播放音乐”“放首歌”没给歌名时，query 留空，会从本地曲库随机挑一首。"
            "返回内容包含文本信息和 ResourceLink 音频资源（可通过 resource://read_from_http 分页读取后播放）。"
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌曲名，或 歌手 + 歌名，例如：青花瓷；留空则从曲库随机",
                },
                "song_id": {"type": "string", "description": "search_song 返回的歌曲 ID（可选）"},
                "source": {
                    "type": "string",
                    "enum": SOURCE_CHOICES,
                    "description": "指定音源；auto 表示自动选择",
                },
                "prefer_quality": {
                    "type": "string",
                    "enum": ["standard", "high"],
                    "description": "音质偏好，默认 standard",
                },
            },
        },
        _play_song,
    )

    server.add_tool(
        "play_playlist",
        (
            "批量搜索并返回多首歌曲的播放直链，用于连续播放（连播）。"
            "用户说“播放某歌手的歌”“来几首xx”“播放歌单”时使用；"
            "用户说“随便放点”“放点音乐”没给关键词时，query 留空，"
            "会从本地曲库随机挑歌。"
            "返回结果含 songs 数组，每项有 play_url；"
            "请按顺序逐首调用设备工具 self.music.enqueue_url 加入播放队列"
            "（不要用 self.music.play_url，那是立即播放并清空队列）。"
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌手名、歌名或关键词，例如：单依纯；留空则从曲库随机",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "返回歌曲数量，默认 5",
                },
                "source": {
                    "type": "string",
                    "enum": SOURCE_CHOICES,
                    "description": "指定音源；auto 表示自动选择",
                },
            },
        },
        _play_playlist,
    )

    server.add_tool(
        "get_play_url",
        (
            "解析歌曲的直链播放地址。当其他工具需要具体音频 URL 时使用。"
            "参数与 play_song 相同，只返回地址不播放。"
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌曲名或歌手+歌名"},
                "song_id": {"type": "string", "description": "歌曲 ID（可选）"},
                "source": {
                    "type": "string",
                    "enum": SOURCE_CHOICES,
                    "description": "指定音源",
                },
            },
        },
        _get_play_url,
    )

    server.add_tool(
        "library_list",
        (
            "列出本地曲库中已保存的歌曲。播放过的歌曲会自动保存到曲库，"
            "之后可离线重播。返回每首歌的 play_url，可直接交给设备播放。"
        ),
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "description": "返回数量，默认 20"},
            },
        },
        _library_list,
    )

    server.add_tool(
        "library_search",
        (
            "在本地曲库中按歌名/歌手搜索已保存的歌曲，返回 play_url。"
            "用户说“放曲库里的xx”“本地有没有xx”时使用。"
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌名或歌手关键词"},
            },
            "required": ["query"],
        },
        _library_search,
    )

    server.add_tool(
        "library_delete",
        (
            "从本地曲库删除歌曲（会同时删除音频文件）。"
            "用户说“删掉xx”“把这首从曲库去掉”时使用。"
        ),
        {
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "精确指定曲目 ID（优先）"},
                "query": {"type": "string", "description": "按关键词批量删除"},
            },
        },
        _library_delete,
    )

    server.add_tool(
        "library_clear",
        "清空整个本地曲库。用户明确说“清空曲库/删除所有本地歌曲”时才使用。",
        {"type": "object", "properties": {}},
        _library_clear,
    )

    server.add_tool(
        "library_stats",
        "查看本地曲库的歌曲数量与占用空间。",
        {"type": "object", "properties": {}},
        _library_stats,
    )

    server.add_tool(
        "list_sources",
        "列出当前可用的免费音乐源及健康状态。",
        {"type": "object", "properties": {}},
        _list_sources,
    )

    server.add_tool(
        "local_player_control",
        (
            "控制本机媒体播放器（mpv/vlc）播放/停止，仅当 LOCAL_PLAYER 配置启用时有效。"
            "用于电脑端本地播放场景；小智硬件播放走 play_song 的音频资源，不需要此工具。"
        ),
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "stop", "status"],
                    "description": "操作类型",
                },
                "url": {"type": "string", "description": "action=play 时必填的音频地址"},
            },
            "required": ["action"],
        },
        _local_player_control,
    )

    async def _read_audio_resource(uri: str, arguments: Optional[dict]) -> list:
        blob = await proxy.read_chunk(arguments)
        return [
            {
                "uri": uri,
                "mimeType": "audio/mpeg",
                "blob": blob,
            }
        ]

    server.add_resource(
        proxy.URI,
        "音频流代理",
        _read_audio_resource,
        mime_type="audio/mpeg",
        description=(
            "通过 HTTP Range 分页读取远程音频。arguments: "
            '{"url": "音频地址", "start": 0, "end": 102400}；'
            "到达末尾返回 base64 编码的 [DONE]。"
        ),
    )
    return server
