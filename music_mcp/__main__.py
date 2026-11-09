"""入口：python -m music_mcp"""

from __future__ import annotations

import asyncio
import logging
import sys

from music_mcp import stream_server
from music_mcp.app import build_server
from music_mcp.config import settings
from music_mcp.utils import setup_logging


async def _run(server) -> None:
    # 内置流服务供 B站等需要服务端转码的音源使用；
    # 即使未启用这些音源，启动也不影响（仅占用一个本地端口）。
    try:
        await stream_server.start()
    except Exception as exc:  # pragma: no cover - 端口占用等
        logging.getLogger("music_mcp").warning("音频流服务启动失败: %s", exc)
    try:
        await server.run_stdio()
    finally:
        await stream_server.stop()


def main() -> int:
    setup_logging(settings.log_level)
    logger = logging.getLogger("music_mcp")
    logger.info("小智音乐 MCP 启动，音源: %s", settings.enabled_sources)
    if "bilibili" in settings.enabled_sources:
        logger.info(
            "B站音源已启用，设备将通过 http://%s:%d 拉流",
            settings.stream_public_host,
            settings.stream_port,
        )
    server = build_server()
    try:
        asyncio.run(_run(server))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
