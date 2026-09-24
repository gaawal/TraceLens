"""Server-side log watcher: evaluates the user's tagged rules against live logs.

Why this exists at all: the browser's live view is torn down when the tab closes
(``App.tsx`` deliberately aborts the stream), and each open tab holds its own SSH
``tail -F``. Monitoring therefore stopped when nobody was looking, and three viewers of one
module meant three remote tails. This worker is the piece that makes monitoring survive,
and shares one tail across every watch on the same target.

Two layers:

* ``TargetHub`` — exactly one SSH tail per ``(environment, target)``, fanning lines out to
  any number of subscribers. This is the scaling fix.
* ``WatchRunner`` — one per watch, consuming from the hubs it needs, evaluating the watch's
  compiled rule deterministically, and recording hits.

Matching is intentionally **not** an LLM call: evaluating every line with a model would be
slow, expensive and non-deterministic. The model's job is authoring rules and explaining
hits, not streaming.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from django.db import close_old_connections
from django.utils import timezone

from apps.logsources.models import LogWatch, LogWatchHit, LogWatchLevel, LogWatchTriggerKind
from apps.logsources.services.watch_events import WatchEventBus
from apps.logsources.services.watch_rules import CompiledRule, rules_for_watch

logger = logging.getLogger("tracelens.logsources.watch_worker")
process_logger = logging.getLogger("tracelens.process.watch")

# Recent lines kept per hub so a hit can include the run-up to the trigger.
TAIL_BUFFER_LINES = 200
# Hard ceiling on how long a single hub may go without any bytes before the runner treats
# it as a stalled link (silence triggers depend on this being bounded).
HUB_IDLE_TIMEOUT_SECONDS = 120


@dataclass
class TailLine:
    text: str
    machine: str = ""
    subsystem: str = ""
    fm: str = ""
    source_path: str = ""
    source_category: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "machine": self.machine,
            "subsystem": self.subsystem,
            "fm": self.fm,
            "source_path": self.source_path,
            "source_category": self.source_category,
        }


class TargetHub:
    """One SSH tail per (environment, target), fanned out to N subscribers."""

    _lock = threading.RLock()
    _hubs: dict[str, "TargetHub"] = {}

    def __init__(self, key: str, environment_id: int, target: dict[str, Any], categories: list[str]) -> None:
        self.key = key
        self.environment_id = environment_id
        self.target = dict(target or {})
        self.categories = list(categories or [])
        self._subscribers: set[queue.Queue] = set()
        self._recent: list[TailLine] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_line_at = time.monotonic()
        self.error = ""

    # ------------------------------------------------------------- registry
    @classmethod
    def acquire(cls, environment_id: int, target: dict[str, Any], categories: Iterable[str]) -> "TargetHub":
        key = cls.key_for(environment_id, target, categories)
        with cls._lock:
            hub = cls._hubs.get(key)
            if hub is None:
                hub = cls(key, environment_id, target, categories)
                cls._hubs[key] = hub
                hub.start()
                process_logger.info("[WATCH] tail.start target=%s environment=%s", hub.target, environment_id)
            return hub

    @classmethod
    def release(cls, hub: "TargetHub") -> None:
        """Stop the tail once nothing needs it, so SSH sessions do not accumulate."""
        with cls._lock:
            if hub._subscribers:
                return
            cls._hubs.pop(hub.key, None)
        hub.stop()
        process_logger.info("[WATCH] tail.stop target=%s environment=%s", hub.target, hub.environment_id)

    @classmethod
    def active_count(cls) -> int:
        with cls._lock:
            return len(cls._hubs)

    @staticmethod
    def key_for(environment_id: int, target: dict[str, Any], categories: Iterable[str]) -> str:
        subsystem = str((target or {}).get("subsystem") or "").strip()
        fm = str((target or {}).get("fm") or "").strip()
        kind = str((target or {}).get("kind") or "normal").strip()
        cats = ",".join(sorted(str(item) for item in (categories or []) if str(item)))
        return f"{environment_id}|{subsystem}|{fm}|{kind}|{cats}"

    # ----------------------------------------------------------- pub/sub local
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def recent(self, count: int) -> list[TailLine]:
        with self._lock:
            return list(self._recent[-max(0, count):]) if count else []

    def _fan_out(self, line: TailLine) -> None:
        with self._lock:
            self._recent.append(line)
            if len(self._recent) > TAIL_BUFFER_LINES:
                del self._recent[: len(self._recent) - TAIL_BUFFER_LINES]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(line)
            except queue.Full:
                # A stalled subscriber must never stall the shared tail.
                logger.warning("watch.hub.subscriber_overflow key=%s", self.key)

    # ---------------------------------------------------------------- producer
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"watch-tail-{self.key[:40]}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from apps.environments.models import Environment
        from apps.logsources.services.remote_logs import build_live_log_plan, stream_live_log_events

        while not self._stop.is_set():
            close_old_connections()
            try:
                environment = Environment.objects.filter(pk=self.environment_id).first()
                if environment is None:
                    self.error = "环境不存在"
                    return
                plan = build_live_log_plan(
                    environment,
                    [str(self.target.get("subsystem") or "")],
                    [str(self.target.get("fm") or "")],
                    self.categories,
                    fm_targets=[self.target],
                )
                if not plan:
                    self.error = "没有可实时监听的 current 日志文件"
                    self._stop.wait(20)
                    continue
                self.error = ""
                for payload in stream_live_log_events(environment, plan):
                    if self._stop.is_set():
                        return
                    self.last_line_at = time.monotonic()
                    for line in self._split_payload(payload):
                        self._fan_out(line)
            except Exception as exc:  # noqa: BLE001 - a broken target must not kill the worker
                self.error = str(exc)[:400]
                logger.warning("watch.hub.failed key=%s error=%s", self.key, exc)
                self._stop.wait(15)

    def _split_payload(self, payload: dict[str, Any]) -> list[TailLine]:
        """Turn one live-log frame into individual lines, carrying its provenance."""
        if not isinstance(payload, dict):
            return []
        if str(payload.get("type") or "") != "logs":
            return []
        lines: list[TailLine] = []
        for chunk in payload.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("text") or "")
            machine = str(chunk.get("machine_name") or "").strip()
            subsystem = str(chunk.get("subsystem") or "").strip()
            fm = str(chunk.get("fm") or "").strip()
            path = str(chunk.get("path") or "").strip()
            category = str(chunk.get("source_category") or "").strip()
            for raw in text.splitlines():
                if raw.strip():
                    lines.append(TailLine(raw, machine, subsystem, fm, path, category))
        return lines


@dataclass
class _WatchState:
    """Per-watch mutable bookkeeping that does not belong in the database."""

    compiled: list[CompiledRule] = field(default_factory=list)
    rule_problems: list[dict[str, str]] = field(default_factory=list)
    burst_times: list[float] = field(default_factory=list)
    last_fire_at: float = 0.0
    last_seen_match_at: float = 0.0
    quota_window_start: float = field(default_factory=time.monotonic)
    quota_used: int = 0


class WatchRunner:
    """Evaluates one watch against the shared hubs."""

    def __init__(self, watch: LogWatch, token: str) -> None:
        self.watch_id = watch.pk
        self.token = token
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = _WatchState()
        self._hub_queues: list[tuple[TargetHub, queue.Queue]] = []
        self.watch_name = watch.name
        # Silence detection measures from when the watch actually started running, so a
        # freshly enabled watch is not reported as silent before it has had a chance to see
        # anything.
        self._start_monotonic = time.monotonic()

    # ------------------------------------------------------------------ lifecycle
    def start(self, watch: LogWatch) -> None:
        self._reload_rules(watch)
        self._attach(watch)
        self._thread = threading.Thread(target=self._run, name=f"watch-runner-{self.watch_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for hub, q in self._hub_queues:
            hub.unsubscribe(q)
            TargetHub.release(hub)
        self._hub_queues = []

    # --------------------------------------------------------------------- setup
    def _reload_rules(self, watch: LogWatch) -> None:
        """Compile from the shared rule set — the watcher never owns a rule copy."""
        from apps.environments.models import ResourceSettings
        from apps.logsources.services.watch_rules import extraction_rules_for_watch

        settings_obj = ResourceSettings.get_solo()
        self._state.rule_problems = []
        if watch.extraction_rule_id:
            # 实时采集: the watch exists to feed one data extractor.
            self._state.compiled = extraction_rules_for_watch(
                settings_obj.data_extraction_rules or [], watch.extraction_rule_id
            )
            if not self._state.compiled:
                reason = f"数据提取器 {watch.extraction_rule_id} 不存在、已停用或缺少 Match 关键字"
                self._state.rule_problems = [{"rule_id": watch.extraction_rule_id, "reason": reason}]
                logger.warning("watch.no_extraction_rule watch=%s reason=%s", self.watch_id, reason)
            return
        self._state.compiled = rules_for_watch(settings_obj.display_rules or [], watch.source_rule_id)
        if not self._state.compiled:
            reason = (
                f"语义规则 {watch.source_rule_id} 不存在或未启用"
                if watch.source_rule_id else "没有可用的语义规则"
            )
            self._state.rule_problems = [{"rule_id": watch.source_rule_id or "-", "reason": reason}]
            logger.warning("watch.no_rule watch=%s reason=%s", self.watch_id, reason)

    def _attach(self, watch: LogWatch) -> None:
        targets = [item for item in (watch.targets or []) if isinstance(item, dict)]
        for target in targets:
            hub = TargetHub.acquire(watch.environment_id, target, watch.source_categories or [])
            self._hub_queues.append((hub, hub.subscribe()))

    # ------------------------------------------------------------------- main loop
    def _run(self) -> None:
        silence_deadline = time.monotonic()
        while not self._stop.is_set():
            close_old_connections()
            try:
                watch = LogWatch.objects.filter(pk=self.watch_id, enabled=True).first()
                if watch is None:
                    return
                if not WatchEventBus.refresh_lease(self.watch_id, self.token):
                    return  # lost ownership
                if watch.trigger_kind == LogWatchTriggerKind.SILENCE:
                    silence_deadline = self._check_silence(watch, silence_deadline)
                self._drain(watch)
            except Exception:  # noqa: BLE001
                logger.exception("watch.runner.loop_failed watch=%s", self.watch_id)
            self._stop.wait(0.5)

    def _drain(self, watch: LogWatch) -> None:
        for hub, q in self._hub_queues:
            while True:
                try:
                    line = q.get_nowait()
                except queue.Empty:
                    break
                self._evaluate_line(watch, line)

    # ---------------------------------------------------------------- evaluation
    def _evaluate_line(self, watch: LogWatch, line: TailLine) -> None:
        for rule in self._state.compiled:
            if not rule.matches(line.text):
                continue
            self._state.last_seen_match_at = time.monotonic()
            kind = watch.trigger_kind or LogWatchTriggerKind.APPEAR
            if kind == LogWatchTriggerKind.BURST:
                self._on_burst(watch, line, rule)
            elif kind != LogWatchTriggerKind.SILENCE:
                self._fire(watch, line, rule)
            return

    def _on_burst(self, watch: LogWatch, line: TailLine, rule: CompiledRule) -> None:
        window = max(1, int((watch.trigger_config or {}).get("within_seconds") or 60))
        threshold = max(1, int((watch.trigger_config or {}).get("count") or 5))
        now = time.monotonic()
        self._state.burst_times = [t for t in self._state.burst_times if now - t <= window]
        self._state.burst_times.append(now)
        if len(self._state.burst_times) >= threshold:
            # Fire once per window: the counter resets so a sustained storm does not
            # produce one hit per line.
            fired = len(self._state.burst_times)
            self._state.burst_times = []
            self._fire(watch, line, rule, extra_detail={"burst_count": fired, "within_seconds": window})

    def _check_silence(self, watch: LogWatch, deadline: float) -> float:
        """Fire when the expected log stops arriving.

        This is the signal line matching cannot express: 'the heartbeat log is gone' is
        usually a worse failure than 'an error appeared'.
        """
        now = time.monotonic()
        window = max(10, int((watch.trigger_config or {}).get("silence_seconds") or 300))
        seen = self._state.last_seen_match_at or 0.0
        reference = max(seen, self._started_at())
        if now - reference >= window and now >= deadline:
            synthetic = TailLine(
                text=f"[silence] {watch.name}：{window} 秒内没有出现预期日志",
                subsystem=self._first_target_value("subsystem"),
                fm=self._first_target_value("fm"),
            )
            rule = self._state.compiled[0] if self._state.compiled else None
            if rule is not None:
                self._fire(watch, synthetic, rule, extra_detail={"silence_seconds": window})
            return now + window
        return deadline

    def _started_at(self) -> float:
        return self._start_monotonic

    def _first_target_value(self, key: str) -> str:
        for hub, _ in self._hub_queues:
            value = str(hub.target.get(key) or "").strip()
            if value:
                return value
        return ""

    # ----------------------------------------------------------------- hit record
    def _fire(
        self,
        watch: LogWatch,
        line: TailLine,
        rule: CompiledRule,
        *,
        extra_detail: dict[str, Any] | None = None,
    ) -> None:
        from apps.logsources.services.watch_rules import normalize_signature

        capture = dict(watch.capture_config or {})
        cooldown = max(0, int(capture.get("cooldown_seconds") or 0))
        now = time.monotonic()
        if cooldown and now - self._state.last_fire_at < cooldown:
            self._bump_dropped(watch)
            return

        quota = int(capture.get("hourly_quota") or 0)
        if quota:
            if now - self._state.quota_window_start >= 3600:
                self._state.quota_window_start = now
                self._state.quota_used = 0
            if self._state.quota_used >= quota:
                self._bump_dropped(watch)
                return

        signature = normalize_signature(line.text)
        before = max(0, int(capture.get("before_lines") or 0))
        window_lines: list[dict[str, Any]] = []
        if before:
            for hub, _ in self._hub_queues:
                if hub.target.get("subsystem") == line.subsystem or not line.subsystem:
                    window_lines = [item.as_dict() for item in hub.recent(before)]
                    break
        window_lines.append(line.as_dict())

        seq = WatchEventBus.next_seq(self.watch_id)
        matched_at = timezone.now()
        dedup_key = LogWatchHit.build_dedup_key(self.watch_id, signature, matched_at)
        try:
            hit = LogWatchHit.objects.create(
                watch=watch,
                seq=seq,
                matched_at=matched_at,
                signature=signature,
                level=_level_of(line.text),
                machine=line.machine,
                subsystem=line.subsystem,
                fm=line.fm,
                source_path=line.source_path,
                source_category=line.source_category,
                line_text=line.text[:8000],
                window_lines=window_lines[-max(1, before) - 1:],
                extracted={},
                dedup_key=dedup_key,
            )
        except Exception:  # noqa: BLE001 - a duplicate must not stop the watch
            logger.debug("watch.hit.duplicate watch=%s signature=%s", self.watch_id, signature)
            return

        self._state.last_fire_at = now
        self._state.quota_used += 1
        LogWatch.objects.filter(pk=self.watch_id).update(
            hit_count=(watch.hit_count or 0) + 1,
            last_hit_at=matched_at,
            last_heartbeat_at=matched_at,
        )

        WatchEventBus.publish_hit({
            "type": "watch.hit",
            "watch_id": self.watch_id,
            "watch_name": watch.name,
            "environment_id": watch.environment_id,
            "level": watch.level,
            "seq": hit.seq,
            "hit_id": hit.pk,
            "matched_at": matched_at.isoformat(),
            "signature": signature,
            "label": rule.label_template or rule.name,
            "label_color": rule.label_color,
            "display_text": rule.display_template or rule.name,
            # Which surface this hit belongs to. 实时采集 hits carry real values for the
            # collector panel and must not appear as symptom lanes on the timeline, so the
            # decision travels with the event instead of being re-derived (wrongly) per client.
            "display_mode": rule.display_mode,
            "show_on_timeline": bool(rule.show_on_timeline),
            "subsystem": hit.subsystem,
            "fm": hit.fm,
            "machine": hit.machine,
            "line_text": hit.line_text[:600],
            **(extra_detail or {}),
        })
        process_logger.info(
            "[WATCH] hit watch=%s seq=%s signature=%s", self.watch_id, hit.seq, signature[:80]
        )

    def _bump_dropped(self, watch: LogWatch) -> None:
        LogWatch.objects.filter(pk=self.watch_id).update(dropped_count=(watch.dropped_count or 0) + 1)


def _level_of(text: str) -> str:
    for level in ("FATAL", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
        if f"[{level}]" in text:
            return level
    return ""


class WatchSupervisor:
    """Reconciles desired watches with running runners."""

    def __init__(self, token: str, poll_seconds: float = 2.0) -> None:
        self.token = token
        self.poll_seconds = max(0.5, poll_seconds)
        self.runners: dict[int, WatchRunner] = {}
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()
        for watch_id in list(self.runners):
            self.runners.pop(watch_id).stop()
            # Hand the lease back immediately. Without this a clean restart waits out the
            # full lease TTL before the new process can adopt its own watches.
            WatchEventBus.release_lease(watch_id, self.token)
        self.runners.clear()

    def run_forever(self) -> None:
        while not self._stopping.is_set():
            close_old_connections()
            try:
                self._reconcile()
            except Exception:  # noqa: BLE001
                logger.exception("watch.supervisor.reconcile_failed")
            self._stopping.wait(self.poll_seconds)

    def _reconcile(self) -> None:
        desired = {
            watch.pk: watch
            for watch in LogWatch.objects.filter(enabled=True).select_related("environment")
        }

        # Drop runners whose watch was disabled or deleted.
        for watch_id in list(self.runners):
            if watch_id not in desired:
                self.runners.pop(watch_id).stop()
                WatchEventBus.release_lease(watch_id, self.token)

        for watch_id, watch in desired.items():
            if watch_id in self.runners:
                runner = self.runners[watch_id]
                runner.watch_name = watch.name
                continue
            claim = WatchEventBus.try_claim_watch(watch_id, self.token)
            if claim is None:
                # No Redis: a single-worker deployment still works, but two workers would
                # double-record. Refuse rather than risk duplicate hits.
                self._record_error(watch, "Redis 不可用，无法获取监控租约；请先恢复 Redis。")
                continue
            if not claim:
                # Another worker owns it; if that worker died the lease will expire on its own.
                continue
            runner = WatchRunner(watch, self.token)
            try:
                runner.start(watch)
            except Exception as exc:  # noqa: BLE001
                WatchEventBus.release_lease(watch_id, self.token)
                self._record_error(watch, f"启动监控失败：{exc}")
                logger.exception("watch.runner.start_failed watch=%s", watch_id)
                continue
            self.runners[watch_id] = runner

        # Stop tails that lost their last subscriber, so SSH sessions do not leak.
        orphan_hub_sweep()
        log_orphan_hubs()

    def _record_error(self, watch: LogWatch, message: str) -> None:
        try:
            LogWatch.objects.filter(pk=watch.pk).update(last_error=message[:2000])
        except Exception:  # noqa: BLE001
            pass


def log_orphan_hubs() -> None:
    """Log the shared-tail count so an operator can see SSH pressure at a glance."""
    count = TargetHub.active_count()
    if count:
        logger.debug("watch.hub.active count=%s", count)


def watch_worker_heartbeat() -> dict[str, Any]:
    return {"active_hubs": TargetHub.active_count(), "at": timezone.now().isoformat()}


def orphan_hub_sweep() -> None:
    """Release hubs with no subscribers (runners stopped while a hub lingered)."""
    with TargetHub._lock:
        orphans = [hub for hub in TargetHub._hubs.values() if not hub._subscribers]
    for hub in orphans:
        TargetHub.release(hub)
