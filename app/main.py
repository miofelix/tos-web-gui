"""
FastAPI 入口。

页面与静态：
- /                 -> 返回 static/index.html
- /static/*         -> 静态资源

只读 / 同步类 API：
- GET    /api/buckets                -> 列出所有 bucket（原始）
- GET    /api/list?path=             -> 原始 tosutil ls 输出（raw 调试用）
- GET    /api/browse?path=           -> 结构化浏览（给 UI 用）

变更类 API：
- POST   /api/upload?path=           -> 多段上传：先落 /data/uploads，再后台 tosutil cp，返回 {task_id}
- POST   /api/download/start?path=   -> 后台 tosutil cp 到 /data/downloads，返回 {task_id}
- GET    /api/download/{task_id}/file-> 任务完成后 FileResponse 流回浏览器
- POST   /api/dirsize?path=          -> 后台 tosutil du，返回 {task_id}
- POST   /api/delete?path=           -> 同步删除
- POST   /api/mkdir?path=            -> 同步创建占位 .keep

任务总线：
- GET    /api/tasks                  -> 全部任务
- GET    /api/task/{task_id}         -> 单任务状态
- DELETE /api/task/{task_id}         -> 运行中则 cancel；终态则 dismiss
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import tasks, tosutil
from app.tasks import TaskKind, TaskState
from app.tosutil import TosutilError

# 数据目录：上传缓冲区 + 下载落地目录。
# 在容器里通过 `-v ~/tos-web-data:/data` 持久化。
DATA_DIR = Path(os.environ.get("TOS_WEB_DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DOWNLOAD_DIR = DATA_DIR / "downloads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TOS Web GUI", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# 公共响应工具
# ---------------------------------------------------------------------------

def _ok(payload: dict[str, Any], **extra: Any) -> JSONResponse:
    body = {"success": True, **payload, **extra}
    return JSONResponse(body)


def _tosutil_error_response(exc: TosutilError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "success": False,
            "error": str(exc),
            "returncode": exc.returncode,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        },
    )


def _handle_tosutil(callable_, *args: Any, **kwargs: Any) -> JSONResponse:
    try:
        result = callable_(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TosutilError as exc:
        return _tosutil_error_response(exc)
    return _ok(result)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, bool]:
    return {"ok": True}


# ---------------------------------------------------------------------------
# 只读 / 同步类 API
# ---------------------------------------------------------------------------

@app.get("/api/buckets")
def api_list_buckets() -> JSONResponse:
    return _handle_tosutil(tosutil.list_buckets)


@app.get("/api/list")
def api_list(path: str = Query(..., description="tos://bucket/path/")) -> JSONResponse:
    """原始递归输出，保留给 raw 视图 / curl 调试。"""
    return _handle_tosutil(tosutil.list_path, path)


@app.get("/api/browse")
def api_browse(
    path: str = Query("", description="tos://bucket/path/ 或留空列出 bucket"),
) -> JSONResponse:
    try:
        if not path or path.strip() == "tos://":
            result = tosutil.list_buckets()
            entries = tosutil.parse_bucket_list(result["stdout"])
            return _ok({
                "path": "",
                "is_root": True,
                "entries": entries,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            })

        tosutil.validate_tos_path(path)
        normalized = path if path.endswith("/") else path + "/"
        result = tosutil.list_dir(normalized)
        entries = tosutil.parse_listing(result["stdout"], normalized)
        return _ok({
            "path": normalized,
            "is_root": False,
            "entries": entries,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TosutilError as exc:
        return _tosutil_error_response(exc)


@app.post("/api/delete")
def api_delete(path: str = Query(..., description="tos:// 对象路径")) -> JSONResponse:
    return _handle_tosutil(tosutil.delete_path, path)


@app.post("/api/mkdir")
def api_mkdir(path: str = Query(..., description="要创建的 tos:// 目录")) -> JSONResponse:
    return _handle_tosutil(tosutil.mkdir, path)


# ---------------------------------------------------------------------------
# 后台任务的通用进度回调
# ---------------------------------------------------------------------------

def _apply_progress_line(task_id: str, line: str) -> None:
    """从 tosutil 一行输出里抽 %、速度、bytes，写回 task，并入终端日志。"""
    t = tasks.get(task_id)
    if t is None:
        return
    tasks.append_log(task_id, line)
    t.message = line[:240]

    m = tosutil.PROGRESS_PERCENT_RE.search(line)
    if m:
        try:
            pct = int(m.group(1))
            if 0 <= pct <= 100:
                t.progress = pct / 100.0
        except ValueError:
            pass

    m_speed = tosutil.PROGRESS_SPEED_RE.search(line)
    if m_speed:
        t.speed = m_speed.group(1).strip()

    m_bytes = tosutil.PROGRESS_BYTES_RE.search(line)
    if m_bytes:
        try:
            done = int(m_bytes.group(1))
            total = int(m_bytes.group(2))
            t.bytes_done = done
            t.bytes_total = total
            if total > 0:
                t.progress = max(t.progress or 0.0, done / total)
        except ValueError:
            pass


def _bind_process(task_id: str):
    """返回一个回调，把 process 句柄存到 task 上，便于 cancel。"""
    def _on(proc):
        t = tasks.get(task_id)
        if t is not None:
            t._process = proc
    return _on


def _is_cancelled(task_id: str) -> bool:
    t = tasks.get(task_id)
    return t is not None and t.state == TaskState.CANCELLED.value


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def api_upload(
    path: str = Query(..., description="目标 tos:// 目录"),
    file: UploadFile = File(...),
) -> JSONResponse:
    """
    多段上传。

    1. 请求体由 FastAPI 完整接收（浏览器侧用 XHR 的 upload.onprogress 看进度）
    2. 落到 /data/uploads/<safe_name>
    3. 注册一个 upload Task，后台跑 `tosutil cp local remote`，返回 task_id
    """
    try:
        tosutil.validate_tos_path(path)
        filename = tosutil._safe_basename(file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    folder = path if path.endswith("/") else path + "/"
    remote = folder + filename
    local_path = UPLOAD_DIR / filename

    # 把请求体落盘交给一个线程，避免阻塞事件循环（大文件可能写几秒）
    def _persist() -> None:
        with local_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)

    try:
        await asyncio.to_thread(_persist)
    finally:
        await file.close()

    task = tasks.create(TaskKind.UPLOAD, remote, name=filename)
    task.local_path = str(local_path)
    asyncio.create_task(_run_upload(task.id, str(local_path), remote))

    return _ok({"task_id": task.id, "task": tasks.to_dict(task)})


async def _run_upload(task_id: str, local: str, remote: str) -> None:
    tasks.update(task_id, state=TaskState.RUNNING.value, message="启动 tosutil cp")
    try:
        rc = await tosutil.stream_tosutil(
            ["cp", local, remote],
            lambda line: _apply_progress_line(task_id, line),
            on_process=_bind_process(task_id),
        )
    except asyncio.CancelledError:
        tasks.mark_terminal(task_id, TaskState.CANCELLED, message="已取消")
        raise
    except Exception as exc:
        tasks.mark_terminal(task_id, TaskState.ERROR, error=f"{type(exc).__name__}: {exc}")
        return

    if _is_cancelled(task_id):
        return
    if rc == 0:
        tasks.mark_terminal(task_id, TaskState.DONE, progress=1.0, message="完成")
    else:
        tasks.mark_terminal(task_id, TaskState.ERROR, error=f"tosutil exited {rc}")


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

@app.post("/api/download/start")
async def api_download_start(
    path: str = Query(..., description="tos:// 对象路径"),
) -> JSONResponse:
    """启动后台下载任务，返回 task_id。文件下载到 /data/downloads/<safe_name>。"""
    try:
        tosutil.validate_tos_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if path.endswith("/"):
        raise HTTPException(status_code=400, detail="cannot download a directory path")

    remote_basename = path.rsplit("/", 1)[-1]
    if not remote_basename or remote_basename == "tos:":
        raise HTTPException(status_code=400, detail="cannot determine filename from path")

    try:
        safe_name = tosutil._safe_basename(remote_basename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    local_path = DOWNLOAD_DIR / safe_name
    task = tasks.create(TaskKind.DOWNLOAD, path, name=safe_name)
    task.local_path = str(local_path)
    asyncio.create_task(_run_download(task.id, path, str(local_path)))
    return _ok({"task_id": task.id, "task": tasks.to_dict(task)})


async def _run_download(task_id: str, remote: str, local: str) -> None:
    tasks.update(task_id, state=TaskState.RUNNING.value, message="启动 tosutil cp")
    # 确保父目录存在
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    try:
        rc = await tosutil.stream_tosutil(
            ["cp", remote, local],
            lambda line: _apply_progress_line(task_id, line),
            on_process=_bind_process(task_id),
        )
    except asyncio.CancelledError:
        tasks.mark_terminal(task_id, TaskState.CANCELLED, message="已取消")
        raise
    except Exception as exc:
        tasks.mark_terminal(task_id, TaskState.ERROR, error=f"{type(exc).__name__}: {exc}")
        return

    if _is_cancelled(task_id):
        return
    if rc == 0 and Path(local).is_file():
        tasks.mark_terminal(
            task_id,
            TaskState.DONE,
            progress=1.0,
            message="完成",
            local_path=local,
        )
    else:
        tasks.mark_terminal(
            task_id,
            TaskState.ERROR,
            error=f"tosutil exited {rc} (or cached file missing)",
        )


@app.post("/api/download_dir/start")
async def api_download_dir_start(
    path: str = Query(..., description="tos:// 目录路径（递归下载）"),
) -> JSONResponse:
    """
    启动后台目录下载任务，返回 task_id。

    目录会被 `tosutil cp -r` 递归拉到 /data/downloads/<dir_name>/；
    不打包、不流回浏览器，用户从挂载的 /data/downloads/ 自取。
    """
    try:
        tosutil.validate_tos_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    normalized = path if path.endswith("/") else path + "/"
    dir_name = normalized.rstrip("/").rsplit("/", 1)[-1]
    if not dir_name or dir_name == "tos:":
        raise HTTPException(status_code=400, detail="cannot determine directory name")
    try:
        safe_name = tosutil._safe_basename(dir_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    local_path = DOWNLOAD_DIR / safe_name
    task = tasks.create(TaskKind.DOWNLOAD_DIR, normalized, name=safe_name + "/")
    task.local_path = str(local_path)
    asyncio.create_task(_run_download_dir(task.id, normalized, str(local_path)))
    return _ok({"task_id": task.id, "task": tasks.to_dict(task)})


async def _run_download_dir(task_id: str, remote: str, local: str) -> None:
    tasks.update(task_id, state=TaskState.RUNNING.value, message="启动 tosutil cp -r")
    Path(local).mkdir(parents=True, exist_ok=True)
    try:
        rc = await tosutil.stream_tosutil(
            ["cp", "-r", remote, local],
            lambda line: _apply_progress_line(task_id, line),
            on_process=_bind_process(task_id),
        )
    except asyncio.CancelledError:
        tasks.mark_terminal(task_id, TaskState.CANCELLED, message="已取消")
        raise
    except Exception as exc:
        tasks.mark_terminal(task_id, TaskState.ERROR, error=f"{type(exc).__name__}: {exc}")
        return

    if _is_cancelled(task_id):
        return
    if rc == 0:
        tasks.mark_terminal(
            task_id,
            TaskState.DONE,
            progress=1.0,
            message=f"已下载到 {local}",
            local_path=local,
        )
    else:
        tasks.mark_terminal(
            task_id,
            TaskState.ERROR,
            error=f"tosutil exited {rc}",
        )


@app.get("/api/download/{task_id}/file")
def api_download_file(task_id: str):
    t = tasks.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    if t.kind != TaskKind.DOWNLOAD.value:
        raise HTTPException(status_code=400, detail="not a download task")
    if t.state != TaskState.DONE.value:
        raise HTTPException(status_code=409, detail=f"task is {t.state}")
    if not t.local_path or not Path(t.local_path).is_file():
        raise HTTPException(status_code=410, detail="cached file is missing")
    return FileResponse(
        path=t.local_path,
        filename=t.name,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# 目录大小
# ---------------------------------------------------------------------------

@app.post("/api/dirsize")
async def api_dirsize(
    path: str = Query(..., description="要计算大小的 tos:// 目录"),
) -> JSONResponse:
    try:
        tosutil.validate_tos_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    normalized = path if path.endswith("/") else path + "/"
    name = normalized.rstrip("/").rsplit("/", 1)[-1] or normalized
    task = tasks.create(TaskKind.DIRSIZE, normalized, name=name)
    asyncio.create_task(_run_dirsize(task.id, normalized))
    return _ok({"task_id": task.id, "task": tasks.to_dict(task)})


async def _run_dirsize(task_id: str, path: str) -> None:
    tasks.update(task_id, state=TaskState.RUNNING.value, message="启动 tosutil du")
    lines: list[str] = []

    def on_line(line: str) -> None:
        lines.append(line)
        tasks.append_log(task_id, line)
        t = tasks.get(task_id)
        if t is not None:
            t.message = line[:240]

    try:
        rc = await tosutil.stream_tosutil(
            ["du", path],
            on_line,
            on_process=_bind_process(task_id),
        )
    except asyncio.CancelledError:
        tasks.mark_terminal(task_id, TaskState.CANCELLED, message="已取消")
        raise
    except Exception as exc:
        tasks.mark_terminal(task_id, TaskState.ERROR, error=f"{type(exc).__name__}: {exc}")
        return

    if _is_cancelled(task_id):
        return

    tail = "\n".join(lines[-8:])
    if rc != 0:
        tasks.mark_terminal(
            task_id,
            TaskState.ERROR,
            error=f"tosutil exited {rc}",
            message=tail,
        )
        return

    parsed = tosutil.parse_du("\n".join(lines))
    if parsed is None:
        tasks.mark_terminal(
            task_id,
            TaskState.ERROR,
            error="无法从 tosutil du 输出里解析出 Total Size",
            message=tail,
        )
        return

    tasks.mark_terminal(
        task_id,
        TaskState.DONE,
        progress=1.0,
        result=parsed,
        message=f"{parsed['bytes']} bytes" + (
            f" / {parsed['objects']} objects" if parsed.get("objects") is not None else ""
        ),
    )


# ---------------------------------------------------------------------------
# 任务总线
# ---------------------------------------------------------------------------

@app.get("/api/tasks")
def api_tasks(
    since: float = Query(0.0, description="只返回 started_at 或 finished_at >= since 的任务（可选）"),
) -> JSONResponse:
    items = []
    for t in tasks.list_all():
        ts = max(t.finished_at or 0, t.started_at)
        if ts >= since:
            items.append(tasks.to_dict(t))
    return _ok({"tasks": items, "server_time": time.time()})


@app.get("/api/task/{task_id}")
def api_task(task_id: str) -> JSONResponse:
    t = tasks.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _ok({"task": tasks.to_dict(t)})


@app.delete("/api/task/{task_id}")
async def api_task_delete(task_id: str) -> JSONResponse:
    t = tasks.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    if t.state in (TaskState.PENDING.value, TaskState.RUNNING.value):
        ok = await tasks.cancel(task_id)
        return _ok({"action": "cancel", "ok": ok})
    ok = tasks.dismiss(task_id)
    return _ok({"action": "dismiss", "ok": ok})
