"""
tosutil 子进程封装。

所有对 tosutil 的调用都统一走 run_tosutil()，禁止 shell=True，
统一捕获 stdout/stderr 并以结构化字典形式返回给上层。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

# tosutil 在镜像内位于 /usr/local/bin/tosutil；本地开发时若已加入 PATH 也能直接调用。
TOSUTIL_BIN = os.environ.get("TOSUTIL_BIN", "tosutil")

# 单次 tosutil 调用的默认超时（秒），覆盖大多数小文件场景。
# 大文件上传/下载可以通过环境变量调大。
DEFAULT_TIMEOUT = int(os.environ.get("TOSUTIL_TIMEOUT", "1800"))

# path 校验里禁止的明显危险字符。即使我们用 shell=False，
# 这里也做一层防御，避免出现奇怪的换行/注入式输入。
_DANGEROUS_CHARS = set(";|&$`\n\r<>\x00")


class TosutilError(RuntimeError):
    """tosutil 调用失败时抛出，携带 returncode 与 stderr。"""

    def __init__(self, returncode: int, stderr: str, stdout: str = "") -> None:
        super().__init__(stderr.strip() or f"tosutil exited with code {returncode}")
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def validate_tos_path(path: str, *, allow_empty: bool = False) -> str:
    """校验 tos:// 路径。返回原始 path 以便链式使用。"""
    if not path:
        if allow_empty:
            return ""
        raise ValueError("path is required")
    if not path.startswith("tos://"):
        raise ValueError("path must start with tos://")
    if len(path) <= len("tos://"):
        raise ValueError("path must include a bucket name")
    for ch in path:
        if ch in _DANGEROUS_CHARS:
            raise ValueError("path contains invalid character")
    return path


def _safe_basename(name: str) -> str:
    """从用户提供的文件名里去掉目录成分，避免路径穿越。"""
    base = os.path.basename(name or "")
    base = base.lstrip("./\\")
    if not base or base in (".", ".."):
        raise ValueError("invalid filename")
    return base


