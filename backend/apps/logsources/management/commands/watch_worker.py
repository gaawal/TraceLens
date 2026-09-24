"""Dedicated process for realtime log watches.

Run alongside the web process, not inside it — a watch holds long-lived SSH ``tail -F``
channels and must keep running when no browser is connected::

    python manage.py watch_worker --poll-seconds 2

Safe to run several replicas: each watch is guarded by a Redis lease, so exactly one worker
tails it. That also means Redis is required for multi-worker operation; a single worker
refuses to start watches without it rather than risking duplicate hits.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import uuid

from django.core.management.base import BaseCommand

from apps.logsources.models import LogWatch
from apps.logsources.services.watch_events import WatchEventBus
from apps.logsources.services.watch_worker import WatchSupervisor

logger = logging.getLogger("tracelens.logsources.watch_worker_command")


class Command(BaseCommand):
    help = "Evaluate TraceLens log watches against live logs, independently of any browser."

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=float(os.getenv("TRACELENS_WATCH_POLL_SECONDS", "2")),
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Reconcile a single time and exit (for smoke tests).",
        )

    def handle(self, *args, **options):
        worker_id = uuid.uuid4().hex
        supervisor = WatchSupervisor(token=worker_id, poll_seconds=float(options["poll_seconds"]))

        if not WatchEventBus.enabled():
            self.stdout.write(self.style.WARNING(
                "Redis 不可用：watch_worker 无法获取租约，将以单机模式拒绝启动监控。"
            ))

        enabled = LogWatch.objects.filter(enabled=True).count()
        self.stdout.write(self.style.SUCCESS(
            f"TraceLens watch worker started (worker={worker_id[:8]}, enabled_watches={enabled}, "
            f"poll={options['poll_seconds']}s)"
        ))

        if options["once"]:
            supervisor._reconcile()
            self.stdout.write(self.style.SUCCESS("Reconciled once."))
            supervisor.stop()
            return

        stopping = threading.Event()

        def request_stop(_signum=None, _frame=None):
            stopping.set()

        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is not None:
                try:
                    signal.signal(sig, request_stop)
                except (ValueError, OSError):
                    pass

        thread = threading.Thread(target=supervisor.run_forever, name="watch-supervisor", daemon=True)
        thread.start()
        try:
            while not stopping.is_set():
                stopping.wait(0.5)
        finally:
            supervisor.stop()
            self.stdout.write(self.style.SUCCESS("TraceLens watch worker stopped"))
