# TOS Web GUI

浏览器里的火山云 TOS 对象存储管理工具。

后端用 **FastAPI**，前端是最小化的原生 HTML/CSS/JS，所有 TOS 操作都通过 **subprocess** 调
用官方 `tosutil` 完成 —— 不使用 S3 SDK，也不依赖 boto3 / minio / s3fs 等任何 S3 兼容客户端
方案。

> 设计目标：第一版只追求“简单、稳定、能跑通”。

## 功能

- 列出当前账号下的所有 bucket
- 浏览任意 `tos://bucket/prefix/` 下的对象与子目录
- 上传本地文件到指定 `tos://` 路径（先落盘到 `/data/uploads/` 再 `tosutil cp`）
- 下载 `tos://` 对象到容器内 `/data/downloads/`
- 删除 `tos://` 对象
- 通过上传空的 `.keep` 占位对象“创建目录”

API 一览：

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/api/buckets`                       | 列出所有 bucket |
| GET  | `/api/list?path=tos://bucket/dir/`   | 浏览目录 |
| POST | `/api/upload?path=tos://bucket/dir/` | 上传文件（multipart） |
| POST | `/api/download?path=tos://bucket/dir/file` | 下载到 `/data/downloads/` |
| POST | `/api/delete?path=tos://bucket/dir/file`   | 删除对象 |
| POST | `/api/mkdir?path=tos://bucket/dir/`        | 上传 `.keep` 占位 |

## 准备 tosutil

`tosutil` 不会被打进镜像。请自行准备：

1. 从火山引擎控制台下载与你 OS / 架构匹配的 `tosutil` 二进制（注意：构建 Linux 镜像时
   要用 **Linux 版** 的 `tosutil`）。
2. 把它放到项目根目录，文件名就叫 `tosutil`：

   ```bash
   cp /path/to/tosutil ./tosutil
   chmod +x ./tosutil
   ```

3. 在能用 `tosutil` 的机器上跑一次：

   ```bash
   ./tosutil config -i <AK> -k <SK> -e <Endpoint> -re <Region>
   ```

   完成后会在 `$HOME/.tosutilconfig` 生成凭证文件。**这个文件必须以挂载方式提供给容器，
   绝对不要打进镜像。**

## 本地开发（用 uv）

```bash
# 安装依赖（首次会自动生成 uv.lock）
uv sync

# 启动开发服务器（默认 http://localhost:8080）
uv run uvicorn app.main:app \
    --reload \
    --host 0.0.0.0 \
    --port 8080
```

本地跑时，确保 `tosutil` 在 `PATH` 里（或设环境变量 `TOSUTIL_BIN=/abs/path/to/tosutil`）。
同时确保 `~/.tosutilconfig` 已经配置好。

数据目录默认是 `/data`。本地开发可以指向别处：

```bash
export TOS_WEB_DATA_DIR="$PWD/.devdata"
```

## Docker

### 构建

```bash
docker build -t tos-web-gui .
```

### 运行

```bash
docker run --rm \
    -p 8080:8080 \
    -v ~/.tosutilconfig:/root/.tosutilconfig:ro \
    -v ~/tos-web-data:/data \
    tos-web-gui
```

说明：

- `-v ~/.tosutilconfig:/root/.tosutilconfig:ro`：以**只读**方式把宿主机的凭证挂进容器。
  这是唯一把凭证带进容器的方式。**凭证永远不会被打进镜像。**
- `-v ~/tos-web-data:/data`：上传缓冲区和下载落地目录持久化到宿主机。

然后浏览器访问：

> http://localhost:8080

## 安全说明

- 不使用 `shell=True`，所有 `tosutil` 调用都是参数数组形式（见 `app/tosutil.py`）。
- 所有用户传入的 `path` 都会经过 `validate_tos_path()`：必须以 `tos://` 开头，且不能包含
  `;`、`|`、`&`、`$`、反引号、换行、`<`、`>` 等明显危险字符。
- 上传文件名做了 `os.path.basename` + 去前缀，避免路径穿越。
- 服务端不打印 AK / SK / Token。`tosutil` 的 stderr 会原样返回给前端便于排错，请只在受信
  任的网络里暴露该服务。
- 镜像里不包含任何凭证；`.tosutilconfig` 通过 volume 挂载，且建议加 `:ro`。

## 常见问题

### `tosutil: command not found`
- **本地开发**：把 `tosutil` 加到 `PATH`，或者设置 `TOSUTIL_BIN=/abs/path/to/tosutil`。
- **Docker**：确认项目根目录有 `tosutil`，并且镜像是用同一目录构建的（`docker build .`）。
  注意必须是 **Linux 版**的 `tosutil`，不要把 macOS / Windows 版本拷进去。

### 起来了但所有请求都返回错误
- 99% 是 `~/.tosutilconfig` 没挂进容器。复查 `docker run` 是否带了
  `-v ~/.tosutilconfig:/root/.tosutilconfig:ro`。
- 也有可能是文件权限问题：容器默认以 root 运行，宿主机文件至少要 root 可读。

### 上传失败提示权限不足 / 目录不存在
- 检查目标 bucket 是否存在、当前 AK/SK 是否有写权限。
- 浏览一下父目录（`/api/list`），确认目标 prefix 合理。

### 当前凭证不支持 Cyberduck / rclone，但 tosutil 正常
- 这正是本项目存在的原因。火山云的某些凭证（例如带特殊策略的子账号 / STS Token）只能被
  官方 `tosutil` 正确识别，标准 S3 兼容客户端会失败。本项目通过把 `tosutil` 包成 HTTP API
  来绕开这个限制 —— 服务端只是一个 `tosutil` 的薄壳。

### `uv.lock` 是否要提交？
- 推荐提交，能保证镜像构建可复现。如果没提交，Dockerfile 会自动 fallback 到
  `uv sync --no-dev`，由 uv 在构建时即时解析依赖。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 入口与 API
│   ├── tosutil.py         # tosutil subprocess 封装
│   └── static/
│       └── index.html     # 最小化前端
├── pyproject.toml
├── Dockerfile
├── .dockerignore
├── .gitignore
└── README.md
```
