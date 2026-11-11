# 小智音乐 MCP 运行镜像。
#
# 默认使用官方 Python 镜像；国内构建可通过 build args 换成镜像站：
#   docker compose build \
#     --build-arg PYTHON_IMAGE=docker.1panel.live/library/python:3.12-slim \
#     --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#
# ffmpeg 是 B 站等音源转码的硬依赖，必须装进镜像。

ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 换 apt 源（Debian bookworm/trixie 使用 deb822 的 .sources 文件）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        tzdata; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，充分利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY . .

# 以非 root 运行；挂载目录由 compose 的 user/权限处理
RUN set -eux; \
    if ! getent group "${APP_GID}" >/dev/null; then addgroup --gid "${APP_GID}" app; fi; \
    if ! getent passwd "${APP_UID}" >/dev/null; then \
        adduser --uid "${APP_UID}" --gid "${APP_GID}" --disabled-password --gecos "" app; \
    fi; \
    mkdir -p /data/cache /data/library; \
    chown -R "${APP_UID}:${APP_GID}" /app /data

USER ${APP_UID}:${APP_GID}

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=3).status == 200 else 1)"

CMD ["python3", "mcp_pipe.py"]
