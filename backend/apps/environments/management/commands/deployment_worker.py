from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db.utils import OperationalError, ProgrammingError

from apps.environments.models import DeploymentStatus, EnvironmentDeployment
from apps.environments.services.deployment import (
    _run_deployment,
    activate_due_scheduled_deployments,
    recover_orphaned_scheduled_deployments,
)
from apps.environments.services.deployment_events import DeploymentEventBus

logger = logging.getLogger("tracelens.environment_deployment_worker")
process_logger = logging.getLogger("tracelens.process.deployment")


class Command(BaseCommand):
    help = "Run TraceLens deployments outside the ASGI web process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--concurrency",
            type=int,
            default=int(os.getenv("TRACELENS_DEPLOYMENT_WORKER_CONCURRENCY", "4")),
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=float(os.getenv("TRACELENS_DEPLOYMENT_WORKER_POLL_SECONDS", "0.5")),
        )

    def handle(self, *args, **options):
        concurrency = max(1, min(32, int(options["concurrency"])))
        poll_seconds = max(0.1, float(options["poll_seconds"]))
        stopping = threading.Event()
        worker_id = uuid.uuid4().hex

        def request_stop(_signum=None, _frame=None):
            stopping.set()

        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is not None:
                try:
                    signal.signal(sig, request_stop)
                except (ValueError, OSError):
                    pass

        self.stdout.write(self.style.SUCCESS(
            f"TraceLens deployment worker started (concurrency={concurrency}, poll={poll_seconds:.1f}s)"
        ))
        process_logger.info(
            "[DEPLOY-WORKER] start worker=%s concurrency=%s poll=%.1fs",
            worker_id[:8], concurrency, poll_seconds,
        )
        running: dict[int, Future] = {}
        executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="deployment-worker")

        try:
            while not stopping.is_set():
                close_old_connections()
                DeploymentEventBus.refresh_worker_heartbeat(worker_id)
                try:
                    # Scheduled tasks are only moved into the common PENDING queue here.
                    # Execution itself always happens through this worker pool.
                    recover_orphaned_scheduled_deployments(limit=max(20, concurrency * 4))
                    activate_due_scheduled_deployments(limit=max(20, concurrency * 4), launch=False)

                    finished_ids = [deployment_id for deployment_id, future in running.items() if future.done()]
                    for deployment_id in finished_ids:
                        future = running.pop(deployment_id)
                        try:
                            future.result()
                        except Exception:
                            logger.exception("deployment worker future failed id=%s", deployment_id)

                    capacity = concurrency - len(running)
                    if capacity > 0:
                        candidates = list(
                            EnvironmentDeployment.objects.filter(status=DeploymentStatus.PENDING)
                            .order_by("created_at", "id")
                            .values("id", "current_step", "scheduled_at")[: max(capacity * 3, capacity)]
                        )
                        for candidate in candidates:
                            if capacity <= 0:
                                break
                            deployment_id = int(candidate["id"])
                            if deployment_id in running:
                                continue

                            runner_token = uuid.uuid4().hex
                            claim = DeploymentEventBus.try_claim_runner(deployment_id, runner_token)
                            if claim is False:
                                continue
                            # When Redis is disabled/unavailable there is no cross-process
                            # claim primitive. The supported deployment topology starts one
                            # dedicated worker service, so this process-local guard remains
                            # sufficient while SQLite stays authoritative.
                            start_key = str(candidate.get("current_step") or "").strip() or None
                            # Every task revalidates remote trust/time in the worker.
                            # This keeps HTTP task creation fast and avoids relying on
                            # stale checks from an open browser.
                            run_preflight = True
                            process_logger.info(
                                "[DEPLOY-WORKER] claim task=%s start_key=%s redis_claim=%s",
                                deployment_id, start_key or "<initial>", claim,
                            )
                            future = executor.submit(
                                _run_deployment,
                                deployment_id,
                                start_key,
                                run_preflight,
                                runner_token if claim is True else None,
                            )
                            running[deployment_id] = future
                            capacity -= 1
                except (OperationalError, ProgrammingError):
                    # Startup may race migrations when the worker and API container start
                    # together. Keep polling until the database schema is ready.
                    pass
                except Exception:
                    logger.exception("deployment worker poll failed")
                finally:
                    close_old_connections()

                stopping.wait(poll_seconds)
        finally:
            DeploymentEventBus.clear_worker_heartbeat(worker_id)
            # Do not start new deployments after shutdown begins. Existing tasks get a
            # short grace period; container/process shutdown will then close SSH channels.
            executor.shutdown(wait=False, cancel_futures=True)
            close_old_connections()
            process_logger.info("[DEPLOY-WORKER] stop worker=%s", worker_id[:8])
            self.stdout.write("TraceLens deployment worker stopped")
