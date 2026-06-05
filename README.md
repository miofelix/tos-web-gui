# TOS Web GUI

> Browser-based GUI for Volcengine TOS. Wraps the official `tosutil` binary over HTTP — built for credentials that no S3-compatible client (Cyberduck, rclone, boto3) can use.

浏览器里的火山云 TOS 对象存储管理工具。

## 为什么有这个项目

**当前手上的火山云凭证不被 Cyberduck / rclone / boto3 / minio 等任何 S3 兼容客户端识别，
只有官方 `tosutil` 能正常用。** 这通常出现在带特殊策略的子账号或 STS Token 场景下。

本项目就是为了绕开这个限制：把 `tosutil` 包成 HTTP API + 一个简洁的网页前端，让没法用标准 S3
客户端的同事也能在浏览器里完成浏览和目录大小查看。

后端 **FastAPI**，前端是原生 HTML/CSS/JS，所有 TOS 操作都通过 **subprocess** 调用
`tosutil` 完成 —— **不引入任何 S3 SDK**。

## 功能

- **文件浏览器**：bucket → 子目录，行级 size / 修改时间
- **导航**：面包屑、后退/前进栈、上级、刷新；快捷键 `Alt+←` / `Alt+→`
- **路径复制**：文件和文件夹行支持一键复制完整 `tos://...` 路径
- **目录大小**：目录行的 Size 列显示「计算」链接，点击后后台跑 `tosutil du` 并把结果回填到该列
- **任务面板**：右下任务面板展示后台目录大小任务的状态、日志和结果

## 快速开始

### 1. 准备 `tosutil` 和凭证

`tosutil` 是火山引擎的商业二进制，**本项目镜像里不包含它**，需要你自己从官方渠道下载并提供
给容器。

```bash
# 1) 从火山控制台下载与目标容器架构匹配的 Linux 版二进制
#    (linux/amd64 → tosutil-Linux-amd64bit；linux/arm64 → tosutil-Linux-arm64bit)
cp /path/to/tosutil-linux ~/bin/tosutil
chmod +x ~/bin/tosutil

# 2) 在能跑 tosutil 的机器上配一次凭证，生成 ~/.tosutilconfig
~/bin/tosutil config -i <AK> -k <SK> -e <Endpoint> -re <Region>
```

> 二进制和 `~/.tosutilconfig` 都只能通过 volume 挂进容器，**绝不要打进镜像**。

### 2. Docker 运行（推荐）

直接拉已经构建好的镜像：

```bash
docker run -d \
  --name tos-web-gui \
  --restart unless-stopped \
  -p 41880:8080 \
  -v "/path/to/tosutil-Linux-<arch>bit:/usr/local/bin/tosutil:ro" \
  -v "$HOME/.tosutilconfig:/root/.tosutilconfig:ro" \
  miofelix/tos-web-gui
```

两个挂载分别是：**tosutil 二进制 / 凭证文件**，缺一不可。第一个挂载的左侧
换成你本机实际的 Linux 版 `tosutil` 路径，`<arch>` 按容器架构填 `amd64` 或 `arm64`。

常用运维操作：

```bash
docker logs -f tos-web-gui     # 跟随日志
docker stop tos-web-gui        # 停止
docker rm   tos-web-gui        # 删除容器（镜像保留）
```

或者本地自己构建：

```bash
docker build -t tos-web-gui .
docker run -d \
  --name tos-web-gui \
  --restart unless-stopped \
  -p 41880:8080 \
  -v "/path/to/tosutil-Linux-<arch>bit:/usr/local/bin/tosutil:ro" \
  -v "$HOME/.tosutilconfig:/root/.tosutilconfig:ro" \
  tos-web-gui
```

打开 http://localhost:41880 即可。

### 3. 本地开发

```bash
uv sync
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 41880
```

本地跑要确保 `tosutil` 在 `PATH` 里（或设 `TOSUTIL_BIN=/abs/path/to/tosutil`），
且 `~/.tosutilconfig` 已配置。

## API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET    | `/api/buckets`                 | 列出所有 bucket |
| GET    | `/api/browse?path=`            | 结构化浏览（空路径返回 bucket 列表） |
| GET    | `/api/list?path=`              | 原始 `tosutil ls` 输出，调试用 |
| POST   | `/api/dirsize?path=`           | 启动后台 `tosutil du`，返回 `{task_id}` |
| GET    | `/api/tasks`                   | 列出所有任务 |
| GET    | `/api/task/{task_id}`          | 单任务状态 |
| DELETE | `/api/task/{task_id}`          | 运行中则 cancel、终态则 dismiss |

## 安全说明

- 不用 `shell=True`，所有 `tosutil` 调用都是参数数组形式（见 `app/tosutil.py`）。
- 用户传入的 `path` 经 `validate_tos_path()`：必须以 `tos://` 开头，禁止 `;`、`|`、`&`、
  `$`、反引号、换行、`<`、`>` 等危险字符。
- 服务端不打印 AK / SK / Token；`tosutil` 的 stderr 会原样返回给前端便于排错 ——
  **请只在受信任的网络里暴露该服务**。
- 镜像不含任何凭证；`.tosutilconfig` 通过 volume 挂载，建议加 `:ro`。

## 常见问题

**`tosutil: command not found` / `tosutil binary not found`**
- 本地：把 `tosutil` 加进 `PATH`，或设 `TOSUTIL_BIN=/abs/path/to/tosutil`。
- Docker：确认 `-v /path/to/tosutil:/usr/local/bin/tosutil:ro` 这个挂载有带上，且宿主机
  二进制是 **与容器架构匹配的 Linux 版**（容器是 linux/arm64 就要 arm64 版，amd64 同理）。

**容器里 `tosutil` 跑起来就报 `exec format error`**
- 二进制架构和容器架构不匹配。检查 `docker image inspect miofelix/tos-web-gui | grep Arch`
  和你 `tosutil` 二进制的 `file` 输出。

**起来了但所有请求都报错**
- 检查 `~/.tosutilconfig` 是否挂进容器（`-v ~/.tosutilconfig:/root/.tosutilconfig:ro`）。
- 也可能是文件权限（容器默认 root，宿主机文件要 root 可读）。

**进度条一直是条纹滚动 / 「无法解析 du 输出」**
- 后端解析不到你这个 `tosutil` 版本的输出格式。功能不受影响，UI 退化展示。
- 把 `GET /api/task/{id}` 的 `message` 贴过来，调 `app/tosutil.py` 里的 `_DU_*_RE`
  正则即可。

**`uv.lock` 是否要提交？**
- 推荐提交，构建可复现。没提交时 Dockerfile 会 fallback 到 `uv sync --no-dev`。

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口与 API
│   ├── tosutil.py         # tosutil subprocess 封装 + 流式解析
│   ├── tasks.py           # 进程内任务登记表
│   └── static/
│       └── index.html     # 前端（文件浏览器 + 任务面板）
├── pyproject.toml
├── Dockerfile
├── LICENSE
└── README.md
```

## License

本项目代码采用 [MIT License](./LICENSE) 开源，版权所有 © 2026 Yu Fan。

**免责声明**：本项目与火山引擎无任何官方关系。`tosutil` 是火山引擎的商业软件，其下载、
安装和使用须遵守火山引擎相应的服务条款与许可协议；本项目仅通过 subprocess 调用用户自行
准备的 `tosutil` 二进制，**不分发、不修改、不再授权该二进制**。
