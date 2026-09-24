from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.machines.models import Machine, MachineRole


@receiver(post_save, sender=Machine)
def ensure_upper_machine_environment(sender, instance: Machine, **kwargs) -> None:
    if instance.role != MachineRole.UPPER:
        return
    from apps.environments.models import Environment

    Environment.objects.get_or_create(
        upper_machine=instance,
        defaults={"name": instance.name},
    )


@receiver(post_delete, sender=Machine)
def close_deleted_machine_session(sender, instance: Machine, **kwargs) -> None:
    from apps.common.services.ssh import SSH_SESSION_POOL

    SSH_SESSION_POOL.invalidate(instance, "machine_deleted")
