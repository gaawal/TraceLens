import os

from django.apps import AppConfig


class EnvironmentsConfig(AppConfig):
    name = "apps.environments"
    verbose_name = "环境拓扑"

    def ready(self):
        # Deployment execution/scheduling belongs to the dedicated deployment_worker
        # process. Keeping it out of ASGI workers prevents long SSH jobs from consuming
        # web-process resources. This opt-in switch exists only for legacy deployments
        # that intentionally run without the dedicated worker service.
        if os.getenv("TRACELENS_DEPLOYMENT_EMBEDDED_SCHEDULER", "false").lower() in {"1", "true", "yes", "on"}:
            from apps.environments.services.deployment_scheduler import ensure_scheduler_started
            ensure_scheduler_started()
