from __future__ import annotations

import logging

from django.db.backends.signals import connection_created
from django.dispatch import receiver

logger = logging.getLogger("tracelens.sqlite")


@receiver(connection_created, dispatch_uid="tracelens.sqlite.concurrent-pragmas")
def configure_sqlite_for_concurrency(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout=30000")
            # WAL lets web readers continue while the deployment worker persists logs.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        # Never make an otherwise valid database connection unusable because a host
        # filesystem does not support one of the optional SQLite pragmas.
        logger.exception("failed to configure SQLite concurrency pragmas")
