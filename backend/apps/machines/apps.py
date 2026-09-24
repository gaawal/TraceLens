from django.apps import AppConfig


class MachinesConfig(AppConfig):
    name = "apps.machines"
    verbose_name = "机器资源"

    def ready(self) -> None:
        from apps.machines import signals  # noqa: F401
