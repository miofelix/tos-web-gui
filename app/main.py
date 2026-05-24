"""
FastAPI 入口。

- /              -> 返回 static/index.html
- /static/*      -> 静态资源
- /api/buckets   -> 列出所有 bucket
- /api/list      -> 浏览 tos:// 目录
- /api/upload    -> 上传本地文件到 tos://
- /api/download  -> 把 tos:// 对象下载到 /data/downloads
- /api/delete    -> 删除 tos:// 对象
- /api/mkdir     -> 通过上传 .keep 占位对象“创建目录”
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import tosutil
from app.tosutil import TosutilError

# 数据目录：上传缓冲区 + 下载落地目录。
# 在容器里通过 `-v ~/tos-web-data:/data` 持久化。
DATA_DIR = Path(os.environ.get("TOS_WEB_DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DOWNLOAD_DIR = DATA_DIR / "downloads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TOS Web GUI", version="0.1.0")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# 公共错误处理
# ---------------------------------------------------------------------------

def _ok(payload: dict, **extra) -> JSONResponse:
    """统一的成功响应封装。"""
    body = {"success": True, **payload, **extra}
    return JSONResponse(body)


def _handle_tosutil(callable_, *args, **kwargs) -> JSONResponse:
    """把 tosutil 调用的成功/失败统一转成 JSON 响应。"""
    try:
        result = callable_(*args, **kwargs)
    except ValueError as exc:
        # 校验类错误（路径不合法、文件名不合法等）
        raise HTTPException(status_code=400, detail=str(exc))
    except TosutilError as exc:
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
    return _ok(result)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/buckets")
def api_list_buckets() -> JSONResponse:
    return _handle_tosutil(tosutil.list_buckets)


@app.get("/api/list")
def api_list(path: str = Query(..., description="tos://bucket/path/")) -> JSONResponse:
    """原始递归输出，保留给 raw 视图 / curl 调试。"""
    return _handle_tosutil(tosutil.list_path, path)


@app.get("/api/browse")
def api_browse(path: str = Query("", description="tos://bucket/path/ 或留空列出 bucket")) -> JSONResponse:
    """
    结构化浏览接口，供文件浏览器 UI 使用。

    - 空路径或 "tos://" -> 列出所有 bucket
    - "tos://bucket/" 或 "tos://bucket/prefix/" -> 列出当前层目录与对象
    """
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


@app.post("/api/upload")
async def api_upload(
    path: str = Query(..., description="目标 tos:// 目录"),
    file: UploadFile = File(...),
) -> JSONResponse:
    # 校验目标路径
    try:
        tosutil.validate_tos_path(path)
        filename = tosutil._safe_basename(file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    folder = path if path.endswith("/") else path + "/"
    remote = folder + filename

    # 先把上传内容落到 /data/uploads，再交给 tosutil cp
    local_path = UPLOAD_DIR / filename
    try:
        with local_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        await file.close()

    try:
        result = tosutil.upload_file(str(local_path), remote)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TosutilError as exc:
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

    return _ok(result, remote=remote, local=str(local_path), filename=filename)


@app.get("/api/download")
def api_download(path: str = Query(..., description="tos:// 对象路径")):
    """
    下载对象：后端先 `tosutil cp` 到 /data/downloads/<basename>，再以
    `FileResponse` 流回浏览器并触发原生下载弹窗 (Content-Disposition: attachment)。
    服务端文件作为缓存保留，方便重复获取。
    """
    try:
        tosutil.validate_tos_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if path.endswith("/"):
        raise HTTPException(status_code=400, detail="cannot download a directory path")

    remote_basename = path.rsplit("/", 1)[-1]
    if not remote_basename or remote_basename == "tos:":
        raise HTTPException(status_code=400, detail="cannot determine filename from path")

    # 进一步收紧本地文件名，防止远端 key 里的可疑字符
    try:
        safe_name = tosutil._safe_basename(remote_basename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    local_path = DOWNLOAD_DIR / safe_name
    try:
        tosutil.download_file(path, str(local_path))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TosutilError as exc:
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

    if not local_path.is_file():
        raise HTTPException(status_code=500, detail="tosutil reported success but file is missing")

    return FileResponse(
        path=str(local_path),
        filename=safe_name,
        media_type="application/octet-stream",
    )


@app.post("/api/delete")
def api_delete(path: str = Query(..., description="tos:// 对象路径")) -> JSONResponse:
    return _handle_tosutil(tosutil.delete_path, path)


@app.post("/api/mkdir")
def api_mkdir(path: str = Query(..., description="要创建的 tos:// 目录")) -> JSONResponse:
    return _handle_tosutil(tosutil.mkdir, path)
