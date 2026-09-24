"""Keep 实时监听 / 实时采集 watches from outliving the rule they were created for.

Deleting or un-ticking a rule used to leave its watch enabled: an SSH tail nobody asked for,
feeding a rule that no longer exists. The rule lists are the only place that decides this, so
the pruning lives next to them rather than in every caller.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("tracelens.logsources.signals")


@receiver(post_save, sender="environments.ResourceSettings", dispatch_uid="logsources.prune_watches")
def prune_orphan_watches(sender, instance, **kwargs) -> None:
    """Disable watchers whose rule no longer exists or has been disabled.

    Deliberately *not* driven by the opt-in flag: un-ticking a rule is handled by the sync
    callers (the 实时监听 toggle and the assistant tool), which disable exactly what dropped
    out. Pruning on "not ticked" here would silently stop watches that predate the opt-in
    concept the first time any settings page saved — a surprise, not a cleanup.
    """
    from apps.environments.models import ResourceSettings
    from apps.logsources.models import LogWatch
    from apps.logsources.services.watch_capture import SOURCES

    settings_obj = instance if isinstance(instance, ResourceSettings) else ResourceSettings.get_solo()
    for source in SOURCES.values():
        usable = {
            str(rule.get("id"))
            for rule in (getattr(settings_obj, source.settings_field, None) or [])
            if isinstance(rule, dict) and rule.get("enabled", True) is not False
        }
        stale = (
            LogWatch.objects.filter(enabled=True)
            .exclude(**{source.watch_field: ""})
            .exclude(**{f"{source.watch_field}__in": usable})
        )
        for watch in stale:
            watch.enabled = False
            watch.save(update_fields=["enabled", "updated_at"])
            logger.info(
                "watch.pruned kind=%s rule_id=%s reason=rule_missing_or_disabled",
                source.kind,
                getattr(watch, source.watch_field),
            )
