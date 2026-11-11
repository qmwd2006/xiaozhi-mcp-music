#!/usr/bin/env python3
"""把指定 B 站视频（bvid）直接加入本地曲库。

用于采集脚本过滤过严、或已知具体视频的场景（官方 MV、现场版等）。

用法：
    python scripts/add_song.py BV1wJ411y7mc --title 一念一生 --artist 李健
    python scripts/add_song.py BV1wJ411y7mc --title 一念一生 --artist 李健 --duration 274
    python scripts/add_song.py BV1wJ411y7mc --dry-run      # 只解析音频地址
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from music_mcp import stream_server
from music_mcp.library import library
from music_mcp.sources.bilibili import BilibiliSource

logger = logging.getLogger("add_song")


async def main() -> int:
    parser = argparse.ArgumentParser(description="把指定 bvid 加入曲库")
    parser.add_argument("bvid", help="B 站视频号，例如 BV1wJ411y7mc")
    parser.add_argument("--title", required=False, default="", help="歌名（入库用）")
    parser.add_argument("--artist", required=False, default="", help="歌手（入库用）")
    parser.add_argument("--duration", type=int, default=0, help="时长（秒）")
    parser.add_argument("--dry-run", action="store_true", help="只解析，不下载")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    if library.has_file(args.bvid):
        info = library.get(args.bvid) or {}
        print(f"曲库已存在: {args.bvid} | {info.get('title')} | {info.get('artist')}")
        return 0

    src = BilibiliSource()
    try:
        upstream = await src.get_audio_source_url(args.bvid)
    finally:
        await src._client.aclose()

    if args.dry_run:
        print("音频直链:", upstream[:120] + "...")
        return 0

    await stream_server._transcode_and_store(
        args.bvid,
        upstream,
        title=args.title or args.bvid,
        artist=args.artist,
        duration=args.duration,
    )
    info = library.get(args.bvid) or {}
    print(
        f"已入库: {args.bvid} | {info.get('title')} | {info.get('artist')} "
        f"| {info.get('duration')}s | {info.get('size', 0) // 1024}KB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
