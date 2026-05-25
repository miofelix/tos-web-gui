"""
tosutil 子进程封装。

所有对 tosutil 的调用都统一走 run_tosutil()，禁止 shell=True，
统一捕获 stdout/stderr 并以结构化字典形式返回给上层。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

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
# ISO 8601 单 token，如 `2026-03-26T06:47:00Z` / `2026-03-26T06:47:00+0800`
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?$"
)

# size token：支持纯整数、带千分位逗号、带 B/KB/MB/GB/TB(+iB) 单位的小数。
_SIZE_TOKEN_RE = re.compile(
    r"^(\d+(?:,\d{3})*(?:\.\d+)?)\s*([KMGT]i?B?|B)?$",
    re.I,
)
_SIZE_UNIT_MULTIPLIERS: dict[str, int] = {
    "":   1, "B":   1,
    "K":  1024, "KB":  1024, "KIB":  1024,
    "M":  1024 ** 2, "MB":  1024 ** 2, "MIB":  1024 ** 2,
    "G":  1024 ** 3, "GB":  1024 ** 3, "GIB":  1024 ** 3,
    "T":  1024 ** 4, "TB":  1024 ** 4, "TIB":  1024 ** 4,
}


def _try_size(tok: str) -> int | None:
    """
    把一个 token 解释成字节数。失败返回 None。

    - 纯整数：`1024` -> 1024
    - 带千分位：`1,024` -> 1024
    - 带单位：`1.5MB` / `2 GiB` -> 字节数
    - 无单位的小数（如 `1.5`）认为不合理，返回 None，避免误吞奇怪的浮点
    """
    s = tok.strip()
    if not s:
        return None
    m = _SIZE_TOKEN_RE.match(s)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit_str = (m.group(2) or "").upper()
    if "." in num_str and not unit_str:
        return None
    try:
        num = float(num_str)
    except ValueError:
        return None
    mult = _SIZE_UNIT_MULTIPLIERS.get(unit_str)
    if mult is None:
        return None
    return int(num * mult)


def _merge_wrapped_lines(raw_lines: list[str]) -> list[str]:
    """
    tosutil 对很长的 key 会把它单独放一行，下一行才是 metadata（date/size/class/etag）。
    这里把这种「URL-only + metadata-only」的相邻两行合并成一行，便于后续单行解析。
    """
    out: list[str] = []
    i = 0
    n = len(raw_lines)
    while i < n:
        line = raw_lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        tokens = stripped.split()
        # 候选：单 token 的 file URL（目录以 / 结尾，不会换行）
        is_lone_file_url = (
            len(tokens) == 1
            and tokens[0].startswith("tos://")
            and not tokens[0].endswith("/")
            and i + 1 < n
        )
        if is_lone_file_url:
            nxt = raw_lines[i + 1].strip()
            nxt_tokens = nxt.split()
            # 下一行必须有内容、且不含另一个 URL，才认作 metadata-only 续行
            if nxt_tokens and not any(t.startswith("tos://") for t in nxt_tokens):
                out.append(tokens[0] + " " + nxt)
                i += 2
                continue
        out.append(line)
        i += 1
    return out


def parse_listing(text: str, base_path: str) -> list[dict[str, Any]]:
    """
    解析 `tosutil ls -d <base>` 的输出。

    输出格式在不同 tosutil 版本里有差异，这里兼容两种主流形态：

    形态 A（URL 在行首，ISO 时间，带单位 size）：
        tos://bkt/dir/file.zip   2026-03-26T06:47:00Z   32.03GB  STANDARD  "<etag>"
        tos://bkt/dir/sub/

        Folder list:   /  Object list:   /  Folder number is: N   等节标题/统计行
        会因为不含 tos:// 自然被忽略。

        长 key 会被 tosutil 换行成两行（URL 一行 + metadata 一行），
        _merge_wrapped_lines 会把它们合并回单行后再解析。

    形态 B（URL 在行尾，空格分隔的日期，纯字节数 size）：
        2024-01-15 10:30:45 +0800   1024   <etag>   STANDARD   tos://bkt/dir/file.txt

    解析策略：
    - URL token 可以在行内任意位置；其余所有 token 不区分前后，统一进入 metadata 扫描
    - mtime 优先 ISO 8601 单 token，否则尝试 YYYY-MM-DD + HH:MM:SS [±TZ] 三 token
    - size 优先取 mtime 之后紧邻的 token；取不到再扫所有 metadata token 找第一个能当 size 的
    - URL 必须以 base 开头、本层名不含 `/`，否则跳过
    """
    base = base_path if base_path.endswith("/") else base_path + "/"

    merged_lines = _merge_wrapped_lines(text.splitlines())

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw_line in merged_lines:
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

        if not is_dir:
            other_tokens = tokens[:url_idx] + tokens[url_idx + 1:]

            # 1) 锚定时间：先试 ISO 8601 单 token，再试 date+time(+tz) 三段
            date_end = -1
            for j, tok in enumerate(other_tokens):
                if _ISO_DATETIME_RE.match(tok):
                    mtime = tok
                    date_end = j
                    break
                if (
                    _DATE_RE.match(tok)
                    and j + 1 < len(other_tokens)
                    and _TIME_RE.match(other_tokens[j + 1])
                ):
                    mtime = f"{tok} {other_tokens[j + 1]}"
                    date_end = j + 1
                    if (
                        j + 2 < len(other_tokens)
                        and _TZ_RE.match(other_tokens[j + 2])
                    ):
                        mtime = f"{mtime} {other_tokens[j + 2]}"
                        date_end = j + 2
                    break

            # 2) 优先把 date 之后紧跟的第一个 token 当 size
            if date_end >= 0 and date_end + 1 < len(other_tokens):
                size = _try_size(other_tokens[date_end + 1])

            # 3) 兜底：扫整段 metadata 找首个能解析为 size 的 token
            if size is None:
                for tok in other_tokens:
                    sz = _try_size(tok)
                    if sz is not None:
                        size = sz
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


# ---------------------------------------------------------------------------
# 流式调用 + 进度解析（给 task 模块用）
# ---------------------------------------------------------------------------

# 不同 tosutil 版本输出格式略有差异，下面这些正则都做 best-effort，
# 匹配不到就让任务进度退化为 indeterminate。
PROGRESS_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*%")
PROGRESS_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB|B)/s)", re.I)
PROGRESS_BYTES_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*(?:B|bytes)?", re.I)


async def stream_tosutil(
    args: Sequence[str],
    on_line: Callable[[str], None],
    *,
    on_process: Callable[["asyncio.subprocess.Process"], None] | None = None,
) -> int:
    """
    异步启动 tosutil 子进程，把 stdout+stderr 合并后逐「帧」回调给 on_line。

    「帧」= 以 \n 或 \r 任一结尾的一段输出。tosutil 在交互式终端会用 \r
    覆盖同一行做进度刷新（典型的「succeed/failed/total 一行」），用 \n 分隔
    完整事件。两者都拆出来给 on_line，前端就能拿到接近终端的实时画面。

    - 永远 shell=False、参数数组
    - 返回 returncode（非 0 由调用方决定怎么报错）
    - 若调用方需要持有 process 句柄做 cancel，可传 on_process 回调
    - 被 asyncio.CancelledError 中断时会 kill 子进程再向上抛
    """
    proc = await asyncio.create_subprocess_exec(
        TOSUTIL_BIN,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if on_process is not None:
        on_process(proc)
    try:
        assert proc.stdout is not None
        buf = bytearray()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            # 任意 \r 或 \n 都视为帧边界；CRLF 中间的空帧会被 strip 后丢掉
            while True:
                nl = buf.find(b"\n")
                cr = buf.find(b"\r")
                if nl < 0 and cr < 0:
                    break
                if nl < 0:
                    idx = cr
                elif cr < 0:
                    idx = nl
                else:
                    idx = min(nl, cr)
                frame_bytes = bytes(buf[:idx])
                del buf[: idx + 1]
                frame = frame_bytes.decode("utf-8", errors="replace").rstrip()
                if frame:
                    try:
                        on_line(frame)
                    except Exception:
                        # 进度回调出错不能掀翻子进程读取循环
                        pass
        # flush 尾部（如果最后一帧没有终止符）
        if buf:
            tail = bytes(buf).decode("utf-8", errors="replace").rstrip()
            if tail:
                try:
                    on_line(tail)
                except Exception:
                    pass
        return await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise


# 兼容多种 tosutil 版本的 du 汇总输出。关键样本：
#   "Total object number:            240989              Total object size:              396.39GB"
#   "Total Size: 1234"               （老版本简化）
#   "Total Bytes: 1234"
#
# 重点：捕获的值是字符串（可能带 KB/MB/GB/TB 单位），统一交给 _try_size() 换算。
# 「object」/「storage」/「total」前缀都允许；但「size」「bytes」前必须有 `:`，避免
# 把列表头 "Object size" 这种没分隔符的列名误匹配。
_DU_TOTAL_RE = re.compile(
    r"(?:object\s+|storage\s+|total\s+)*(?:size|bytes)[:：]\s*([0-9][\w.,]*)",
    re.I,
)
_DU_OBJECTS_RE = re.compile(
    r"(?:object\s+(?:number|count)|total\s+objects?|对象\s*数|总数)\s*(?:is)?\s*[:：]\s*(\d+)",
    re.I,
)


def parse_du(text: str) -> dict[str, int | None] | None:
    """
    解析 `tosutil du` 输出，返回 {"bytes": int, "objects": int | None}。

    策略：
    1. `_DU_TOTAL_RE` 抓 'Total ... size: 396.39GB' 的值（带单位），过 `_try_size` 换算字节
    2. `_DU_OBJECTS_RE` 抓 'Total object number: 240989' 的对象数
    3. 都用最后一次匹配（典型 du 输出在末尾汇总，避免被中间统计行覆盖）
    4. 兜底：扫整段文本找所有能解释为 size 的 token，取最大值。仅在 du 输出极简
       （只有 storage-class 表格、没有 Total 汇总行）时才会触发

    解析失败返回 None。
    """
    bytes_total: int | None = None
    objects: int | None = None

    for m in _DU_TOTAL_RE.finditer(text):
        candidate = _try_size(m.group(1))
        if candidate is not None:
            bytes_total = candidate  # 用最后一次匹配，对应底部汇总行

    for m in _DU_OBJECTS_RE.finditer(text):
        try:
            objects = int(m.group(1))
        except ValueError:
            pass

    if bytes_total is None:
        candidates: list[int] = []
        for tok in text.split():
            sz = _try_size(tok.strip(",.;:"))
            if sz is not None:
                candidates.append(sz)
        if candidates:
            bytes_total = max(candidates)

    if bytes_total is None:
        return None
    return {"bytes": bytes_total, "objects": objects}


# ---------------------------------------------------------------------------
# 原有同步 API
# ---------------------------------------------------------------------------


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
    "stream_tosutil",
    "list_buckets",
    "list_path",
    "list_dir",
    "parse_listing",
    "parse_bucket_list",
    "parse_du",
    "PROGRESS_PERCENT_RE",
    "PROGRESS_SPEED_RE",
    "PROGRESS_BYTES_RE",
    "upload_file",
    "download_file",
    "delete_path",
    "mkdir",
    "_safe_basename",
]