def run_tosutil(args: Sequence[str], *, timeout: int | None = None) -> dict:
    """
    统一的 tosutil 调用入口。

    - 永远不使用 shell=True
    - 始终以参数数组形式传入
    - 捕获 stdout / stderr
    - 失败时抛 TosutilError
    """
    cmd = [TOSUTIL_BIN, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TosutilError(127, f"tosutil binary not found: {TOSUTIL_BIN}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TosutilError(124, f"tosutil timed out after {exc.timeout}s") from exc

    if proc.returncode != 0:
        raise TosutilError(proc.returncode, proc.stderr or "", proc.stdout or "")

    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def list_buckets() -> dict:
    """列出当前账号下的所有 bucket。"""
    return run_tosutil(["ls"])


def list_path(path: str) -> dict:
    """列出某个 tos:// 路径下的对象与子目录（原始递归输出，用于 raw 视图）。"""
    validate_tos_path(path)
    return run_tosutil(["ls", path])


def list_dir(path: str) -> dict:
    """非递归列出当前一层 (`tosutil ls -d`)，给文件浏览器用。"""
    validate_tos_path(path)
    normalized = path if path.endswith("/") else path + "/"
    return run_tosutil(["ls", "-d", normalized])


# ---------------------------------------------------------------------------
# 输出解析：把 tosutil ls 的纯文本拆成结构化条目
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_TZ_RE = re.compile(r"^[+-]\d{4}$")


def parse_listing(text: str, base_path: str) -> list[dict[str, Any]]:
    """
    解析 `tosutil ls -d <base>` 的输出。

    每行的格式形如：
        2024-01-15 10:30:45 +0800   1024   <etag>   STANDARD   tos://bucket/dir/file.txt
        <空白...>                                                tos://bucket/dir/subdir/

    解析策略：
    - 找到行内首个以 `tos://` 开头的 token，作为 URL
    - URL 必须以 base_path 开头，且只看本层（不含中间斜杠的相对名）
    - URL 以 `/` 结尾视为子目录；否则视为文件
    - 文件尝试从 URL 前的 token 里抽取「size」（首个纯数字）和「mtime」(YYYY-MM-DD HH:MM:SS [±TZ])
    - 含有 `tos://` 之外内容的总计/统计行会被自动忽略
    """
    base = base_path if base_path.endswith("/") else base_path + "/"
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        tokens = raw_line.split()

        url = None
        url_idx = -1
        for i, tok in enumerate(tokens):
            if tok.startswith("tos://"):
                url = tok
                url_idx = i
                break
        if url is None:
            continue

        # 跳过列出当前目录自身的占位行
        if url == base or url.rstrip("/") == base.rstrip("/"):
            continue
        if not url.startswith(base):
            continue

        rel = url[len(base):]
        if not rel:
            continue

        is_dir = url.endswith("/")
        name = rel.rstrip("/")

        # 本层只接受没有更深层级的条目，避免 -d 偶发返回更深路径时混进来
        if "/" in name:
            continue
        if name in seen:
            continue
        seen.add(name)

        size: int | None = None
        mtime: str | None = None
        if not is_dir and url_idx > 0:
            prefix = tokens[:url_idx]
            for tok in prefix:
                if tok.isdigit():
                    size = int(tok)
                    break
            for i, tok in enumerate(prefix):
                if (
                    _DATE_RE.match(tok)
                    and i + 1 < len(prefix)
                    and _TIME_RE.match(prefix[i + 1])
                ):
                    mtime = f"{tok} {prefix[i + 1]}"
                    if i + 2 < len(prefix) and _TZ_RE.match(prefix[i + 2]):
                        mtime = f"{mtime} {prefix[i + 2]}"
                    break

        entries.append({
            "name": name,
            "path": url,
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime,
        })

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def parse_bucket_list(text: str) -> list[dict[str, Any]]:
    """解析 `tosutil ls`（不带路径）输出，得到 bucket 列表。"""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for tok in line.split():
            if tok.startswith("tos://"):
                bucket = tok[len("tos://"):].strip("/")
                if bucket and "/" not in bucket and bucket not in seen:
                    seen.add(bucket)
                    entries.append({
                        "name": bucket,
                        "path": f"tos://{bucket}/",
                        "is_dir": True,
                        "size": None,
                        "mtime": None,
                    })
                break
    entries.sort(key=lambda e: e["name"].lower())
    return entries


def upload_file(local: str, remote: str) -> dict:
    """把本地文件上传到 tos:// 远端路径。"""
    validate_tos_path(remote)
    local_path = Path(local)
    if not local_path.is_file():
        raise ValueError(f"local file not found: {local}")
    return run_tosutil(["cp", str(local_path), remote])


def download_file(remote: str, local: str) -> dict:
    """把 tos:// 远端对象下载到本地路径。"""
    validate_tos_path(remote)
    local_path = Path(local)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return run_tosutil(["cp", remote, str(local_path)])


def delete_path(path: str) -> dict:
    """删除某个对象。tosutil rm 对目录需要额外参数，这里只覆盖单对象语义。"""
    validate_tos_path(path)
    return run_tosutil(["rm", path])


def mkdir(path: str) -> dict:
    """
    TOS 没有真正的目录，这里通过上传一个空的 .keep 对象来“创建目录”。
    """
    validate_tos_path(path)
    folder = path if path.endswith("/") else path + "/"
    keep_remote = folder + ".keep"

    # 用临时空文件做占位；用完即删，避免污染 /tmp。
    tmp_dir = tempfile.mkdtemp(prefix="tos-mkdir-")
    keep_local = Path(tmp_dir) / ".keep"
    try:
        keep_local.touch()
        return run_tosutil(["cp", str(keep_local), keep_remote])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


__all__ = [
    "TosutilError",
    "validate_tos_path",
    "run_tosutil",
    "list_buckets",
    "list_path",
    "list_dir",
    "parse_listing",
    "parse_bucket_list",
    "upload_file",
    "download_file",
    "delete_path",
    "mkdir",
    "_safe_basename",
]
