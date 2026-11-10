#!/usr/bin/env python3
"""切换 MCP 智能体配置，并重启音乐 MCP 桥接服务。

用法：
    ./switch_env.sh real   # 真机（agentId 2620974）
    ./switch_env.sh sim    # 模拟设备（agentId 2622661）
    ./switch_env.sh status # 查看当前指向

说明：.env.real / .env.sim 是两套完整配置（含各自 MCP_ENDPOINT），
切换时覆盖 .env 并重启 mcp_pipe（连带重启 music_mcp 子进程）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PYTHON = PROJECT_DIR / ".venv/bin/python"
LOG = PROJECT_DIR / "logs/mcp_pipe.log"


def agent_id_of(env_file: Path) -> str:
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return "?"
    m = re.search(r"token=([^\s]+)", text)
    if not m:
        return "?"
    payload = m.group(1).split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return str(json.loads(base64.urlsafe_b64decode(payload))["agentId"])
    except Exception:
        return "?"


def _pids(pattern: str) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    )
    return [int(x) for x in result.stdout.split() if x.isdigit()]


def stop_service() -> None:
    for pattern in ("mcp_pipe.py", "music_mcp"):
        for pid in _pids(pattern):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    for _ in range(20):
        if not _pids("mcp_pipe.py"):
            return
        time.sleep(0.5)


def start_service() -> bool:
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    with LOG.open("ab") as fh:
        subprocess.Popen(
            [str(PYTHON), "mcp_pipe.py"],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(6)
    text = LOG.read_text(encoding="utf-8", errors="ignore")
    if "Successfully connected" not in text:
        print("警告：未在日志中看到连接成功", file=sys.stderr)
        print("\n".join(text.splitlines()[-10:]), file=sys.stderr)
        return False
    print("服务已启动并连接成功")
    for line in text.splitlines():
        if "音源" in line or "音频流服务" in line:
            print("  " + line.split(" - ", 1)[-1])
    return True


def main() -> int:
    os.chdir(PROJECT_DIR)
    action = sys.argv[1] if len(sys.argv) > 1 else "status"

    if action in ("real", "sim"):
        src = PROJECT_DIR / f".env.{action}"
        if not src.exists():
            print(
                f"缺少 {src.name}。请先创建：把当前可用的 MCP_ENDPOINT 保存为该文件",
                file=sys.stderr,
            )
            return 1
        (PROJECT_DIR / ".env").write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"已切换到 {action} 配置（agentId {agent_id_of(src)}）")
        stop_service()
        return 0 if start_service() else 1

    if action == "status":
        print(f"当前 .env agentId: {agent_id_of(PROJECT_DIR / '.env')}")
        for name in (".env.real", ".env.sim"):
            path = PROJECT_DIR / name
            if path.exists():
                print(f"  {name} -> agentId {agent_id_of(path)}")
        pids = subprocess.run(
            ["pgrep", "-af", "mcp_pipe.py|music_mcp"], capture_output=True, text=True
        )
        print(pids.stdout.rstrip() or "  （服务未运行）")
        return 0

    print("用法: switch_env.sh {real|sim|status}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
