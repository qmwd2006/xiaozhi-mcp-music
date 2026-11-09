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

# 只保留可能包含完整歌曲的视频标题特征
_MUSIC_HINTS = ("音乐", "歌曲", "无损", "音质", "hi-res", "hires", "mv", "官方", "音频")


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


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

    async def search(self, query: str, limit: int = 10) -> List[Song]:
        params = {
            "search_type": "video",
            "keyword": query,
            "page": "1",
        }
        try:
            resp = await self._client.get(_SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SourceError(f"B站搜索失败: {exc}") from exc

        if data.get("code") != 0:
            raise SourceError(f"B站搜索返回异常: {data.get('message')}")

        items = ((data.get("data") or {}).get("result")) or []
        songs: List[Song] = []
        for item in items:
            if item.get("type") != "video":
                continue
            bvid = str(item.get("bvid") or "").strip()
            title = _strip_tags(str(item.get("title") or ""))
            if not bvid or not title:
                continue
            # 过滤明显不是完整歌曲的（教程/讲解/翻唱合集等仍需靠标题判断，这里只做粗筛）
            lower = title.lower()
            if not any(h in lower for h in _MUSIC_HINTS) and "翻唱" not in title:
                # 仍然保留，但排在后面；这里简单跳过纯教程类
                if any(k in title for k in ("教学", "教程", "讲解", "指弹", "吉他谱", "钢琴课")):
                    continue
            duration = _parse_duration(str(item.get("duration") or ""))
            if duration and duration < 45:
                # 太短的基本是切片/片段，不是完整歌曲
                continue
            author = _strip_tags(str(item.get("author") or ""))
            songs.append(
                Song(
                    source=self.name,
                    source_name=self.display_name,
                    song_id=bvid,
                    title=clean_text(title),
                    artist=clean_text(author) or "未知",
                    album="",
                    duration=duration,
                    cover_url=str(item.get("pic") or "").replace("http://", "https://"),
                    mime_type="audio/mpeg",
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

        source_url = await self.get_audio_source_url(song.song_id)
        # 预热：完成转码落盘，保证本地流立即可读。
        await stream_server.ensure_cached(song.song_id, source_url)
        local_url = stream_server.build_stream_url(song.song_id)
        return PlayResult(
            url=local_url,
            mime_type="audio/mpeg",
            note="哔哩哔哩(本地转码)",
            extra_source_url=source_url,
        )
