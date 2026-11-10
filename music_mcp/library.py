"""本地曲库：持久化已转码的音频与元数据，支持语音管理。

设计：
- 音频文件与索引都放在 LIBRARY_DIR（默认 ./library），独立于临时缓存，
  不会被自动清理，可长期保留、随时重播。
- 索引为 index.json，记录 bvid/标题/歌手/时长/大小/加入时间/播放次数。
- 曲库文件通过内置流服务 /library/{bvid}.mp3 提供给设备。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from music_mcp.config import settings

logger = logging.getLogger("music_mcp.library")


class Library:
    """本地曲库管理器（线程安全，JSON 索引）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dir = settings.library_dir
        self._index_path = self._dir / "index.json"
        self._items: Dict[str, dict] = {}
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._index_path.exists():
            self._items = {}
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._items = {
                    str(k): v for k, v in data.items() if isinstance(v, dict)
                }
            else:
                self._items = {}
        except Exception as exc:
            logger.warning("读取曲库索引失败: %s", exc)
            self._items = {}

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._index_path)
        except Exception as exc:
            logger.error("写入曲库索引失败: %s", exc)

    # ---------- 路径 ----------

    @property
    def directory(self) -> Path:
        return self._dir

    def file_path(self, bvid: str) -> Path:
        return self._dir / f"{bvid}.mp3"

    def has_file(self, bvid: str) -> bool:
        p = self.file_path(bvid)
        return p.exists() and p.stat().st_size > 0

    # ---------- 增删改查 ----------

    def add(
        self,
        bvid: str,
        title: str,
        artist: str,
        duration: int = 0,
        source_url: str = "",
    ) -> Optional[dict]:
        """登记一首歌（音频文件需已存在于曲库目录）。"""
        if not bvid:
            return None
        if not self.has_file(bvid):
            logger.debug("曲库登记跳过（文件不存在）: %s", bvid)
            return None
        with self._lock:
            now = int(time.time())
            old = self._items.get(bvid) or {}
            item = {
                "bvid": bvid,
                "title": title or old.get("title") or bvid,
                "artist": artist or old.get("artist") or "",
                "duration": int(duration or old.get("duration") or 0),
                "size": self.file_path(bvid).stat().st_size,
                "added_at": old.get("added_at") or now,
                "updated_at": now,
                "play_count": int(old.get("play_count") or 0),
                "last_played_at": int(old.get("last_played_at") or 0),
                "source_url": source_url or old.get("source_url") or "",
            }
            self._items[bvid] = item
            self._save()
            return item

    def get(self, bvid: str) -> Optional[dict]:
        with self._lock:
            return self._items.get(bvid)

    def mark_played(self, bvid: str) -> None:
        with self._lock:
            item = self._items.get(bvid)
            if not item:
                return
            item["play_count"] = int(item.get("play_count") or 0) + 1
            item["last_played_at"] = int(time.time())
            self._save()

    def remove(self, bvid: str) -> bool:
        """从曲库删除（含音频文件）。"""
        with self._lock:
            existed = bvid in self._items
            self._items.pop(bvid, None)
            path = self.file_path(bvid)
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("删除曲库文件失败 %s: %s", path, exc)
            if existed:
                self._save()
            return existed

    def clear(self) -> int:
        """清空曲库（删除所有音频与索引）。"""
        with self._lock:
            count = len(self._items)
            for bvid in list(self._items.keys()):
                try:
                    self.file_path(bvid).unlink(missing_ok=True)
                except Exception:
                    pass
            self._items = {}
            self._save()
            return count

    # ---------- 查询 ----------

    def list(self, limit: int = 50) -> List[dict]:
        with self._lock:
            items = list(self._items.values())
        items.sort(key=lambda x: x.get("added_at") or 0, reverse=True)
        return items[:limit]

    def search(self, query: str, limit: int = 20) -> List[dict]:
        """按关键词搜索曲库，支持同音字容错。

        语音识别常把歌名同音字写错（如「珠玉」-> 「朱玉」），
        因此匹配时同时比较原文字符与拼音，避免因一个字写错搜不到。
        """
        q = (query or "").strip().lower()
        with self._lock:
            items = list(self._items.values())
        if not q:
            return self.list(limit)

        tokens = [t for t in q.replace("的", " ").split() if t]
        token_keys = [(t, self._pinyin_key(t)) for t in tokens]
        matched = []
        for item in items:
            haystack = f"{item.get('title','')} {item.get('artist','')}".lower()
            hay_key = self._pinyin_key(haystack)
            if all(
                token in haystack or (key and key in hay_key)
                for token, key in token_keys
            ):
                matched.append(item)
        matched.sort(key=lambda x: x.get("added_at") or 0, reverse=True)
        return matched[:limit]

    @staticmethod
    def _pinyin_key(text: str) -> str:
        """把文本转成无分隔的拼音串，用于同音字比较。"""
        try:
            from pypinyin import lazy_pinyin
        except ImportError:
            return ""
        cleaned = "".join(ch if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else " "
                          for ch in (text or "").lower())
        if not cleaned.strip():
            return ""
        return "".join(lazy_pinyin(cleaned))

    def stats(self) -> dict:
        with self._lock:
            items = list(self._items.values())
        total_bytes = sum(int(i.get("size") or 0) for i in items)
        return {
            "count": len(items),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / 1024 / 1024, 1),
            "dir": str(self._dir),
        }


library = Library()
