from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Iterable

from django.conf import settings

from apps.environments.models import DeploymentStatus, EnvironmentDeployment
from apps.environments.serializers import EnvironmentDeploymentSummarySerializer
from apps.logsources.services.redis_store import RedisLogStore

logger = logging.getLogger("tracelens.environment_deployment_events")

EVENT_CHANNEL = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:events"
ACTIVE_INDEX_KEY = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:active-environments"
ACTIVE_READY_KEY = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:active-index-ready"
ACTIVE_KEY_PREFIX = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:active:"
ACTIVE_TTL_SECONDS = int(getattr(settings, "TRACELENS_DEPLOYMENT_ACTIVE_TTL", 24 * 3600))
ACTIVE_INDEX_TTL_SECONDS = int(getattr(settings, "TRACELENS_DEPLOYMENT_ACTIVE_INDEX_TTL", 3600))
RUNNER_KEY_PREFIX = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:runner:"
RUNNER_TTL_SECONDS = max(5, int(getattr(settings, "TRACELENS_DEPLOYMENT_RUNNER_TTL", 8)))
SCHEDULER_LOCK_PREFIX = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:scheduler-env:"
SCHEDULER_TICK_KEY = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:scheduler-tick"
WORKER_HEARTBEAT_KEY = f"tracelens:{RedisLogStore.CACHE_SCHEMA}:deployment:worker-heartbeat"
WORKER_HEARTBEAT_TTL_SECONDS = max(5, int(getattr(settings, "TRACELENS_DEPLOYMENT_WORKER_HEARTBEAT_TTL", 8)))


def _active_key(environment_id: int) -> str:
    return f"{ACTIVE_KEY_PREFIX}{int(environment_id)}"


def _runner_key(deployment_id: int) -> str:
    return f"{RUNNER_KEY_PREFIX}{int(deployment_id)}"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _display_command(value: Any) -> str:
    command = str(value or "")
    prefix = "source ~/.bashrc && "
    while command.startswith(prefix):
        command = command[len(prefix):]
    return command


def _step_state_payload(deployment: EnvironmentDeployment) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for step in deployment.steps.all():
        payload.append({
            "id": step.id,
            "key": step.key,
            "name": step.name,
            "sort_order": step.sort_order,
            "status": step.status,
            "status_label": step.get_status_display(),
            "command": _display_command(step.command),
            "success_marker": step.success_marker,
            "stdout_length": len(step.stdout or ""),
            "stderr_length": len(step.stderr or ""),
            "process_log": step.process_log or "",
            "exit_status": step.exit_status,
            "retry_count": step.retry_count,
            "message": step.message,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "finished_at": step.finished_at.isoformat() if step.finished_at else None,
            "created_at": step.created_at.isoformat() if step.created_at else None,
            "updated_at": step.updated_at.isoformat() if step.updated_at else None,
        })
    return payload


