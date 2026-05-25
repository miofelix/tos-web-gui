FROM python:3.12-slim

# 让 Python 输出立刻刷新到 stdout，方便 docker logs 实时查看。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy

# 一些常用工具 + tosutil 运行时需要的 ca-certificates。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv（用 pip 安装到全局 python 里，简单可靠）。
RUN pip install --no-cache-dir "uv>=0.4"

WORKDIR /app

# 先复制依赖描述，最大化利用 docker layer cache。
# uv.loc[k] 是一个字符类 glob，对应 "uv.lock"，但即使文件不存在也不会让 COPY 失败。
COPY pyproject.toml ./
COPY uv.loc[k] ./

# 有 lockfile 走 --frozen，没有就让 uv 现场解析。
RUN if [ -f uv.lock ]; then \
        uv sync --frozen --no-dev; \
    else \
        uv sync --no-dev; \
    fi

# 项目代码。
COPY app ./app

# tosutil 二进制（用户自备）。两种用法：
#
# 1) 多架构发布（默认）：把对应 Linux 平台的二进制按下面命名放到 tosutils/：
#       tosutils/tosutil-Linux-amd64bit
#       tosutils/tosutil-Linux-arm64bit
#    然后一条 buildx 就能出 multi-arch manifest：
#       docker buildx build --platform linux/amd64,linux/arm64 \
#         -t miofelix/tos-web-gui:0.1.0 -t miofelix/tos-web-gui:latest --push .
#    BuildKit 注入的 TARGETARCH 会让每个 platform 自动选对应那份。
#
# 2) 单架构快速构建：把对应平台的 tosutil 放到根目录 ./tosutil，
#    然后传 --build-arg TOSUTIL_PATH=tosutil 覆盖默认。
ARG TARGETARCH
ARG TOSUTIL_PATH=tosutils/tosutil-Linux-${TARGETARCH}bit
COPY ${TOSUTIL_PATH} /usr/local/bin/tosutil
RUN chmod +x /usr/local/bin/tosutil

# 数据目录：上传缓冲 + 下载落地。建议挂载持久卷到 /data。
RUN mkdir -p /data/uploads /data/downloads

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
