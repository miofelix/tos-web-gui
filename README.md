# TOS Web GUI

> Browser-based GUI for Volcengine TOS. Wraps the official `tosutil` binary over HTTP — built for credentials that no S3-compatible client (Cyberduck, rclone, boto3) can use.

浏览器里的火山云 TOS 对象存储管理工具。

## 适用场景

当火山云 TOS 凭证无法被 Cyberduck / rclone / boto3 / minio 等 S3 兼容客户端识别，
但可以通过官方 `tosutil` 正常访问时，可以用这个项目提供一个浏览器界面。

应用会把 `tosutil` 封装成 HTTP API + 网页前端，方便在浏览器里完成 TOS 浏览和目录大小查看。

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

本项目镜像不包含 `tosutil`。请从火山引擎官方渠道获取与容器架构匹配的 Linux 版
`tosutil`，并在能运行 `tosutil` 的环境中生成凭证配置。

```bash
# 1) 从火山控制台下载与目标容器架构匹配的 Linux 版二进制
#    (linux/amd64 → tosutil-Linux-amd64bit；linux/arm64 → tosutil-Linux-arm64bit)
cp /path/to/tosutil-linux ~/bin/tosutil
chmod +x ~/bin/tosutil

# 2) 在能跑 tosutil 的机器上配一次凭证，生成 ~/.tosutilconfig
~/bin/tosutil config -i <AK> -k <SK> -e <Endpoint> -re <Region>
```

> `tosutil` 二进制和 `~/.tosutilconfig` 都通过 volume 挂进容器；请不要把它们写入镜像或提交到仓库。

### 2. Docker Compose 运行（推荐）

仓库内提供了 `compose.yaml`，统一通过 Docker Compose 在本地构建并启动：

```bash
cp .env.example .env
$EDITOR .env
docker compose up -d --build
```

`.env` 里需要填：

```dotenv
TOSUTIL_HOST_BIN=/absolute/path/to/tosutil-Linux-amd64bit
TOSUTIL_CONFIG_HOST_PATH=/absolute/path/to/.tosutilconfig
TOS_WEB_PORT=41880
```

- `TOSUTIL_HOST_BIN`：宿主机上的 Linux 版 `tosutil` 绝对路径，架构需与容器一致。
- `TOSUTIL_CONFIG_HOST_PATH`：宿主机上的 `tosutil` 凭证配置文件绝对路径。
- `TOS_WEB_PORT`：宿主机暴露端口，默认 `41880`。

常用命令：

```bash
docker compose logs -f tos-web-gui
docker compose restart tos-web-gui
docker compose down
```

打开 http://localhost:41880 即可；如果改了 `TOS_WEB_PORT`，端口也要相应替换。

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
- Docker Compose：确认 `.env` 里的 `TOSUTIL_HOST_BIN` 指向宿主机上的 Linux 版
  `tosutil`，且二进制架构与容器一致（容器是 linux/arm64 就要 arm64 版，amd64 同理）。

**容器里 `tosutil` 跑起来就报 `exec format error`**
- 二进制架构和容器架构不匹配。检查当前 Docker 平台和你 `tosutil` 二进制的 `file`
  输出是否一致。

**起来了但所有请求都报错**
- 检查 `.env` 里的 `TOSUTIL_CONFIG_HOST_PATH` 是否指向正确的 `.tosutilconfig`。
- 也可能是文件权限（容器默认 root，宿主机文件要 root 可读）。

**进度条一直是条纹滚动 / 「无法解析 du 输出」**
- 后端解析不到你这个 `tosutil` 版本的输出格式。功能不受影响，UI 退化展示。
- 把 `GET /api/task/{id}` 的 `message` 贴过来，调 `app/tosutil.py` 里的 `_DU_*_RE`
  正则即可。

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口与 API
│   ├── tosutil.py         # tosutil subprocess 封装 + 流式解析
│   ├── tasks.py           # 进程内任务登记表
│   └── static/
│       └── index.html     # 前端（文件浏览器 + 任务面板）
├── compose.yaml           # 本地 Docker Compose 启动配置
├── .env.example           # Docker Compose 环境变量示例
├── pyproject.toml
├── Dockerfile
├── LICENSE
└── README.md
```

## License

本项目代码采用 [MIT License](./LICENSE) 开源，版权所有 © 2026 Yu Fan。

**免责声明**：本项目与火山引擎无官方关联，不提供 `tosutil` 二进制。请从火山引擎官方渠道
获取 `tosutil`，并遵守其下载、安装和使用条款。本项目仅通过 subprocess 调用你挂载到容器中的
`tosutil`。
