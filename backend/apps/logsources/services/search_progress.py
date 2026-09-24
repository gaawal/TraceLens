from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from typing import Any

from apps.logsources.services.redis_store import RedisLogStore

logger = logging.getLogger("tracelens.log_search_progress")


class LogSearchCancelled(RuntimeError):
    """Raised when a running remote log operation was cancelled by the user."""


class LogSearchProgressStore:
    """Operation progress + cooperative cancellation state.

    Redis is preferred so multiple Django workers see the same state. A local
    fallback keeps cancellation/progress usable when Redis is unavailable.
    """

    TTL = 3600
    _local: dict[str, tuple[float, dict[str, Any]]] = {}
    _lock = threading.RLock()

    @classmethod
    def _key(cls, operation_id: str) -> str:
        return f"tracelens:progress:{operation_id}"

    @classmethod
    def _cleanup(cls) -> None:
        now = time.monotonic()
        with cls._lock:
            for key, (expires, _) in list(cls._local.items()):
                if expires <= now:
                    cls._local.pop(key, None)

    @classmethod
    def get(cls, operation_id: str) -> dict[str, Any] | None:
        if not operation_id:
            return None
        value = RedisLogStore.get_json(cls._key(operation_id))
        if isinstance(value, dict):
            return value
        cls._cleanup()
        with cls._lock:
            item = cls._local.get(operation_id)
            return deepcopy(item[1]) if item else None

    @classmethod
    def put(cls, operation_id: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = {**value, "operation_id": operation_id, "updated_at": time.time()}
        RedisLogStore.set_json(cls._key(operation_id), payload, cls.TTL)
        with cls._lock:
            cls._local[operation_id] = (time.monotonic() + cls.TTL, deepcopy(payload))
        return payload

    @classmethod
    def start(
        cls,
        operation_id: str,
        *,
        environment_id: int | None = None,
        source_type: str = "environment",
        source_ref: str = "",
    ) -> dict[str, Any]:
        logger.info(
            "log.search.progress.start operation=%s source_type=%s environment=%s source_ref=%s",
            operation_id, source_type, environment_id, source_ref,
        )
        return cls.put(operation_id, {
            "environment_id": environment_id,
            "source_type": source_type,
            "source_ref": source_ref,
            "stage": "planning",
            "percent": 4,
            "current_subsystem": "",
            "current_module": "",
            "current_host": "",
            "current_username": "",
            "current_source_category": "",
            "current_root": "",
            "current_directory": "",
            "current_action": "正在准备日志检索",
            "target_total": 0,
            "target_current": 0,
            "current_subsystem_files": 0,
            "current_subsystem_checked": 0,
            "discovered_files": 0,
            "checked_files": 0,
            "selected_files": 0,
            "artifact_total": 0,
            "artifact_done": 0,
            "message": "正在准备日志检索",
            "done": False,
            "cancel_requested": False,
            "cancelled": False,
            "error": "",
        })

    @classmethod
    def patch(cls, operation_id: str, **patch: Any) -> dict[str, Any]:
        current = cls.get(operation_id) or {"operation_id": operation_id}
        current.update(patch)
        return cls.put(operation_id, current)

    @classmethod
    def cancel(cls, operation_id: str) -> dict[str, Any]:
        logger.info("log.search.cancel.request operation=%s", operation_id)
        current = cls.get(operation_id) or {"operation_id": operation_id}
        if current.get("done"):
            return current
        return cls.patch(
            operation_id,
            cancel_requested=True,
            stage="cancelling",
            message="正在停止日志检索",
        )

    @classmethod
    def is_cancelled(cls, operation_id: str) -> bool:
        if not operation_id or operation_id == "-":
            return False
        current = cls.get(operation_id)
        return bool(current and current.get("cancel_requested"))

    @classmethod
    def raise_if_cancelled(cls, operation_id: str) -> None:
        if cls.is_cancelled(operation_id):
            raise LogSearchCancelled("日志检索已停止。")

    @classmethod
    def cancelled(cls, operation_id: str) -> dict[str, Any]:
        logger.info("log.search.cancel.complete operation=%s", operation_id)
        return cls.patch(
            operation_id,
            stage="cancelled",
            percent=100,
            done=True,
            cancel_requested=True,
            cancelled=True,
            message="日志检索已停止",
            error="",
        )

    @classmethod
    def target_started(
        cls, operation_id: str, *, host: str, username: str = "",
        source_category: str = "", root: str = "", target_current: int = 0, target_total: int = 0,
    ) -> dict[str, Any]:
        current = cls.get(operation_id) or {}
        return cls.patch(
            operation_id,
            stage="indexing",
            percent=max(5, int(current.get("percent", 4))),
            current_host=host,
            current_username=username,
            current_source_category=source_category,
            current_root=root,
            current_directory=root,
            current_directory_candidates=0,
            current_directory_selected=0,
            target_current=max(0, int(target_current)),
            target_total=max(0, int(target_total)),
            current_action="正在连接日志资源",
            message=f"正在连接 {host}" if host else "正在连接日志资源",
        )

    @classmethod
    def directory_started(
        cls, operation_id: str, *, directory: str, subsystem: str = "", modules: list[str] | tuple[str, ...] | set[str] | None = None,
        action: str = "正在读取目录",
    ) -> dict[str, Any]:
        module_names = [str(item) for item in (modules or []) if str(item)]
        current = cls.get(operation_id) or {}
        return cls.patch(
            operation_id,
            stage="indexing",
            percent=max(6, int(current.get("percent", 5))),
            current_directory=directory,
            current_subsystem=subsystem,
            current_module=", ".join(module_names[:4]),
            current_action=action,
            current_subsystem_files=0,
            current_subsystem_checked=0,
            current_directory_candidates=0,
            current_directory_selected=0,
            message=action,
        )

    @classmethod
    def candidates_found(
        cls, operation_id: str, *, directory: str, subsystem: str = "", discovered: int = 0, selected: int = 0,
        action: str = "正在按时间筛选候选文件",
    ) -> dict[str, Any]:
        current = cls.get(operation_id) or {}
        return cls.patch(
            operation_id,
            stage="indexing",
            current_directory=directory,
            current_subsystem=subsystem or current.get("current_subsystem", ""),
            current_action=action,
            current_subsystem_files=max(0, int(discovered)),
            current_subsystem_checked=0,
            current_directory_candidates=max(0, int(discovered)),
            current_directory_selected=max(0, int(selected)),
            message=f"发现 {max(0, int(discovered))} 个候选，时间范围命中 {max(0, int(selected))} 个",
        )

    @classmethod
    def add_files(
        cls,
        operation_id: str,
        *,
        subsystem: str,
        module_count: int,
        discovered: int,
        checked: int = 0,
        selected: int = 0,
    ) -> dict[str, Any]:
        current = cls.get(operation_id) or {}
        discovered_total = int(current.get("discovered_files", 0)) + max(0, discovered)
        checked_total = int(current.get("checked_files", 0)) + max(0, checked)
        selected_total = int(current.get("selected_files", 0)) + max(0, selected)
        prior = int(current.get("percent", 4))
        ratio = checked_total / max(1, discovered_total)
        percent = max(prior, min(55, 5 + int(ratio * 50)))
        return cls.patch(
            operation_id,
            stage="indexing",
            percent=percent,
            current_subsystem=subsystem,
            current_module_count=module_count,
            current_subsystem_files=max(0, discovered),
            current_subsystem_checked=max(0, checked),
            discovered_files=discovered_total,
            checked_files=checked_total,
            selected_files=selected_total,
            current_action="正在检查候选文件",
            current_directory_candidates=max(0, discovered),
            message=f"{subsystem} · {discovered} 个候选文件",
        )

    @classmethod
    def artifact_checked(cls, operation_id: str, *, count: int = 1, selected_increment: int = 0) -> dict[str, Any]:
        current = cls.get(operation_id) or {}
        discovered = int(current.get("discovered_files", 0))
        checked = int(current.get("checked_files", 0)) + count
        selected = int(current.get("selected_files", 0)) + selected_increment
        current_subsystem_files = int(current.get("current_subsystem_files", 0))
        current_subsystem_checked = min(current_subsystem_files, int(current.get("current_subsystem_checked", 0)) + count) if current_subsystem_files else 0
        ratio = checked / max(1, discovered)
        return cls.patch(
            operation_id,
            current_subsystem_checked=current_subsystem_checked,
            checked_files=checked,
            selected_files=selected,
            current_action="正在检查候选文件",
            percent=max(int(current.get("percent", 5)), min(55, 5 + int(ratio * 50))),
        )

    @classmethod
    def plan_ready(cls, operation_id: str, *, artifact_total: int) -> dict[str, Any]:
        return cls.patch(
            operation_id,
            stage="reading",
            percent=60 if artifact_total else 96,
            artifact_total=artifact_total,
            artifact_done=0,
            selected_files=artifact_total,
            matched_files=artifact_total,
            total_files=artifact_total,
            read_files=0,
            current_action="文件定位完成，准备读取日志",
            message=f"已定位 {artifact_total} 个日志文件",
        )

    @classmethod
    def artifact_started(
        cls,
        operation_id: str,
        *,
        identity: str,
        display_name: str,
        full_path: str,
        subsystem: str = "",
        module: str = "",
    ) -> dict[str, Any]:
        current = cls.get(operation_id) or {}
        total = int(current.get("artifact_total", 0))
        done = int(current.get("artifact_done", 0))
        current_index = min(total, done + 1) if total else done + 1
        # Reading progress starts at 60%. Keep the percentage based on completed
        # files, while exposing current_index separately so the UI can say
        # "正在读取 3/8" before file 3 has completed.
        percent = 60 + int((done / max(1, total)) * 35) if total else 60
        return cls.patch(
            operation_id,
            stage="reading",
            percent=min(95, percent),
            current_subsystem=subsystem or current.get("current_subsystem", ""),
            current_module=module or current.get("current_module", ""),
            current_artifact=identity,
            current_file=display_name,
            current_action="正在读取命中日志",
            current_file_path=full_path,
            artifact_current=current_index,
            message=f"正在读取 {current_index}/{total} 个日志文件" if total else f"正在读取 {display_name}",
        )

    @classmethod
    def artifact_done(cls, operation_id: str, *, identity: str) -> dict[str, Any]:
        current = cls.get(operation_id) or {}
        total = int(current.get("artifact_total", 0))
        done = min(total, int(current.get("artifact_done", 0)) + 1) if total else 0
        percent = 60 + int((done / max(1, total)) * 35) if total else 95
        return cls.patch(
            operation_id,
            stage="reading",
            percent=min(95, percent),
            artifact_done=done,
            read_files=done,
            artifact_current=min(total, done + 1) if total and done < total else done,
            message=f"已读取 {done}/{total} 个日志文件" if total else "正在读取日志",
            current_artifact=identity,
        )

    @classmethod
    def finish(cls, operation_id: str) -> dict[str, Any]:
        if cls.is_cancelled(operation_id):
            return cls.cancelled(operation_id)
        logger.info("log.search.progress.finish operation=%s", operation_id)
        return cls.patch(operation_id, stage="complete", percent=100, done=True, message="日志获取完成")

    @classmethod
    def fail(cls, operation_id: str, error: str) -> dict[str, Any]:
        if cls.is_cancelled(operation_id):
            return cls.cancelled(operation_id)
        logger.warning("log.search.progress.failed operation=%s error=%s", operation_id, error)
        return cls.patch(operation_id, stage="error", done=True, error=error, message="日志检索失败")


def request_log_search_cancel(operation_id: str) -> dict[str, Any]:
    """Shared cancellation entry point for every log-search origin.

    Environment resources and ATLog URL sources intentionally use the same
    operation store/cancellation contract; origin-specific views only own their
    surrounding audit or response behavior.
    """
    return LogSearchProgressStore.cancel(operation_id)
