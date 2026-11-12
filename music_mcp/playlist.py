"""持久化播放列表：服务端持有并保存设备端应播放的歌曲清单。

设计：
- 与 Library 类似，使用 RLock + JSON 原子写（tmp + replace），文件放在
  LIBRARY_DIR（容器内 /data/library，宿主持久挂载），重启不丢。
- 结构：
    {"items": [{"id","title","artist","url"}...], "repeat": "list", "updated_at": ts}
- 有限容量（LRU）：超过 MAX_ITEMS 时淘汰最早加入/最久未播的尾部条目。
- 设备端通过 self.music.set_queue 一次性接收本列表，之后本地连播。

注意：本模块只负责"权威存档"，实际播放以设备端本地队列为准。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from music_mcp.config import settings

logger = logging.getLogger("music_mcp.playlist")

MAX_ITEMS = 50


class Playlist:
    """服务端持久化播放列表（线程安全，JSON 归档）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dir = settings.library_dir
        self._path = self._dir / "playlist.json"
        self._items: List[dict] = []
        self._repeat: str = "list"
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("items")
            if isinstance(items, list):
                self._items = [i for i in items if isinstance(i, dict)][:MAX_ITEMS]
            repeat = data.get("repeat")
            if repeat in ("off", "list", "one"):
                self._repeat = repeat
        except Exception as exc:
            logger.warning("读取播放列表失败: %s", exc)
            self._items = []

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "items": self._items,
                "repeat": self._repeat,
                "updated_at": int(time.time()),
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.error("写入播放列表失败: %s", exc)

    # ---------- 读写 ----------

    def set_items(self, items: List[dict], repeat: Optional[str] = None) -> List[dict]:
        """整体替换播放列表（超限保留前 MAX_ITEMS 条）。"""
        cleaned: List[dict] = []
        for i in items:
            if not isinstance(i, dict):
                continue
            url = str(i.get("url") or "").strip()
            if not url:
                continue
            cleaned.append(
                {
                    "id": str(i.get("id") or ""),
                    "title": str(i.get("title") or ""),
                    "artist": str(i.get("artist") or ""),
                    "url": url,
                }
            )
        with self._lock:
            self._items = cleaned[:MAX_ITEMS]
            if repeat in ("off", "list", "one"):
                self._repeat = repeat
            self._save()
            return list(self._items)

    def items(self) -> List[dict]:
        with self._lock:
            return list(self._items)

    def urls(self) -> List[str]:
        with self._lock:
            return [i["url"] for i in self._items if i.get("url")]

    def repeat(self) -> str:
        with self._lock:
            return self._repeat

    def set_repeat(self, mode: str) -> bool:
        if mode not in ("off", "list", "one"):
            return False
        with self._lock:
            self._repeat = mode
            self._save()
        return True

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "count": len(self._items),
                "repeat": self._repeat,
                "dir": str(self._dir),
            }


playlist = Playlist()
