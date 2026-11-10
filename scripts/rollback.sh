#!/usr/bin/env bash
# 回滚到指定镜像 tag。
#
# 用法：
#   ./scripts/rollback.sh sha-abc1234
#   ./scripts/rollback.sh v0.1.0
#
# 可通过 `docker images ghcr.io/qmwd2006/xiaozhi-mcp-music` 查看本地已有 tag。
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
    echo "用法: $0 <tag>" >&2
    echo "本地已有 tag:" >&2
    docker images ghcr.io/qmwd2006/xiaozhi-mcp-music --format '  {{.Tag}}' >&2 || true
    exit 1
fi

exec "$(dirname "$0")/update.sh" "$1"
