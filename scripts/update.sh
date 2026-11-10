#!/usr/bin/env bash
# 更新到指定镜像 tag（默认 latest）。
#
# 用法：
#   ./scripts/update.sh                 # 跟随 MUSIC_MCP_TAG 或 latest
#   ./scripts/update.sh sha-abc1234     # 固定到某个提交
#   ./scripts/update.sh v0.2.0          # 固定到某个版本
#
# 数据目录（library/cache）为 bind mount，更新不会丢失。
set -euo pipefail

cd "$(dirname "$0")/.."

TAG="${1:-}"
if [[ -n "$TAG" ]]; then
    # 写入 .env，compose 通过 ${MUSIC_MCP_TAG} 读取
    if grep -q '^MUSIC_MCP_TAG=' .env 2>/dev/null; then
        sed -i "s|^MUSIC_MCP_TAG=.*|MUSIC_MCP_TAG=${TAG}|" .env
    else
        printf '\nMUSIC_MCP_TAG=%s\n' "$TAG" >> .env
    fi
    echo "已固定镜像 tag: ${TAG}"
fi

echo "== 拉取镜像 =="
docker compose pull

echo "== 重启容器 =="
docker compose up -d

echo "== 状态 =="
docker compose ps
echo
echo "查看日志： docker compose logs -f --tail=100"
