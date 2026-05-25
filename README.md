# TOS Web GUI

浏览器里的火山云 TOS 对象存储管理工具。

后端用 **FastAPI**，前端是最小化的原生 HTML/CSS/JS，所有 TOS 操作都通过 **subprocess** 调
用官方 `tosutil` 完成 —— 不使用 S3 SDK，也不依赖 boto3 / minio / s3fs 等任何 S3 兼容客户端
方案。

> 设计目标：在浏览器里拿到接近 macOS Finder 的对象存储体验。

## 功能

- **文件浏览器**：bucket 列表 → 进 bucket → 进子目录，行级 size / 修改时间
- **导航**：可点击面包屑、`◀ ▶` 后退/前进栈、`⬆` 上级、`⟳` 刷新；快捷键 `Alt+←` / `Alt+→`
- **目录大小一键计算**：每个目录行的 `📐` 按钮触发后台 `tosutil du`，结果回填到 size 单元格
- **上传 / 下载实时进度**：右下浮层任务面板。上传分两段展示「浏览器 → 服务器（XHR 真实进度）」和「服务器 → TOS（解析 tosutil 输出）」
- **下载触发原生下载弹窗**：服务端把文件缓存在 `/data/downloads/`，再由浏览器原生下载
- **删除二次确认**：模态需要键入完整对象名/目录名才能点亮「永久删除」
- **创建目录**：上传空的 `.keep` 占位对象

API 一览：

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET    | `/api/buckets`                       | 列出所有 bucket（原始） |
| GET    | `/api/list?path=`                    | 原始 `tosutil ls` 输出，curl 调试用 |
| GET    | `/api/browse?path=`                  | 结构化浏览（空路径返回 bucket 列表） |
| POST   | `/api/upload?path=`                  | multipart 上传，返回 `{task_id}` |
| POST   | `/api/download/start?path=`          | 启动后台下载任务，返回 `{task_id}` |
| GET    | `/api/download/{task_id}/file`       | 任务完成后流回文件（Content-Disposition: attachment） |
| POST   | `/api/dirsize?path=`                 | 启动后台 `tosutil du` 任务，返回 `{task_id}` |
| POST   | `/api/delete?path=`                  | 删除对象（同步） |
| POST   | `/api/mkdir?path=`                   | 上传 `.keep` 占位（同步） |
| GET    | `/api/tasks`                         | 列出所有任务（含已完成的） |
| GET    | `/api/task/{task_id}`                | 单任务状态 |
| DELETE | `/api/task/{task_id}`                | 运行中则 cancel、终态则 dismiss |

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
- 浏览一下父目录（`/api/browse?path=...`），确认目标 prefix 合理。

### 进度条一直是「indeterminate（条纹滚动）」
- 说明后端解析不到你这个 `tosutil` 版本的进度行。功能不受影响（任务仍在跑），UI 只是退化展示。
- 把 `GET /api/task/{id}` 里的 `message` 字段贴过来，看一眼真实输出格式，可以调
  `app/tosutil.py` 顶部的 `PROGRESS_PERCENT_RE` / `PROGRESS_SPEED_RE` / `PROGRESS_BYTES_RE`。

### 目录大小一直转圈 / 报「无法解析 du 输出」
- 同上，可能是 `tosutil du` 的「Total Size」行格式不一样。把任务里的 `message` / 错误贴过
  来，调 `app/tosutil.py:_DU_TOTAL_RE` / `_DU_OBJECTS_RE` 即可。

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
│   ├── main.py            # FastAPI 入口与 API（同步 + 后台任务）
│   ├── tosutil.py         # tosutil subprocess 封装 + 流式调用 + 解析
│   ├── tasks.py           # 进程内任务登记表（upload / download / dirsize）
│   └── static/
│       └── index.html     # 文件浏览器前端（含右下任务面板、删除模态）
├── pyproject.toml
├── Dockerfile
├── .dockerignore
├── .gitignore
└── README.md
```
