"""音乐源管理器：注册、搜索、解析播放地址。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from music_mcp.config import settings
from music_mcp.models import PlayResult, Song

from .base import SourceError, MusicSource
from .kuwo import KuwoSource
from .netease import NeteaseSource
from .migu import MiguSource
from .kugou import KugouSource
from .bilibili import BilibiliSource

logger = logging.getLogger("music_mcp.sources")


def _source_classes() -> Dict[str, type]:
    return {
        "kuwo": KuwoSource,
        "netease": NeteaseSource,
        "migu": MiguSource,
        "kugou": KugouSource,
        "bilibili": BilibiliSource,
    }


class SourceManager:
    def __init__(self, enabled: Optional[List[str]] = None):
        enabled = enabled or settings.enabled_sources
        classes = _source_classes()
        self.sources: Dict[str, MusicSource] = {}
        for name in enabled:
            cls = classes.get(name)
            if cls is None:
                logger.warning("未知音源 %s，已跳过", name)
                continue
            try:
                self.sources[name] = cls()
            except Exception as exc:  # pragma: no cover
                logger.warning("初始化音源 %s 失败: %s", name, exc)
        if not self.sources:
            logger.warning("没有可用的音源，请检查 MUSIC_SOURCES 配置")

    @property
    def names(self) -> List[str]:
        return list(self.sources.keys())

    def get(self, name: str) -> Optional[MusicSource]:
        return self.sources.get(name)

    async def search(
        self,
        query: str,
        limit: int = 8,
        source: Optional[str] = None,
    ) -> List[Song]:
        """在指定或全部音源中搜索。

        多音源时先给每个音源保底分配名额，避免排序靠后的音源（如 B 站）
        因前面的音源占满 limit 而完全丢失；剩余名额再按轮转补齐。
        """
        if not query.strip():
            return []
        names = [source] if source and source in self.sources else self.names
        if not names:
            return []

        # 每个音源至少尝试 2 条，最多不超过 limit，保证覆盖度。
        per_source = max(2, (limit + len(names) - 1) // len(names))
        per_source = min(per_source, limit)

        # 并发查询各音源，避免串行等待拖慢整体响应。
        async def _one(name: str) -> List[Song]:
            try:
                return await self.sources[name].search(query, limit=per_source)
            except Exception as exc:
                logger.warning("音源 %s 搜索失败: %s", name, exc)
                return []

        batches = await asyncio.gather(*(_one(n) for n in names))

        # 轮转合并：每轮从各音源各取一条，保证结果分布均衡。
        results: List[Song] = []
        seen: set = set()
        max_len = max((len(b) for b in batches), default=0)
        for i in range(max_len):
            for batch in batches:
                if i < len(batch):
                    song = batch[i]
                    key = (song.source, song.song_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(song)
                    if len(results) >= limit:
                        return results
        return results

    async def resolve(self, song: Song) -> PlayResult:
        src = self.sources.get(song.source)
        if src is None:
            raise SourceError(f"音源 {song.source} 不可用")
        return await src.get_play_url(song)

    async def health(self, max_age_seconds: int = 300) -> Dict[str, dict]:
        """返回各音源健康状态（带 5 分钟缓存）。"""
        now = time.time()
        if not hasattr(self, "_health_cache"):
            self._health_cache: Dict[str, tuple] = {}
        out: Dict[str, dict] = {}
        for name, src in self.sources.items():
            cached = self._health_cache.get(name)
            if cached and now - cached[0] < max_age_seconds:
                out[name] = cached[1]
                continue
            try:
                songs = await src.search("青花瓷", limit=1)
                ok = bool(songs)
            except Exception:
                ok = False
            info = {
                "ok": ok,
                "name": src.display_name,
            }
            self._health_cache[name] = (now, info)
            out[name] = info
        return out


manager = SourceManager()
