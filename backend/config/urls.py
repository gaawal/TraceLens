from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from apps.common.views import api_root, health_check
from apps.environments.realtime_views import deployment_event_stream
from apps.environments.views import (
    EnvironmentDiscoveryViewSet,
    EnvironmentFolderViewSet,
    EnvironmentViewSet,
    ResourceSettingsViewSet,
    MachineRelationViewSet,
)
from apps.logsources.views import (
    EnvironmentLogViewSet,
    LogFmDefinitionViewSet,
    LogFormatParserRuleViewSet,
    LogQuerySkillViewSet,
    LogSourceRuleViewSet,
    LogSubsystemDefinitionViewSet,
    LogSceneViewSet,
    LogUrlImportViewSet,
)
from apps.logsources.watch_views import LogWatchViewSet, watch_event_stream
from apps.machines.views import MachineViewSet
from apps.reports.views import CpdReportViewSet
from apps.audits.views import DataExtractionRecordViewSet, LogSearchAuditViewSet
from apps.knowledge.views import AbnormalCaseViewSet
from apps.tooling.views import ToolViewSet
from apps.atlog.views import AtLogAnalysisViewSet

router = DefaultRouter()
router.register("machines", MachineViewSet, basename="machine")
router.register("environments", EnvironmentViewSet, basename="environment")
router.register("environment-folders", EnvironmentFolderViewSet, basename="environment-folder")
router.register("resource-settings", ResourceSettingsViewSet, basename="resource-settings")
router.register("environment-logs", EnvironmentLogViewSet, basename="environment-log")
router.register("log-scenes", LogSceneViewSet, basename="log-scene")
router.register("log-url-imports", LogUrlImportViewSet, basename="log-url-import")
router.register("machine-relations", MachineRelationViewSet, basename="machine-relation")
router.register(
    "environment-discoveries",
    EnvironmentDiscoveryViewSet,
    basename="environment-discovery",
)
router.register("log-source-rules", LogSourceRuleViewSet, basename="log-source-rule")
router.register("log-query-skills", LogQuerySkillViewSet, basename="log-query-skill")
router.register("log-format-rules", LogFormatParserRuleViewSet, basename="log-format-rule")
router.register("log-watches", LogWatchViewSet, basename="log-watch")
router.register("log-subsystems", LogSubsystemDefinitionViewSet, basename="log-subsystem")
router.register("log-fms", LogFmDefinitionViewSet, basename="log-fm")
router.register("cpd-reports", CpdReportViewSet, basename="cpd-report")
router.register("log-audits", LogSearchAuditViewSet, basename="log-audit")
router.register("data-extractions", DataExtractionRecordViewSet, basename="data-extraction")
router.register("abnormal-cases", AbnormalCaseViewSet, basename="abnormal-case")
router.register("tools", ToolViewSet, basename="tool")
router.register("atlog-analysis", AtLogAnalysisViewSet, basename="atlog-analysis")

from apps.tooling.task_views import running_tasks, task_events

urlpatterns = [
    path("api/ai/tasks/running", running_tasks),
    path("api/ai/tasks/<str:task_id>/events", task_events),
    path("admin/", admin.site.urls),
    path("api/", api_root, name="api-root"),
    path("api/health/", health_check, name="health-check"),
    path("api/deployment-events/", deployment_event_stream, name="deployment-events"),
    path("api/watch-events/", watch_event_stream, name="watch-events"),
    path("api/", include(router.urls)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
