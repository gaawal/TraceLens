from __future__ import annotations

import hashlib
import logging

from django.core import signing

from apps.common.services.ssh import execute
from apps.machines.models import AuthenticationType, Machine

logger = logging.getLogger("tracelens.machine_probe")
TOKEN_SALT = "tracelens.machine.connection-test.v1"


def credential_digest(
    auth_type: str,
    password: str = "",
    private_key: str = "",
    private_key_passphrase: str = "",
) -> str:
    secret = password if auth_type == AuthenticationType.PASSWORD else f"{private_key}\0{private_key_passphrase}"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalized_payload(data: dict) -> dict:
    return {
        "host": str(data.get("host", "")).strip(),
        "ssh_port": int(data.get("ssh_port", 22)),
        "username": str(data.get("username", "")).strip(),
        "auth_type": str(data.get("auth_type", AuthenticationType.NONE)),
        "credential_digest": credential_digest(
            str(data.get("auth_type", AuthenticationType.NONE)),
            str(data.get("password", "")),
            str(data.get("private_key", "")),
            str(data.get("private_key_passphrase", "")),
        ),
    }


def probe_connection(data: dict) -> dict:
    payload = normalized_payload(data)
    machine = Machine(
        name=f"{payload['host']}({payload['username']})",
        host=payload["host"],
        ssh_port=payload["ssh_port"],
        username=payload["username"],
        auth_type=payload["auth_type"],
        role=data.get("role", "upper"),
    )
    if payload["auth_type"] == AuthenticationType.PASSWORD:
        machine.set_password(str(data.get("password", "")))
    elif payload["auth_type"] == AuthenticationType.PRIVATE_KEY:
        machine.set_private_key(str(data.get("private_key", "")))
        machine.set_private_key_passphrase(str(data.get("private_key_passphrase", "")))
    logger.info("machine.probe.start host=%s port=%s user=%s", machine.host, machine.ssh_port, machine.username)
    result = execute(machine, 'printf \'%s\\n\' "$(hostname)"; uname -srm', timeout=15)
    if result.exit_status != 0:
        raise RuntimeError(result.stderr or "远程连接测试失败")
    lines = result.stdout.splitlines()
    token = signing.dumps(payload, salt=TOKEN_SALT, compress=True)
    logger.info("machine.probe.success host=%s hostname=%s", machine.host, lines[0] if lines else "")
    return {
        "success": True,
        "hostname": lines[0] if lines else "",
        "system": " ".join(lines[1:]),
        "connection_test_token": token,
    }


def verify_probe_token(token: str, data: dict, max_age: int = 300) -> None:
    try:
        signed = signing.loads(token, salt=TOKEN_SALT, max_age=max_age)
    except signing.BadSignature as exc:
        raise ValueError("连接测试凭证无效或已过期，请重新测试连接。") from exc
    if signed != normalized_payload(data):
        raise ValueError("机器连接参数已变化，请重新测试连接。")
