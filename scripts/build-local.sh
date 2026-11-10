#!/usr/bin/env bash
# 在本地构建镜像并重启（CI/GHCR 不可用时的兜底）。
#
# 默认使用国内镜像源；如本机可直连官方源，可覆盖：
#   PYTHON_IMAGE=python:3.12-slim APT_MIRROR=deb.debian.org \
#   PIP_INDEX_URL=https://pypi.org/simple ./scripts/build-local.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== 本地构建 =="
docker compose build --pull

echo "== 启动 =="
docker compose up -d --force-recreate

echo "== 状态 =="
docker compose ps
