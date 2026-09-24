from django.apps import AppConfig


class CommonConfig(AppConfig):
    name = "apps.common"
    verbose_name = "公共能力"

    def ready(self):
        # Register SQLite WAL/busy-timeout connection hooks. The module import is the
        # registration side effect; non-SQLite databases are ignored by the receiver.
        from apps.common import sqlite_pragmas  # noqa: F401
