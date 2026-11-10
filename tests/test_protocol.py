"""MCP 协议层测试：initialize / tools / resources。"""

from __future__ import annotations

import json

import pytest

from music_mcp.app import build_server


@pytest.fixture
def server():
    return build_server()


@pytest.mark.asyncio
async def test_initialize(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["serverInfo"]["name"] == "xiaozhi-music-mcp"
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert "resources" in result["capabilities"]


@pytest.mark.asyncio
async def test_unknown_method(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request("foo/bar", {})
    assert resp["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_tools_list_contains_expected(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request("tools/list", {})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"search_song", "play_song", "get_play_url", "list_sources"} <= names


@pytest.mark.asyncio
async def test_tools_call_unknown_tool(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request("tools/call", {"name": "nope", "arguments": {}})
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_search_song_missing_query(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request("tools/call", {"name": "search_song", "arguments": {}})
    assert resp["result"]["isError"] is True
    assert "query" in resp["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_play_song_missing_args_empty_library(server, harness_factory, monkeypatch):
    # 无歌名时从曲库随机挑一首；曲库为空才报错。
    from music_mcp import app as app_module

    monkeypatch.setattr(app_module, "_random_library_songs", lambda count=1: [])
    harness = harness_factory(server)
    resp = await harness.request("tools/call", {"name": "play_song", "arguments": {}})
    assert resp["result"]["isError"] is True
    assert "曲库" in resp["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_resources_list_contains_audio_proxy(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request("resources/list", {})
    uris = {r["uri"] for r in resp["result"]["resources"]}
    assert "resource://read_from_http" in uris


@pytest.mark.asyncio
async def test_resources_read_missing_url(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request(
        "resources/read",
        {"uri": "resource://read_from_http", "arguments": {"start": 0, "end": 10}},
    )
    assert "error" in resp


@pytest.mark.asyncio
async def test_ping(server, harness_factory):
    harness = harness_factory(server)
    resp = await harness.request("ping", {})
    assert resp["result"] == {}
