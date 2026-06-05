"""
FastAPI 入口。

页面与静态：
- /                 -> 返回 static/index.html
- /static/*         -> 静态资源

只读 / 同步类 API：
- GET    /api/buckets                -> 列出所有 bucket（原始）
- GET    /api/list?path=             -> 原始 tosutil ls 输出（raw 调试用）
- GET    /api/browse?path=           -> 结构化浏览（给 UI 用）

后台任务 API：
- POST   /api/dirsize?path=          -> 后台 tosutil du，返回 {task_id}

任务总线：
- GET    /api/tasks                  -> 全部任务
- GET    /api/task/{task_id}         -> 单任务状态
- DELETE /api/task/{task_id}         -> 运行中则 cancel；终态则 dismiss
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import tasks, tosutil
from app.tasks import TaskKind, TaskState
from app.tosutil import TosutilError

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


# ---------------------------------------------------------------------------
# 后台任务工具
# ---------------------------------------------------------------------------

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
