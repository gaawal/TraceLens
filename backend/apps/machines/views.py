import logging

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.services.ssh import SSH_SESSION_POOL, execute
from apps.machines.models import ConnectionStatus, Machine
from apps.machines.serializers import MachineConnectionProbeSerializer, MachineSerializer
from apps.machines.services.connection_probe import probe_connection

logger = logging.getLogger("tracelens.machine_api")


class MachineViewSet(viewsets.ModelViewSet):
    serializer_class = MachineSerializer
    queryset = Machine.objects.all()

    def get_queryset(self):
        queryset = super().get_queryset()
        role = self.request.query_params.get("role")
        if role:
            queryset = queryset.filter(role=role)
        return queryset


    def perform_create(self, serializer):
        instance = serializer.save()
        logger.info(
            "machine.created machine=%s role=%s host=%s user=%s environment=%s",
            instance.id, instance.role, instance.host, instance.username, getattr(getattr(instance, "owned_environment", None), "id", None),
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        SSH_SESSION_POOL.invalidate(instance, "machine_updated")
        logger.info(
            "machine.updated machine=%s role=%s host=%s user=%s",
            instance.id, instance.role, instance.host, instance.username,
        )

    @action(detail=False, methods=["post"], url_path="connect-test-draft")
    def connect_test_draft(self, request):
        serializer = MachineConnectionProbeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info(
            "machine.probe.api.start host=%s port=%s user=%s",
            serializer.validated_data["host"],
            serializer.validated_data["ssh_port"],
            serializer.validated_data["username"],
        )
        try:
            result = probe_connection(serializer.validated_data)
            return Response(result)
        except Exception as exc:
            logger.exception("machine.probe.api.failed host=%s", serializer.validated_data["host"])
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["post"], url_path="connect-test")
    def connect_test(self, request, pk=None):
        machine = self.get_object()
        logger.info("machine.connect_test.start machine=%s host=%s", machine.id, machine.host)
        try:
            result = execute(machine, "printf '%s\\n' \"$(hostname)\"; uname -srm", timeout=15)
            if result.exit_status != 0:
                raise RuntimeError(result.stderr or "远程命令失败")
            lines = result.stdout.splitlines()
            machine.connection_status = ConnectionStatus.ONLINE
            machine.last_connection_checked_at = timezone.now()
            machine.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
            logger.info("machine.connect_test.success machine=%s hostname=%s", machine.id, lines[0] if lines else "")
            return Response({"success": True, "hostname": lines[0] if lines else "", "system": " ".join(lines[1:])})
        except Exception as exc:
            logger.exception("machine.connect_test.failed machine=%s", machine.id)
            machine.connection_status = ConnectionStatus.OFFLINE
            machine.last_connection_checked_at = timezone.now()
            machine.save(update_fields=["connection_status", "last_connection_checked_at", "updated_at"])
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["post"], url_path="disconnect-session")
    def disconnect_session(self, request, pk=None):
        machine = self.get_object()
        SSH_SESSION_POOL.invalidate(machine, "api_disconnect")
        logger.info("machine.session.disconnected machine=%s", machine.id)
        return Response({"success": True})
