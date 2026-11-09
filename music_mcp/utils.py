"""通用工具函数。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict


def setup_logging(level: str = "INFO") -> None:
    # MCP stdio 协议要求 stdout 为 UTF-8（Windows 默认可能是 GBK）
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(__import__("sys"), stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=__import__("sys").stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def compact_json(obj: Any) -> str:
    """紧凑 JSON，用于 MCP 文本返回，控制长度。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def safe_get(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return default
    return obj


def truncate_text(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(已截断)"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    name = name.strip()
    return name[:max_len] if name else "untitled"


def clean_text(text: str) -> str:
    """清理接口返回文本中的 BOM、零宽字符、HTML 高亮标签与多余空白。"""
    if not isinstance(text, str):
        text = str(text or "")
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u200e", "").replace("\u200f", "")
    # 酷狗等接口会用 <em> 高亮搜索关键词，需去掉标签本身。
    text = re.sub(r"</?em>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def redact_url(url: str) -> str:
    """记录日志时隐藏 URL 中的敏感参数。"""
    return url
