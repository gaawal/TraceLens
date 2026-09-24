from django.apps import AppConfig


class LogSourcesConfig(AppConfig):
    name = "apps.logsources"
    verbose_name = "日志源"

    def ready(self):
        # Import for the side effect of registering the capture-watch pruning receiver.
        from apps.logsources import signals  # noqa: F401
