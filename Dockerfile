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

# 项目根目录里必须放一份可执行的 tosutil 二进制（用户自备）。
COPY tosutil /usr/local/bin/tosutil
RUN chmod +x /usr/local/bin/tosutil

# 数据目录：上传缓冲 + 下载落地。建议挂载持久卷到 /data。
RUN mkdir -p /data/uploads /data/downloads

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
