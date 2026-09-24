from __future__ import annotations

import threading
import time
from typing import Any
from dataclasses import dataclass, field


@dataclass
class RunStateStore:
    """Thread-safe lifecycle state for interactive assistant runs.

    Keeping lifecycle state behind an object makes cancellation/guidance logic
    independently testable and removes direct mutation of module-level sets and
    dictionaries from the assistant orchestration code.
    """

    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _active: set[str] = field(default_factory=set, init=False, repr=False)
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)
    _guidance: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    _receipts: dict[tuple[str, str], dict[str, Any] | None] = field(default_factory=dict, init=False, repr=False)

    _durable: set[str] = field(default_factory=set, init=False, repr=False)

    def make_durable(self, run_id: str):
        self._durable.add(run_id)

    def is_ui_pending(self, run_id, action_id):
        from django.utils import timezone
        from apps.tooling.models import AgentUiReceipt
        return AgentUiReceipt.objects.filter(task_id=run_id, action_id=action_id, receipt__isnull=True, expires_at__gt=timezone.now()).exists()

    def expect_ui(self, run_id: str, action_id: str) -> None:
        if run_id in self._durable:
            from datetime import timedelta
            from django.utils import timezone
            from apps.tooling.models import AgentUiReceipt
            AgentUiReceipt.objects.create(task_id=run_id, action_id=action_id, expires_at=timezone.now()+timedelta(seconds=60))
        with self._lock:
            self._receipts[(run_id, action_id)] = None

    def acknowledge_ui(self, run_id: str, action_id: str, receipt: dict[str, Any]) -> bool:
        with self._lock:
            key = (run_id, action_id)
            if key not in self._receipts or self._receipts[key] is not None or run_id in self._cancelled:
                return False
            self._receipts[key] = receipt
            return True

    def wait_ui(self, run_id: str, action_id: str, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        key = (run_id, action_id)
        try:
            while time.monotonic() < deadline:
                if run_id in self._durable:
                    from apps.tooling.models import AgentUiReceipt
                    if self.is_cancelled(run_id): return {"status":"failed", "detail":"用户已停止任务"}
                    record = AgentUiReceipt.objects.filter(task_id=run_id,action_id=action_id).first()
                    if record and record.receipt is not None: return record.receipt
                with self._lock:
                    if run_id in self._cancelled:
                        return {"status": "failed", "detail": "用户已接管页面"}
                    receipt = self._receipts.get(key)
                    if receipt is not None:
                        return receipt
                time.sleep(0.03)
            return {"status": "failed", "detail": "未收到页面完成回执；停止后续操作"}
        finally:
            if run_id in self._durable:
                from apps.tooling.models import AgentUiReceipt
                from django.utils import timezone
                AgentUiReceipt.objects.filter(task_id=run_id,action_id=action_id).update(expires_at=timezone.now())
            with self._lock:
                self._receipts.pop(key, None)

    @staticmethod
    def _normalize(run_id: str) -> str:
        return str(run_id or "").strip()

    def register(self, run_id: str) -> None:
        run_id = self._normalize(run_id)
        if not run_id:
            return
        with self._lock:
            self._active.add(run_id)
            self._cancelled.discard(run_id)
            self._guidance.setdefault(run_id, [])

    def cancel(self, run_id: str) -> bool:
        run_id = self._normalize(run_id)
        if not run_id:
            return False
        with self._lock:
            active = run_id in self._active
            self._cancelled.add(run_id)
            return active

    def guide(self, run_id: str, message: str) -> bool:
        run_id = self._normalize(run_id)
        message = str(message or "").strip()
        if not run_id or not message:
            return False
        with self._lock:
            if run_id not in self._active:
                return False
            queue = self._guidance.setdefault(run_id, [])
            queue.append(message[:2000])
            if len(queue) > 8:
                del queue[:-8]
            return True

    def drain_guidance(self, run_id: str) -> list[str]:
        run_id = self._normalize(run_id)
        if not run_id:
            return []
        with self._lock:
            items = list(self._guidance.get(run_id) or [])
            self._guidance[run_id] = []
            return items

    def finish(self, run_id: str) -> None:
        run_id = self._normalize(run_id)
        if not run_id:
            return
        with self._lock:
            self._durable.discard(run_id)
            self._active.discard(run_id)
            self._cancelled.discard(run_id)
            self._guidance.pop(run_id, None)
            for key in list(self._receipts):
                if key[0] == run_id:
                    self._receipts.pop(key, None)

    def is_cancelled(self, run_id: str) -> bool:
        run_id = self._normalize(run_id)
        if not run_id:
            return False
        if run_id in self._durable:
            from apps.tooling.models import AgentTask
            if AgentTask.objects.filter(task_id=run_id,status='cancelling').exists(): return True
        with self._lock:
            return run_id in self._cancelled


RUN_STATE = RunStateStore()
