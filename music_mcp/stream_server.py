"""内置音频流服务。

用途：为设备提供可拉取的 MP3 流。主要用于 B 站这类「源音频不是 MP3、
且直链带时效签名」的音源：由本服务用 ffmpeg 实时转码并分块输出。

设计要点：
- 单进程 aiohttp 服务，监听 0.0.0.0:STREAM_PORT（默认 8099）。
- GET /stream/bili/{bvid}.mp3  -> 拉 B 站音频流，ffmpeg 转 单声道 24kHz MP3。
- 支持 HTTP Range（设备/服务端可能用 Range 分页拉取）。
- 转码结果按 bvid 落盘缓存（可配置），避免重复转码。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from aiohttp import web

from music_mcp.config import settings
from music_mcp.library import library

logger = logging.getLogger("music_mcp.stream_server")

_runner: Optional[web.AppRunner] = None
_started_port: int = 0


def _ffmpeg_path() -> str:
    return os.getenv("FFMPEG", "ffmpeg")


def _cache_dir() -> Path:
    d = settings.cache_dir / "stream"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_stream_url(bvid: str) -> str:
    """构造设备可访问的转码流地址（临时转码）。"""
    host = settings.stream_public_host
    port = _started_port or settings.stream_port
    return f"http://{host}:{port}/stream/bili/{bvid}.mp3"


def build_library_url(bvid: str) -> str:
    """构造设备可访问的曲库播放地址（持久文件，优先使用）。"""
    host = settings.stream_public_host
    port = _started_port or settings.stream_port
    return f"http://{host}:{port}/library/{bvid}.mp3"


async def _get_bili_audio_url(bvid: str) -> str:
    from music_mcp.sources.bilibili import BilibiliSource

    src = BilibiliSource()
    try:
        return await src.get_audio_source_url(bvid)
    finally:
        await src._client.aclose()


async def _transcode_to_cache(bvid: str, upstream: str) -> Path:
    """把上游音频转成单声道 24kHz MP3 落盘，返回文件路径。

    若曲库中已有该曲，直接复用曲库文件（并同步到临时缓存目录）。
    """
    target = _cache_dir() / f"{bvid}.mp3"
    if target.exists() and target.stat().st_size > 0:
        return target

    # 曲库优先：已下载过的歌曲无需再次转码。
    lib_file = library.file_path(bvid)
    if lib_file.exists() and lib_file.stat().st_size > 0:
        try:
            shutil.copy2(lib_file, target)
            logger.info("复用曲库文件 %s", lib_file.name)
            return target
        except Exception as exc:
            logger.warning("复用曲库文件失败 %s: %s", lib_file, exc)

    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".mp3", dir=str(_cache_dir()))
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    headers = (
        "Referer: https://www.bilibili.com/\r\n"
        f"User-Agent: {settings.user_agent}\r\n"
    )
    cmd = [
        _ffmpeg_path(), "-v", "error", "-y",
        "-headers", headers,
        "-i", upstream,
        "-ac", "1", "-ar", "24000",
        "-c:a", "libmp3lame", "-b:a", "64k",
        "-f", "mp3", str(tmp_path),
    ]
    logger.info("开始转码 B站音频 %s -> %s", bvid, target.name)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg 转码失败: {stderr.decode('utf-8', 'ignore')[:300]}"
        )
    tmp_path.replace(target)
    logger.info("转码完成 %s (%d bytes)", bvid, target.stat().st_size)
    return target


async def _transcode_and_store(
    bvid: str, upstream: str, title: str = "", artist: str = "", duration: int = 0
) -> Path:
    """转码并保存到曲库（持久化），返回曲库文件路径。"""
    target = _cache_dir() / f"{bvid}.mp3"
    if not (target.exists() and target.stat().st_size > 0):
        await _transcode_to_cache(bvid, upstream)
    # 复制到曲库目录
    lib_path = library.file_path(bvid)
    if not (lib_path.exists() and lib_path.stat().st_size > 0):
        lib_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, lib_path)
    library.add(bvid, title=title, artist=artist, duration=duration,
                source_url=upstream)
    return lib_path


async def _stream_file(request: web.Request, path: Path) -> web.StreamResponse:
    """按 Range 返回文件内容（与 resource://read_from_http 兼容）。"""
    size = path.stat().st_size
    range_header = request.headers.get("Range", "")
    start, end = 0, size - 1
    status = 200

    if range_header.startswith("bytes="):
        rng = range_header[len("bytes="):].split(",")[0].strip()
        if "-" in rng:
            a, b = rng.split("-", 1)
            if a:
                start = int(a)
            if b:
                end = int(b)
            else:
                end = size - 1
            if start >= size:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            status = 206

    length = end - start + 1
    resp = web.StreamResponse(
        status=status,
        headers={
            "Content-Type": "audio/mpeg",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
        },
    )
    await resp.prepare(request)
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            await resp.write(chunk)
            remaining -= len(chunk)
    await resp.write_eof()
    return resp


async def ensure_cached(bvid: str, upstream: str) -> Path:
    """确保 bvid 已完成转码落盘；已缓存则直接返回。"""
    cached = _cache_dir() / f"{bvid}.mp3"
    if cached.exists() and cached.stat().st_size > 0:
        return cached
    return await _transcode_to_cache(bvid, upstream)


async def _handle_bili(request: web.Request) -> web.StreamResponse:
    bvid = request.match_info["bvid"]
    try:
        # 曲库优先：已保存的歌曲直接播放，不重新转码。
        lib_path = library.file_path(bvid)
        if lib_path.exists() and lib_path.stat().st_size > 0:
            library.mark_played(bvid)
            return await _stream_file(request, lib_path)
        cached = _cache_dir() / f"{bvid}.mp3"
        if cached.exists() and cached.stat().st_size > 0:
            return await _stream_file(request, cached)
        upstream = await _get_bili_audio_url(bvid)
        path = await _transcode_to_cache(bvid, upstream)
        return await _stream_file(request, path)
    except Exception as exc:
        logger.exception("B站流处理失败 %s", bvid)
        return web.Response(status=502, text=f"stream failed: {exc}")


async def _handle_library(request: web.Request) -> web.StreamResponse:
    """提供曲库中的持久音频文件。"""
    bvid = request.match_info["bvid"]
    path = library.file_path(bvid)
    if not (path.exists() and path.stat().st_size > 0):
        return web.Response(status=404, text="not in library")
    library.mark_played(bvid)
    return await _stream_file(request, path)


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "port": _started_port})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/stream/bili/{bvid}.mp3", _handle_bili)
    app.router.add_get("/library/{bvid}.mp3", _handle_library)
    app.router.add_get("/health", _handle_health)
    return app


async def start() -> int:
    """启动流服务，返回实际监听端口。已启动则直接返回。"""
    global _runner, _started_port
    if _runner is not None:
        return _started_port

    if shutil.which(_ffmpeg_path()) is None:
        logger.warning(
            "未找到 ffmpeg（%s），B站音源将不可用；请安装 ffmpeg 或设置 FFMPEG 环境变量",
            _ffmpeg_path(),
        )

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.stream_port)
    await site.start()
    _runner = runner
    _started_port = settings.stream_port
    logger.info("音频流服务已启动: http://0.0.0.0:%d", _started_port)
    return _started_port


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