class DeploymentEventBus:
    """Redis-backed realtime layer for deployment state and log deltas.

    SQLite remains authoritative. Redis is used only for active-task hot state and
    transient Pub/Sub notifications, so a Redis restart never destroys deployment
    history or the ability to rebuild the current snapshot from the database.
    """

    @classmethod
    def enabled(cls) -> bool:
        return RedisLogStore.enabled()

    @classmethod
    def _client(cls):
        return RedisLogStore._get_client()  # Reuse the project's authenticated Redis pool.


    @classmethod
    def refresh_worker_heartbeat(cls, worker_id: str) -> bool:
        client = cls._client()
        if client is None:
            return False
        try:
            client.setex(
                WORKER_HEARTBEAT_KEY,
                WORKER_HEARTBEAT_TTL_SECONDS,
                str(worker_id).encode("utf-8"),
            )
            return True
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return False

    @classmethod
    def worker_alive(cls) -> bool | None:
        """Return dedicated deployment-worker health when Redis is available."""
        client = cls._client()
        if client is None:
            return None
        try:
            return client.get(WORKER_HEARTBEAT_KEY) is not None
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def clear_worker_heartbeat(cls, worker_id: str) -> None:
        client = cls._client()
        if client is None:
            return
        expected = str(worker_id).encode("utf-8")
        try:
            if client.get(WORKER_HEARTBEAT_KEY) == expected:
                client.delete(WORKER_HEARTBEAT_KEY)
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def try_scheduler_tick_lock(cls, token: str, ttl_seconds: int = 4) -> bool | None:
        client = cls._client()
        if client is None:
            return None
        try:
            return bool(client.set(SCHEDULER_TICK_KEY, str(token).encode("utf-8"), nx=True, ex=max(2, int(ttl_seconds))))
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def release_scheduler_tick_lock(cls, token: str) -> None:
        client = cls._client()
        if client is None:
            return
        expected = str(token).encode("utf-8")
        try:
            if client.get(SCHEDULER_TICK_KEY) == expected:
                client.delete(SCHEDULER_TICK_KEY)
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def try_schedule_lock(cls, environment_id: int, token: str, ttl_seconds: int = 8) -> bool | None:
        client = cls._client()
        if client is None:
            return None
        try:
            return bool(client.set(
                f"{SCHEDULER_LOCK_PREFIX}{int(environment_id)}",
                str(token).encode("utf-8"),
                nx=True,
                ex=max(3, int(ttl_seconds)),
            ))
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def release_schedule_lock(cls, environment_id: int, token: str) -> None:
        client = cls._client()
        if client is None:
            return
        key = f"{SCHEDULER_LOCK_PREFIX}{int(environment_id)}"
        expected = str(token).encode("utf-8")
        try:
            if client.get(key) == expected:
                client.delete(key)
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def try_claim_runner(cls, deployment_id: int, token: str) -> bool | None:
        """Atomically claim one deployment for a dedicated worker.

        True means this worker owns the runner lease, False means another worker is
        already executing the deployment, and None means Redis is unavailable.
        """
        client = cls._client()
        if client is None:
            return None
        try:
            return bool(client.set(
                _runner_key(deployment_id),
                str(token).encode("utf-8"),
                nx=True,
                ex=RUNNER_TTL_SECONDS,
            ))
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def refresh_runner_lease(cls, deployment_id: int, token: str) -> bool:
        """Refresh the short-lived executor heartbeat for one deployment task."""
        client = cls._client()
        if client is None:
            return False
        try:
            client.setex(_runner_key(deployment_id), RUNNER_TTL_SECONDS, str(token).encode("utf-8"))
            return True
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return False

    @classmethod
    def runner_alive(cls, deployment_id: int) -> bool | None:
        """Return True/False for the executor lease, or None when Redis is unavailable."""
        client = cls._client()
        if client is None:
            return None
        try:
            return client.get(_runner_key(deployment_id)) is not None
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def clear_runner_lease(cls, deployment_id: int, token: str) -> None:
        client = cls._client()
        if client is None:
            return
        key = _runner_key(deployment_id)
        expected = str(token).encode("utf-8")
        try:
            current = client.get(key)
            if current == expected:
                client.delete(key)
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def _publish(cls, payload: dict[str, Any]) -> bool:
        client = cls._client()
        if client is None:
            return False
        try:
            client.publish(EVENT_CHANNEL, _json_bytes(payload))
            return True
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return False

    @classmethod
    def _cache_summary(cls, summary: dict[str, Any]) -> None:
        client = cls._client()
        if client is None:
            return
        environment_id = int(summary["environment"])
        try:
            active_statuses = {DeploymentStatus.PENDING, DeploymentStatus.RUNNING, DeploymentStatus.STOPPING, "pending", "running", "stopping"}
            status = summary.get("status")
            if status in active_statuses:
                pipe = client.pipeline(transaction=False)
                pipe.setex(_active_key(environment_id), ACTIVE_TTL_SECONDS, _json_bytes(summary))
                pipe.sadd(ACTIVE_INDEX_KEY, environment_id)
                pipe.expire(ACTIVE_INDEX_KEY, ACTIVE_INDEX_TTL_SECONDS)
                pipe.setex(ACTIVE_READY_KEY, ACTIVE_INDEX_TTL_SECONDS, b"1")
                pipe.execute()
                return
            if status in {DeploymentStatus.SCHEDULED, "scheduled"}:
                # A future task can coexist with a currently running deployment. It must
                # not overwrite or clear the environment's active deployment cache.
                return
            raw_current = client.get(_active_key(environment_id))
            if raw_current is not None:
                try:
                    current = json.loads(raw_current.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    current = {}
                if int(current.get("id") or 0) != int(summary.get("id") or 0):
                    return
            pipe = client.pipeline(transaction=False)
            pipe.delete(_active_key(environment_id))
            pipe.srem(ACTIVE_INDEX_KEY, environment_id)
            pipe.setex(ACTIVE_READY_KEY, ACTIVE_INDEX_TTL_SECONDS, b"1")
            pipe.execute()
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def publish_state(cls, deployment_id: int) -> bool:
        if not cls.enabled():
            return False
        try:
            deployment = EnvironmentDeployment.objects.prefetch_related("steps").get(pk=deployment_id)
            summary = dict(EnvironmentDeploymentSummarySerializer(deployment).data)
            cls._cache_summary(summary)
            return cls._publish({
                "type": "deployment.state",
                "deployment": summary,
                "steps": _step_state_payload(deployment),
            })
        except EnvironmentDeployment.DoesNotExist:
            return False
        except Exception:
            logger.exception("deployment realtime state publish failed deployment=%s", deployment_id)
            return False

    @classmethod
    def publish_log(
        cls,
        deployment_id: int,
        step_key: str,
        stdout: str = "",
        stderr: str = "",
        *,
        chunks: list[dict[str, str]] | None = None,
        stdout_length: int | None = None,
        stderr_length: int | None = None,
        process_log_length: int | None = None,
    ) -> bool:
        if not stdout and not stderr:
            return False
        return cls._publish({
            "type": "deployment.log",
            "deployment_id": int(deployment_id),
            "step_key": str(step_key),
            "stdout": stdout,
            "stderr": stderr,
            "chunks": chunks or [],
            "process_log_length": process_log_length,
            "stdout_length": stdout_length,
            "stderr_length": stderr_length,
        })

    @classmethod
    def warm_active_index(cls, deployments: Iterable[EnvironmentDeployment]) -> None:
        if not cls.enabled():
            return
        client = cls._client()
        if client is None:
            return
        try:
            deployments = list(deployments)
            summaries = [dict(EnvironmentDeploymentSummarySerializer(item).data) for item in deployments]
            pipe = client.pipeline(transaction=False)
            pipe.delete(ACTIVE_INDEX_KEY)
            for summary in summaries:
                environment_id = int(summary["environment"])
                pipe.setex(_active_key(environment_id), ACTIVE_TTL_SECONDS, _json_bytes(summary))
                pipe.sadd(ACTIVE_INDEX_KEY, environment_id)
            if summaries:
                pipe.expire(ACTIVE_INDEX_KEY, ACTIVE_INDEX_TTL_SECONDS)
            pipe.setex(ACTIVE_READY_KEY, ACTIVE_INDEX_TTL_SECONDS, b"1")
            pipe.execute()
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)

    @classmethod
    def active_summaries(cls, allowed_environment_ids: set[int] | None = None) -> list[dict[str, Any]] | None:
        """Return cached active summaries, or None when the cache is not warmed/available."""
        if not cls.enabled():
            return None
        client = cls._client()
        if client is None:
            return None
        try:
            if client.get(ACTIVE_READY_KEY) is None:
                return None
            raw_ids = client.smembers(ACTIVE_INDEX_KEY)
            environment_ids = sorted(int(value.decode("utf-8") if isinstance(value, bytes) else value) for value in raw_ids)
            if allowed_environment_ids is not None:
                environment_ids = [item for item in environment_ids if item in allowed_environment_ids]
            if not environment_ids:
                return []
            values = client.mget([_active_key(item) for item in environment_ids])
            result: list[dict[str, Any]] = []
            stale_ids: list[int] = []
            for environment_id, raw in zip(environment_ids, values):
                if raw is None:
                    stale_ids.append(environment_id)
                    continue
                try:
                    value = json.loads(raw.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    stale_ids.append(environment_id)
                    continue
                if isinstance(value, dict):
                    result.append(value)
            if stale_ids:
                client.srem(ACTIVE_INDEX_KEY, *stale_ids)
            return result
        except Exception as exc:
            RedisLogStore._mark_unavailable(exc)
            return None

    @classmethod
    def _async_client(cls):
        try:
            import redis.asyncio as async_redis
        except ImportError as exc:  # pragma: no cover - redis is already a project dependency.
            raise RuntimeError("Python redis asyncio support is unavailable") from exc

        common: dict[str, Any] = {
            "socket_connect_timeout": getattr(settings, "TRACELENS_REDIS_CONNECT_TIMEOUT", 1.0),
            "socket_timeout": None,
            "decode_responses": False,
        }
        username = str(getattr(settings, "TRACELENS_REDIS_USERNAME", "") or "").strip() or None
        password = str(getattr(settings, "TRACELENS_REDIS_PASSWORD", "") or "") or None
        url = RedisLogStore._normalized_url()
        if url:
            if username is not None:
                common["username"] = username
            if password is not None:
                common["password"] = password
            return async_redis.Redis.from_url(url, **common)
        return async_redis.Redis(
            host=getattr(settings, "TRACELENS_REDIS_HOST", "127.0.0.1"),
            port=int(getattr(settings, "TRACELENS_REDIS_PORT", 6379)),
            db=int(getattr(settings, "TRACELENS_REDIS_DB", 0)),
            username=username,
            password=password,
            **common,
        )

    @classmethod
    async def event_stream(cls) -> AsyncIterator[bytes]:
        """Yield SSE frames from Redis Pub/Sub with keepalives for proxies."""
        if not cls.enabled():
            yield b'event: realtime-unavailable\ndata: {"reason":"redis-disabled"}\n\n'
            return

        client = cls._async_client()
        pubsub = client.pubsub()
        try:
            await client.ping()
            await pubsub.subscribe(EVENT_CHANNEL)
            yield b'event: realtime-ready\ndata: {"transport":"redis-pubsub"}\n\n'
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if message is None:
                    yield b": keepalive\n\n"
                    await asyncio.sleep(0)
                    continue
                raw = message.get("data")
                if isinstance(raw, str):
                    data = raw.encode("utf-8")
                elif isinstance(raw, bytes):
                    data = raw
                else:
                    data = _json_bytes({"type": "deployment.unknown", "data": raw})
                yield b"event: deployment\ndata: " + data + b"\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("deployment realtime stream unavailable: %s", exc)
            payload = _json_bytes({"reason": f"{exc.__class__.__name__}: {exc}"})
            yield b"event: realtime-unavailable\ndata: " + payload + b"\n\n"
        finally:
            try:
                await pubsub.unsubscribe(EVENT_CHANNEL)
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass
