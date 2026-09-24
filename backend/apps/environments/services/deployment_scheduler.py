from __future__ import annotations

import logging
import threading
import uuid

from django.db import close_old_connections
from django.db.utils import OperationalError, ProgrammingError

logger = logging.getLogger("tracelens.environment_deployment_scheduler")

_started = False
_guard = threading.Lock()


def _loop() -> None:
    # A short server-side scan is independent of browser count. scheduled_at is indexed,
    # and Redis/database claim guards make duplicate ASGI workers safe.
    if threading.Event().wait(2.0):
        return
    while True:
        try:
            close_old_connections()
            from apps.environments.services.deployment import (
                activate_due_scheduled_deployments,
                recover_orphaned_scheduled_deployments,
            )
            from apps.environments.services.deployment_events import DeploymentEventBus
            tick_token = uuid.uuid4().hex
            tick_lock = DeploymentEventBus.try_scheduler_tick_lock(tick_token, ttl_seconds=4)
            if tick_lock is not False:
                try:
                    recover_orphaned_scheduled_deployments(limit=20)
                    activate_due_scheduled_deployments(limit=20)
                finally:
                    if tick_lock is True:
                        DeploymentEventBus.release_scheduler_tick_lock(tick_token)
        except (OperationalError, ProgrammingError):
            # Database may not be migrated yet during first startup. Retry after it is ready.
            pass
        except Exception:
            logger.exception("scheduled deployment scan failed")
        finally:
            close_old_connections()
        threading.Event().wait(2.0)


def ensure_scheduler_started() -> None:
    global _started
    with _guard:
        if _started:
            return
        _started = True
        threading.Thread(target=_loop, daemon=True, name="deployment-scheduler").start()
