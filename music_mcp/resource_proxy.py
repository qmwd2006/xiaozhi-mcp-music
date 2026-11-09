"""音频资源代理：通过 HTTP Range 分页读取远程音频。"""

from __future__ import annotations

import base64
import logging
from typing import Optional, Tuple

import httpx

from music_mcp.config import settings

logger = logging.getLogger("music_mcp.resource_proxy")

STREAM_DONE_FLAG = "[DONE]"


class ResourceProxyError(Exception):
    pass


class ResourceProxy:
    """对应小智 Go 服务端的 resource://read_from_http 协议。

    客户端调用 resources/read 并传入 arguments:
      url   - 远程音频地址
      start - 起始字节（默认 0）
      end   - 结束字节（不含，默认 start + 100KB）
    返回 BlobResourceContents(base64)。读到流末尾时返回 base64("[DONE]")。
    """

    URI = "resource://read_from_http"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            proxy=settings.http_proxy,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "*/*",
            },
        )

    def _parse_args(self, arguments: Optional[dict]) -> Tuple[str, int, int]:
        args = arguments or {}
        url = str(args.get("url") or "").strip()
        if not url:
            raise ResourceProxyError("缺少 url 参数")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ResourceProxyError("url 必须为 http(s) 地址")

        def to_int(key: str, default: int) -> int:
            try:
                v = args.get(key)
                if v is None:
                    return default
                return int(v)
            except (TypeError, ValueError):
                return default

        start = max(0, to_int("start", 0))
        end = to_int("end", 0)
        if end <= start:
            end = start + 100 * 1024
        end = min(end, start + settings.max_page_size)
        return url, start, end

    def encode_done(self) -> str:
        return base64.b64encode(STREAM_DONE_FLAG.encode("utf-8")).decode("ascii")

    async def read_chunk(self, arguments: Optional[dict]) -> str:
        """读取一页音频并返回 base64；到达末尾时返回 [DONE] 的 base64。"""
        url, start, end = self._parse_args(arguments)
        headers = {"Range": f"bytes={start}-{end - 1}"}
        try:
            resp = await self._client.get(url, headers=headers)
        except Exception as exc:
            raise ResourceProxyError(f"读取音频失败: {exc}") from exc

        if resp.status_code == 416:
            logger.info("音频读取到末尾 (416): %s", url)
            return self.encode_done()

        if resp.status_code not in (200, 206):
            raise ResourceProxyError(f"读取音频失败 HTTP {resp.status_code}")

        data = resp.content
        if not data:
            return self.encode_done()

        if resp.status_code == 200:
            # 服务器不支持 Range：第一页返回完整文件，后续请求直接结束，
            # 避免客户端反复拿到同一份数据造成死循环。
            if start > 0:
                return self.encode_done()
            return base64.b64encode(data).decode("ascii")

        # 206 Partial Content：按页截取
        page = end - start
        data = data[:page]
        if len(data) < page:
            # 已到文件末尾
            return base64.b64encode(data).decode("ascii")
        return base64.b64encode(data).decode("ascii")

    async def verify_url(self, url: str) -> bool:
        """轻量验证播放地址是否可访问（并确认返回的是音频而非 HTML 错误页）。

        注意：不能盲目跟随重定向。网易云外链对不可用歌曲会 302 到
        `music.163.com/404`，跟随之后会得到一个 200 的 HTML 页面。
        这里对重定向只检查 Location 目标，不实际跟随。
        """
        if not (url.startswith("http://") or url.startswith("https://")):
            return False
        try:
            resp = await self._client.get(url, headers={"Range": "bytes=0-1"})
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location") or resp.headers.get("Location") or ""
                if not location or location.rstrip("/").endswith("/404"):
                    return False
                # 重定向目标是真实音频地址时，再轻量校验一次。
                return await self._verify_audio_url(location)
            return self._is_audio_response(resp)
        except Exception:
            return False

    async def _verify_audio_url(self, url: str) -> bool:
        try:
            resp = await self._client.get(url, headers={"Range": "bytes=0-1"})
        except Exception:
            return False
        return self._is_audio_response(resp)

    @staticmethod
    def _is_audio_response(resp: "httpx.Response") -> bool:
        if resp.status_code not in (200, 206):
            return False
        content_type = (resp.headers.get("content-type") or "").lower()
        # HTML 错误页（如网易云 404）不是音频。
        if "text/html" in content_type:
            return False
        return True


proxy = ResourceProxy()
