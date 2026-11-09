"""网易云音乐音源（开源社区常用接口方案）。"""

from __future__ import annotations

import logging
from typing import List

import httpx

from music_mcp.config import settings
from music_mcp.models import PlayResult, Song
from music_mcp.utils import clean_text

from .base import MusicSource, SourceError

logger = logging.getLogger("music_mcp.sources.netease")


class NeteaseSource(MusicSource):
    name = "netease"
    display_name = "网易云音乐"

    SEARCH_URL = "https://music.163.com/api/search/get/web"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=settings.source_timeout,
            follow_redirects=False,
            proxy=settings.http_proxy,
            headers={
                "User-Agent": settings.user_agent,
                "Referer": "https://music.163.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )

    async def search(self, query: str, limit: int = 10) -> List[Song]:
        params = {
            "csrf_token": "",
            "s": query,
            "type": "1",
            "offset": "0",
            "limit": str(limit),
        }
        try:
            resp = await self._client.get(self.SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SourceError(f"网易云搜索失败: {exc}") from exc

        if data.get("code") not in (200, None):
            raise SourceError(f"网易云搜索返回异常 code={data.get('code')}")
        songs_data = ((data.get("result") or {}).get("songs")) or []
        songs: List[Song] = []
        for item in songs_data:
            sid = str(item.get("id", ""))
            title = str(item.get("name", "")).strip()
            title = clean_text(title)
            if not sid or not title:
                continue
            artists = item.get("artists") or item.get("ar") or []
            artist = " / ".join(clean_text(a.get("name", "")) for a in artists if a.get("name")) or "未知歌手"
            album = ""
            album_info = item.get("album") or {}
            if isinstance(album_info, dict):
                album = clean_text(album_info.get("name", "") or "")
            try:
                duration = int(item.get("duration", 0) or 0) // 1000
            except (TypeError, ValueError):
                duration = 0
            songs.append(
                Song(
                    source=self.name,
                    source_name=self.display_name,
                    song_id=sid,
                    title=title,
                    artist=artist,
                    album=album,
                    duration=duration,
                    cover_url=clean_text(album_info.get("picUrl", "") or item.get("picUrl", "") or ""),
                )
            )
        return songs

    async def get_play_url(self, song: Song) -> PlayResult:
        """外链方式获取 128k mp3；不走复杂 eapi 加密。"""
        outer_url = f"https://music.163.com/song/media/outer/url?id={song.song_id}.mp3"
        try:
            resp = await self._client.get(outer_url)
        except Exception as exc:
            raise SourceError(f"网易云解析播放地址失败: {exc}") from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if location:
                # 不可用歌曲会被重定向到 /404，这不是可播放地址。
                if location.rstrip("/").endswith("/404"):
                    raise SourceError("网易云该歌曲不可用（重定向到 404）")
                return PlayResult(url=location, mime_type="audio/mpeg", note="网易云外链")
            raise SourceError("网易云外链重定向缺少地址")

        if resp.status_code == 200:
            # 外链接口已不稳定：歌曲不可用时返回 200 + HTML 错误页，
            # 必须按内容类型判断，避免把 HTML 当成音频地址。
            content_type = (resp.headers.get("content-type") or "").lower()
            if "text/html" in content_type:
                raise SourceError("网易云外链不可用（返回 HTML 页面）")
            if content_type.startswith("audio/") or resp.content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3"):
                return PlayResult(url=outer_url, mime_type="audio/mpeg", note="网易云外链")
            raise SourceError("网易云外链返回了非音频内容")

        raise SourceError("网易云该歌曲不可用（可能为 VIP/下架）")
