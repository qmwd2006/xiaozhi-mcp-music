"""数据模型：歌曲、播放结果等。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict


@dataclass
class Song:
    """一首歌曲的通用表示，任何音源适配器都应返回该结构。"""

    source: str                 # 音源标识，如 kuwo / netease / migu / kugou
    source_name: str            # 音源显示名，如 酷我音乐
    song_id: str                # 音源内歌曲 ID
    title: str                  # 歌名
    artist: str                 # 歌手（多个歌手用 / 分隔）
    album: str = ""             # 专辑
    duration: int = 0           # 时长（秒）
    cover_url: str = ""         # 封面图
    mime_type: str = "audio/mpeg"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Song":
        known = {
            "source", "source_name", "song_id", "title", "artist",
            "album", "duration", "cover_url", "mime_type",
        }
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(
            source=d.get("source", ""),
            source_name=d.get("source_name", ""),
            song_id=d.get("song_id", ""),
            title=d.get("title", ""),
            artist=d.get("artist", ""),
            album=d.get("album", ""),
            duration=d.get("duration", 0),
            cover_url=d.get("cover_url", ""),
            mime_type=d.get("mime_type", "audio/mpeg"),
            extra=extra,
        )


@dataclass
class PlayResult:
    """解析后的可播放音频地址。"""

    url: str
    mime_type: str = "audio/mpeg"
    bitrate: int = 0
    note: str = ""
    # 某些音源（如 B 站）需要服务端二次转码：这里保存上游原始音频地址，
    # 供内置 stream_server 拉流转码使用。
    extra_source_url: str = ""
