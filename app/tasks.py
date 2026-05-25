"""
进程内的任务登记表。

FastAPI 单进程运行，没必要引入 Redis；这里就是一个 dict 加几个小工具。
所有「上传 / 下载 / 目录大小」这类需要进度反馈的操作都注册成 Task，
前端通过轮询 /api/tasks 拿状态。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskKind(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DOWNLOAD_DIR = "download_dir"
    DIRSIZE = "dirsize"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


_TERMINAL_STATES = {TaskState.DONE, TaskState.ERROR, TaskState.CANCELLED}


@dataclass
class Task:
    id: str
    kind: str                              # TaskKind.value
    path: str                              # tos:// 远端路径
    name: str                              # 显示名（通常是 basename）
    state: str = TaskState.PENDING.value
    progress: float | None = None          # 0.0 ~ 1.0，未知时 None
    bytes_done: int | None = None
    bytes_total: int | None = None
    speed: str | None = None               # 从 tosutil 输出原样抽取
    message: str | None = None             # 最新一行输出
    error: str | None = None
    result: dict | None = None             # 完成后的结构化结果（dirsize 用）
    local_path: str | None = None          # download 缓存或 upload 源
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    # 终端样式日志：tosutil 的 stdout/stderr 帧按时间顺序追加。
    # 给前端「像终端一样的进度面板」用。长度受 _LOG_MAX 约束。
    log_lines: list[str] = field(default_factory=list)

    # 内部：subprocess 句柄，用于 cancel。不序列化。
    _process: Any = None


# 模块级单例
_TASKS: dict[str, Task] = {}
# 已完成任务的最大保留条数；超出后按完成时间淘汰
_MAX_RETAIN = 100
# 每个任务保留的日志行上限（防内存泄漏）
_LOG_MAX = 300
# 序列化给前端的尾部行数
_LOG_TAIL = 80


def create(kind: TaskKind | str, path: str, name: str | None = None) -> Task:
    """新建一个 Task，自动生成 id 并注册到表里。"""
    task_id = uuid.uuid4().hex[:12]
    kind_value = kind.value if isinstance(kind, TaskKind) else kind
    t = Task(id=task_id, kind=kind_value, path=path, name=name or path)
    _TASKS[task_id] = t
    _evict_if_needed()
    return t


def get(task_id: str) -> Task | None:
    return _TASKS.get(task_id)


def list_all() -> list[Task]:
    # 活动中的排前面，再按 started_at 倒序
    items = list(_TASKS.values())
    items.sort(key=lambda x: (x.state in _TERMINAL_STATES, -x.started_at))
    return items


def update(task_id: str, **fields_) -> Task | None:
    """就地更新 Task 字段。未知 task_id 返回 None。"""
    t = _TASKS.get(task_id)
    if t is None:
        return None
    for k, v in fields_.items():
        setattr(t, k, v)
    return t


def mark_terminal(task_id: str, state: TaskState, **fields_) -> Task | None:
    """设置终态并自动写入 finished_at。"""
    if "finished_at" not in fields_:
        fields_["finished_at"] = time.time()
    fields_["state"] = state.value
    return update(task_id, **fields_)


async def cancel(task_id: str) -> bool:
    """杀掉子进程并标记 CANCELLED。已经是终态则返回 False。"""
    t = _TASKS.get(task_id)
    if t is None:
        return False
    if t.state in (s.value for s in _TERMINAL_STATES):
        return False
    proc = t._process
    if proc is not None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # 给子进程一点时间退出，避免 zombie
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except (asyncio.TimeoutError, Exception):
            pass
    mark_terminal(task_id, TaskState.CANCELLED, message="已取消")
    return True


def dismiss(task_id: str) -> bool:
    """从注册表里抹掉。只允许 dismiss 终态任务，避免误删活跃任务。"""
    t = _TASKS.get(task_id)
    if t is None:
        return False
    if t.state not in (s.value for s in _TERMINAL_STATES):
        return False
    _TASKS.pop(task_id, None)
    return True


def append_log(task_id: str, line: str) -> None:
    """把一行 tosutil 输出追加到任务日志，超出上限时丢最早的。"""
    t = _TASKS.get(task_id)
    if t is None:
        return
    t.log_lines.append(line)
    if len(t.log_lines) > _LOG_MAX:
        # 一次切掉一段，避免每行都 pop(0)
        t.log_lines = t.log_lines[-_LOG_MAX:]


def to_dict(t: Task) -> dict[str, Any]:
    """序列化给前端用（剔除 _process，log 只回末尾 N 行）。"""
    return {
        "id": t.id,
        "kind": t.kind,
        "path": t.path,
        "name": t.name,
        "state": t.state,
        "progress": t.progress,
        "bytes_done": t.bytes_done,
        "bytes_total": t.bytes_total,
        "speed": t.speed,
        "message": t.message,
        "error": t.error,
        "result": t.result,
        "local_path": t.local_path,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "log_tail": t.log_lines[-_LOG_TAIL:],
    }


def _evict_if_needed() -> None:
    """超出保留上限时，按 finished_at（未完成的算 +inf）从旧到新淘汰。"""
    if len(_TASKS) <= _MAX_RETAIN:
        return
    candidates = [
        t for t in _TASKS.values()
        if t.state in (s.value for s in _TERMINAL_STATES)
    ]
    candidates.sort(key=lambda x: x.finished_at or 0)
    to_drop = len(_TASKS) - _MAX_RETAIN
    for t in candidates[:to_drop]:
        _TASKS.pop(t.id, None)
