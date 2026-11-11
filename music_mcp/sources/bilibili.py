"""B 站音源适配器。

思路：
- 用 B 站公开搜索接口找视频（音乐区/综合区）。
- 用 view + playurl 接口取 DASH 音频流（fMP4/AAC）。
- B 站音频不是 MP3，且 URL 带时效签名，不能直接交给设备；
  因此由内置 HTTP 服务（music_mcp.stream_server）用 ffmpeg 实时转码为 MP3 供设备拉流。

本模块只负责「搜索 + 解析出可转码的音频源地址 + 提供设备可访问的本地流 URL」。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from music_mcp.config import settings
from music_mcp.models import PlayResult, Song
from music_mcp.utils import clean_text

from .base import MusicSource, SourceError

logger = logging.getLogger("music_mcp.sources.bilibili")

_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"

# 明显不是歌曲视频的标题关键词（B 站音乐区之外的内容常混入）。
_NON_SONG_KEYWORDS = (
    "vlog", "路透", "机场", "出发", "抵达", "快闪", "活动", "花絮", "幕后",
    "采访", "访谈", "综艺", "reaction", "直播回放", "演唱会", "巡演",
    "教学", "教程", "讲解", "指弹", "吉他谱", "钢琴课", "琴谱", "光遇",
    "reaction", "街拍", "生图", "颜值", "混剪", "盘点", "合集",
)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


# 提取歌手时要去掉的修饰词（按长度降序匹配）。
_ARTIST_NOISE = (
    "百万豪装录音棚大声听", "百万录音棚", "录音棚", "无损音质", "无损", "音质",
    "新歌", "官方", "完整版", "纯享", "高清", "超清", "修复", "现场", "live",
    "翻唱", "cover", "mv", "hi-res", "hires", "4k", "8k", "1080p", "重制",
    "全mv", "全曲", "纯妹妹", "经典", "精选", "合集",
    # 「周深神作」「周深封神之作」等标题修饰，去掉后才是歌手名。
    "封神之作", "倾情演唱", "神作", "封神", "之作", "倾情", "献唱", "演绎",
)
# 「歌手《歌名》」：歌手在书名号之前，最多 12 个字符。
_ARTIST_BEFORE_BOOK_TITLE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9·&、]{2,12})\s*[《【]"
)
# 「《歌名》- 歌手」：书名号之后紧跟歌手名。
_ARTIST_AFTER_BOOK_TITLE = re.compile(
    r"[》】]\s*[-—–·]\s*([\u4e00-\u9fffA-Za-z0-9·&、]{2,12})"
)


def _extract_artist_from_title(title: str) -> str:
    """尽量从 B 站标题里提取真实歌手名。

    B 站搜索结果的 author 是 UP 主，不是歌手；真实歌手通常写在标题里，
    形如「单依纯《有趣》」「【4K修复】林志炫《烟花易冷》」。
    提取失败时返回空串。
    """
    text = _strip_tags(title)
    # 先去掉「不含书名号」的方括号块（【4K修复】/【纯享】等），
    # 保留含《》的块，因为「【单依纯《有趣》】」里歌手就在其中。
    text = re.sub(r"[【\[][^】\]《]*[】\]]", " ", text)
    for m in _ARTIST_BEFORE_BOOK_TITLE.finditer(text):
        candidate = m.group(1).strip()
        # 去掉修饰词（可能是前缀也可能是后缀）
        changed = True
        while changed:
            changed = False
            low = candidate.lower()
            for noise in _ARTIST_NOISE:
                if low.endswith(noise) and len(candidate) > len(noise):
                    candidate = candidate[: -len(noise)].strip()
                    changed = True
                    low = candidate.lower()
                elif low.startswith(noise) and len(candidate) > len(noise):
                    candidate = candidate[len(noise):].strip()
                    changed = True
                    low = candidate.lower()
        if _looks_like_artist(candidate):
            return candidate
    # 退而求其次：「《歌名》- 歌手」形式。
    for m in _ARTIST_AFTER_BOOK_TITLE.finditer(text):
        candidate = m.group(1).strip()
        if _looks_like_artist(candidate):
            return candidate
    return ""


def _looks_like_artist(candidate: str) -> bool:
    """过滤明显是描述性短语而非歌手名的候选。"""
    if not (2 <= len(candidate) <= 10):
        return False
    low = candidate.lower()
    # 含描述性词汇的一律排除（这类通常是标题里的修饰语）。
    for bad in ("老师", "解析", "声乐", "合集", "精选", "全mv", "音乐", "现场",
                "合唱", "翻唱", "改编", "教程", "教学", "节目", "综艺",
                "录音棚", "豪装", "大声听", "听", "看", "超", "最", "版"):
        if bad in low:
            return False
    # 真实歌手名很少含标点或超过 10 字；含数字/符号的多为描述。
    if re.search(r"[0-9０-９]", candidate):
        return False
    return True


def _parse_duration(text: str) -> int:
    """把 '3:43' / '1:02:03' 解析为秒。"""
    text = (text or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


class BilibiliSource(MusicSource):
    name = "bilibili"
    display_name = "哔哩哔哩"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=settings.source_timeout,
            follow_redirects=True,
            proxy=settings.http_proxy,
            headers={
                "User-Agent": settings.user_agent,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )

    async def _fetch_items(self, query: str) -> list:
        """按优先级尝试不同检索参数，规避 B 站风控。

        `tids=3 + order=click` 组合检索效果最好，但容易触发风控
        （响应只含 v_voucher、result 为空）。逐级降级：
          1. tids=3 + order=click
          2. tids=3（按默认综合排序）
          3. order=click
          4. 无附加参数
        """
        variants = (
            {"tids": "3", "order": "click"},
            {"tids": "3"},
            {"order": "click"},
            {},
        )
        last_error = ""
        for extra in variants:
            params = {"search_type": "video", "keyword": query, "page": "1"}
            params.update(extra)
            try:
                resp = await self._client.get(_SEARCH_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                last_error = f"B站搜索失败: {exc}"
                continue
            if data.get("code") != 0:
                last_error = f"B站搜索返回异常: {data.get('message')}"
                continue
            items = ((data.get("data") or {}).get("result")) or []
            if items:
                logger.debug(
                    "B站检索命中: keyword=%r params=%s items=%d",
                    query, extra or "none", len(items),
                )
                return items
            # 空结果可能是风控（含 v_voucher），继续降级
            last_error = "B站返回空结果（疑似风控）"
        if last_error:
            logger.warning("B站检索全部降级失败: %s", last_error)
        return []

    async def search(self, query: str, limit: int = 10) -> List[Song]:
        items = await self._fetch_items(query)
        songs: List[Song] = []
        for item in items:
            if item.get("type") != "video":
                continue
            bvid = str(item.get("bvid") or "").strip()
            title = _strip_tags(str(item.get("title") or ""))
            if not bvid or not title:
                continue
            duration = _parse_duration(str(item.get("duration") or ""))
            if duration and duration < 45:
                # 太短的基本是切片/片段，不是完整歌曲
                continue
            # 跳过明显不是歌曲的内容（vlog、路透、活动、教程等）。
            if any(k in title for k in _NON_SONG_KEYWORDS):
                continue
            author = _strip_tags(str(item.get("author") or ""))
            # 优先用标题里提取的真实歌手；失败才退回 UP 主名。
            artist = _extract_artist_from_title(title) or clean_text(author) or "未知"
            songs.append(
                Song(
                    source=self.name,
                    source_name=self.display_name,
                    song_id=bvid,
                    title=clean_text(title),
                    artist=clean_text(artist),
                    album="",
                    duration=duration,
                    cover_url=str(item.get("pic") or "").replace("http://", "https://"),
                    mime_type="audio/mpeg",
                    extra={"uploader": clean_text(author)},
                )
            )
            if len(songs) >= limit:
                break
        return songs

    async def _get_cid(self, bvid: str) -> Optional[int]:
        try:
            resp = await self._client.get(_VIEW_URL, params={"bvid": bvid})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SourceError(f"B站获取视频信息失败: {exc}") from exc
        if data.get("code") != 0:
            raise SourceError(f"B站视频不可用: {data.get('message')}")
        v = data.get("data") or {}
        cid = v.get("cid")
        return int(cid) if cid else None

    async def get_audio_source_url(self, bvid: str) -> str:
        """取 DASH 音频直链（fMP4/AAC），供 ffmpeg 转码使用。"""
        cid = await self._get_cid(bvid)
        if not cid:
            raise SourceError("B站视频缺少 cid")
        params = {"bvid": bvid, "cid": str(cid), "fnval": "16", "fourk": "1"}
        try:
            resp = await self._client.get(
                _PLAYURL_URL,
                params=params,
                headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SourceError(f"B站获取播放地址失败: {exc}") from exc
        if data.get("code") != 0:
            raise SourceError(f"B站播放地址异常: {data.get('message')}")
        dash = ((data.get("data") or {}).get("dash")) or {}
        audios = dash.get("audio") or []
        if not audios:
            raise SourceError("B站未返回音频流（可能需要登录或该视频无音轨）")
        audios.sort(key=lambda a: int(a.get("bandwidth") or 0))
        best = audios[-1]
        url = best.get("baseUrl") or best.get("base_url") or ""
        if not url:
            raise SourceError("B站音频流地址为空")
        return url

    async def get_play_url(self, song: Song) -> PlayResult:
        """返回设备可拉取的本地转码流地址（由 stream_server 提供）。

        先完成转码再返回：设备随后会立刻拉流，若此时还在转码，
        验证/首次读取会失败并被误判为「不可播放」。
        """
        from music_mcp import stream_server
        from music_mcp.library import library

        # 曲库命中：直接复用持久文件，无需联网与转码。
        if library.has_file(song.song_id):
            library.mark_played(song.song_id)
            return PlayResult(
                url=stream_server.build_library_url(song.song_id),
                mime_type="audio/mpeg",
                note="本地曲库",
                extra_source_url=library.get(song.song_id).get("source_url", ""),
            )

        source_url = await self.get_audio_source_url(song.song_id)
        # 转码并保存到曲库（持久化，后续可离线重播）。
        await stream_server._transcode_and_store(
            song.song_id,
            source_url,
            title=song.title,
            artist=song.artist,
            duration=song.duration,
        )
        return PlayResult(
            url=stream_server.build_library_url(song.song_id),
            mime_type="audio/mpeg",
            note="哔哩哔哩(已存入曲库)",
            extra_source_url=source_url,
        )
