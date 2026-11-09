# 小智AI 免费音乐 MCP（xiaozhi-music-mcp）

一个专为小智AI（[xiaozhi.me](https://xiaozhi.me) / xiaozhi-esp32-server）设计的免费音乐 MCP 服务器：
AI 可以自主在多个免费音源中搜索歌曲、解析真实播放地址，并通过小智的
`ResourceLink + resources/read` 分页音频流协议把歌曲推送到设备播放。

> 本项目仅用于学习研究。音乐资源来自互联网公开接口，版权归原权利方所有。

## 功能特性

- 🎵 **多音源聚合**：酷我、网易云稳定可用；咪咕、酷狗作为实验性兜底源，播放失败自动换源换歌
- ▶️ **一句话点歌**：`play_song` 自动完成“搜索 → 解析直链 → 返回可播放资源”
- 🔊 **流式播放**：兼容小智 Go 服务端 `resource://read_from_http` 协议，设备端边读边播
  （每页默认 100KB，支持 HTTP Range、Base64 分块与 `[DONE]` 结束标记）
- 🔌 **标准 MCP**：stdio 传输，可接入官方 `mcp_pipe.py` 桥接，也可被任意 MCP 客户端使用
- 🖥️ **本地播放可选**：配置 `LOCAL_PLAYER=mpv|vlc` 后可在电脑本地播放
- 🔒 **安全**：令牌仅存于本地 `.env`（已 gitignore），日志不会输出令牌
- 🧪 **可测试**：16 项离线测试 + 真实音源冒烟脚本，CI 自动运行

## 架构

```text
小智AI (xiaozhi.me / 自建服务端)
        │  WebSocket MCP 接入点 (wss://.../mcp_endpoint/mcp/?token=...)
        ▼
   mcp_pipe.py 桥接（官方方案，断线自动重连）
        │  stdio JSON-RPC
        ▼
   music_mcp  (python -m music_mcp)
        │
        ├─ 工具: search_song / play_song / get_play_url / list_sources / local_player_control
        ├─ 资源: resource://read_from_http
        │        └─ HTTP Range 分页读取远程音频（Base64 Blob + [DONE]）
        └─ 音源适配器: kuwo / netease / migu / kugou
```

## 快速开始

### 环境要求

- Python 3.10+（推荐 3.13，项目自带 `.python-version`）
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip + venv

### 安装

```bash
git clone https://github.com/ABUGG-007/xiaozhi-mcp-music.git
cd xiaozhi-music-mcp

# uv 方式（Windows）
uv venv .venv --python 3.13
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt

# Windows 也可以直接一键安装
setup.bat
```

### 配置

```bash
cp .env.example .env          # Windows: copy .env.example .env
cp mcp_config.example.json mcp_config.json
```

编辑 `.env`：

```env
# 小智控制台 -> 智能体 -> 配置角色页面，复制的专属 MCP 接入点
MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=你的token

# 音源开关（逗号分隔，全部可用）
MUSIC_SOURCES=kuwo,netease,migu,kugou

# 本地播放器（小智硬件播放不需要；仅电脑本地调试用）
LOCAL_PLAYER=none
AUTO_PLAY_LOCAL=false
```

### 连接小智AI

1. 登录小智控制台，进入智能体的**配置角色**页面，获取该智能体专属的 MCP 接入点
   （`wss://.../mcp/?token=...`），填入 `.env`；
2. 在项目根目录运行：

```bash
python bridge.py            # Windows 直接双击 run.bat
```

看到日志 `Successfully connected to WebSocket server` 即接入成功；
随后远端会自动完成 `initialize`、`tools/list` 握手，并每 60 秒发送 `ping` 心跳。

3. 建议把 [xiaozhi_role_prompt.txt](xiaozhi_role_prompt.txt) 的内容粘贴到角色配置，
   让大模型知道如何自主搜歌、点歌。

### 单独调试 MCP 服务

```bash
python -m music_mcp                          # stdio 模式
python scripts/check_sources.py "青花瓷"      # 音源自检
$env:SMOKE_QUERY="青花瓷"; python scripts/mcp_smoke.py   # 协议冒烟测试
```

## 可用工具

| 工具 | 说明 | 主要参数 |
|---|---|---|
| `search_song` | 在全部/指定音源搜索歌曲 | `query`（歌名/歌手）、`source`、`limit` |
| `play_song` | 搜索并播放；返回文本 + ResourceLink 音频资源 | `query`、`song_id`、`source` |
| `get_play_url` | 只解析播放直链，不播放 | `query`、`song_id`、`source` |
| `list_sources` | 查看可用音源与健康状态 | 无 |
| `local_player_control` | 电脑本地播放器控制（可选） | `action`、`url` |

工具与参数命名遵循小智官方规范：名称清晰、描述完整、返回值简短（设备端有长度限制）。

## 音源说明

| 音源 | 状态 | 说明 |
|---|---|---|
| 酷我音乐 `kuwo` | ✅ 稳定 | 搜索与播放地址均可正常解析；部分热门歌曲仅提供试听片段 |
| 网易云 `netease` | ⚠️ 受限 | 搜索正常；外链接口已不稳定，常返回 HTML 错误页 |
| 咪咕音乐 `migu` | ⚠️ 实验 | 搜索正常；完整播放需要 App 签名，多数歌曲只能返回失败 |
| 酷狗音乐 `kugou` | ⚠️ 实验 | 搜索正常；版权歌曲播放地址为空，作为兜底 |
| 哔哩哔哩 `bilibili` | ✅ 推荐 | 搜索音乐视频；服务端用 ffmpeg 转码为 MP3 后由内置流服务提供 |

`bilibili` 音源的音频不是 MP3（DASH/AAC），且直链带时效签名，无法直接交给设备，
因此由内置 HTTP 流服务（`STREAM_PORT`，默认 8099）用 ffmpeg 转码为
单声道 24kHz MP3 并支持 Range 分页读取，转码结果按视频 ID 缓存。

> 使用 B站音源需要：本机安装 `ffmpeg`；设备能访问 `STREAM_PUBLIC_HOST:STREAM_PORT`
> （同一局域网，或做端口映射）。

接口方案参考 LX Music / MusicFree 等开源社区项目，可能随时间变化；
服务器会在播放失败时自动尝试下一候选歌曲/音源。

## 音频流协议（小智兼容）

`play_song` 返回的 `ResourceLink`：

```json
{
  "type": "resource_link",
  "uri": "resource://read_from_http",
  "name": "青花瓷 - 周杰伦",
  "description": "https://...mp3",
  "mimeType": "audio/mpeg"
}
```

客户端随后调用 `resources/read`：

```json
{
  "uri": "resource://read_from_http",
  "arguments": {"url": "https://...mp3", "start": 0, "end": 102400}
}
```

服务端返回 `BlobResourceContents`（Base64），流末尾返回 `[DONE]`。
该协议与 [xiaozhi-esp32-server-golang 的 MCP 音频示例](https://github.com/hackers365/xiaozhi-esp32-server-golang/tree/main/examples/mcp_audio) 一致。

## 项目结构

```text
.
├── music_mcp/                 # MCP 服务器核心
│   ├── mcp_server.py          # 轻量 JSON-RPC stdio 协议实现
│   ├── app.py                 # 工具装配（search/play/list）
│   ├── resource_proxy.py      # 音频分页读取代理
│   ├── local_player.py        # 可选本地播放器
│   └── sources/               # 音源适配器（kuwo/netease/migu/kugou）
├── scripts/                   # 自检与冒烟测试脚本
├── tests/                     # 离线单元/集成测试（16 项）
├── mcp_pipe.py                # 官方桥接（WebSocket <-> stdio）
├── bridge.py                  # 一键启动桥接
├── mcp_config.example.json    # 桥接配置模板
├── setup.bat / run.bat        # Windows 安装/运行脚本
├── start_bridge_hidden.ps1    # 后台隐藏启动
├── enable_autostart.ps1/.bat  # 开机自启
├── xiaozhi_role_prompt.txt    # 小智角色提示词
└── pyproject.toml             # 打包与测试配置
```

## 测试

```bash
# 离线测试：协议握手、工具注册、Range 分页、416 结束、
#          无 Range 服务器防死循环、酷我特殊返回格式解析
python -m pytest tests -q
```

CI（GitHub Actions）会在每次 push/PR 时自动运行全部测试。

## 常见问题

**搜索有结果但播放失败**
多为版权限制（VIP/下架），服务会自动换下一候选；也可在 `.env` 中把
`MUSIC_SOURCES` 调整为 `kuwo,netease`。

**小智控制台看不到服务**
确认 `.env` 中 `MCP_ENDPOINT` 正确、日志显示 `Successfully connected to WebSocket server`，
并在控制台刷新“启用的服务”列表。

**中文乱码**
协议消息均为 UTF-8；终端乱码通常是 PowerShell 代码页导致，先执行 `chcp 65001`。

**如何更新音源接口**
接口失效时，在 `music_mcp/sources/` 对应适配器中更新请求参数即可；
欢迎提交 PR 保持音源可用。

## 贡献

欢迎提交 Issue 和 Pull Request，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 免责声明

本项目仅用于学习研究，音乐资源来自互联网公开接口，版权归原权利方所有；
请遵守相关法律法规，勿用于商业用途或传播受版权保护的内容。

## License

[MIT](LICENSE)
