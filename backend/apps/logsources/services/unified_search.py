from __future__ import annotations

import hashlib
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Iterable, Iterator

from apps.logsources.services.archive_selector import LogArtifact, parse_log_name, select_artifacts_for_window

logger = logging.getLogger("tracelens.unified_log_search")

_URL_TIMEOUT_SECONDS = 20
# Even a 1-8 MiB log can contain hours of data.  When the caller supplied an
# exact time range, prefer Range + timestamp seek for anything beyond a small
# probe-sized file instead of downloading the entire object first.
_URL_SMALL_FILE_BYTES = 384 * 1024
_URL_FULL_READ_LIMIT = 16 * 1024 * 1024
_URL_MAX_SCAN_BYTES = 64 * 1024 * 1024
_URL_PROBE_BYTES = 256 * 1024
_URL_CHUNK_BYTES = 1024 * 1024
_URL_SEEK_MAX_PROBES = 20
_URL_SEEK_SAFETY_BYTES = 128 * 1024
_TIMESTAMP_RE = re.compile(rb"\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)\]")
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))




class _NoopJsonStore:
    @staticmethod
    def get_json(_key: str):
        return None

    @staticmethod
    def set_json(_key: str, _value, _ttl_seconds: int) -> bool:
        return False


def _redis_store():
    # Reuse the same cache backend as environment log positioning. Keeping the
    # import lazy preserves parser-only testability and command-line unit tests
    # that intentionally run without Django installed.
    try:
        from apps.logsources.services.redis_store import RedisLogStore
        return RedisLogStore
    except (ImportError, ModuleNotFoundError):
        return _NoopJsonStore


def _dt_dump(value: datetime | None) -> str:
    return value.isoformat(timespec="microseconds") if value else ""


def _dt_load(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _progress_store():
    # Keep the transport/time-window engine importable in pure parser tests.
    # Django/Redis progress is only needed when a web search operation exists.
    from apps.logsources.services.search_progress import LogSearchProgressStore
    return LogSearchProgressStore


class UnifiedLogSearchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UrlLogArtifact:
    """A log artifact exposed by an HTTP/HTTPS directory source.

    ATLog only resolves case-specific paths into these artifacts. Time-window
    locating and reading are owned by the shared log-search layer so case
    analysis and environment log positioning do not maintain separate readers.
    """

    url: str
    source_path: str
    source_category: str
    subsystem: str
    fm: str

    @property
    def identity(self) -> str:
        return f"url:{self.url}"

    @property
    def display_name(self) -> str:
        return PurePosixPath(self.source_path or self.url).name or self.source_path or self.url


@dataclass(frozen=True, slots=True)
class UrlWindowResult:
    text: str
    truncated: bool
    mode: str
    size: int | None
    start_offset: int = 0
    scanned_bytes: int = 0


def _parse_timestamp_bytes(value: bytes) -> datetime | None:
    match = _TIMESTAMP_RE.search(value)
    if not match:
        return None
    text = match.group(1).decode("ascii", errors="ignore").replace("T", " ")
    if "." in text:
        head, fraction = text.split(".", 1)
        text = f"{head}.{fraction[:6].ljust(6, '0')}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _decode(content: bytes) -> str:
    if not content:
        return ""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _request(url: str, *, method: str = "GET", start: int | None = None, end: int | None = None, max_bytes: int | None = None):
    headers = {"User-Agent": "TraceLens-LogSearch/1.0", "Accept": "*/*"}
    if start is not None:
        range_end = "" if end is None else str(max(start, end))
        headers["Range"] = f"bytes={max(0, start)}-{range_end}"
    request = urllib.request.Request(url=url, headers=headers, method=method)
    try:
        with _DIRECT_OPENER.open(request, timeout=_URL_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200) or 200)
            content_range = str(response.headers.get("Content-Range") or "")
            length_raw = str(response.headers.get("Content-Length") or "")
            content_length = int(length_raw) if length_raw.isdigit() else None
            if method == "HEAD":
                content = b""
            elif max_bytes is None:
                content = response.read()
            else:
                # If a server ignores Range, never accidentally load an unlimited
                # multi-gigabyte log into the Django process.
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    content = content[:max_bytes]
            return status, content, content_length, content_range
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(url) from exc
        raise UnifiedLogSearchError(f"读取日志链接失败 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise UnifiedLogSearchError(f"无法访问日志链接：{url} · {getattr(exc, 'reason', exc)}") from exc
    except TimeoutError as exc:
        raise UnifiedLogSearchError(f"读取日志链接超时：{url}") from exc


def _head_size(url: str) -> int | None:
    try:
        _, _, content_length, _ = _request(url, method="HEAD")
        if content_length is not None:
            return content_length
    except (UnifiedLogSearchError, FileNotFoundError):
        pass
    try:
        status, content, content_length, content_range = _request(url, start=0, end=0, max_bytes=1024)
        match = re.search(r"/(\d+)$", content_range)
        if match:
            return int(match.group(1))
        if status == 206 and content_length is not None:
            return content_length
        if status != 206 and content_length is not None:
            return content_length
        return len(content) or None
    except (UnifiedLogSearchError, FileNotFoundError):
        return None


def _fetch_range(url: str, start: int, end: int) -> tuple[bytes, bool]:
    expected = max(1, end - start + 1)
    status, content, _content_length, content_range = _request(
        url,
        start=max(0, start),
        end=max(start, end),
        max_bytes=expected + 1024,
    )
    return content, status == 206 or bool(content_range)


def _filter_window_lines(content: bytes, start: datetime, end: datetime) -> bytes:
    """Exact timestamp filter shared by URL-backed case logs.

    Untimestamped continuation lines are retained only while the current record
    is inside the requested interval. This matches the environment stream's
    contract: artifact selection can be approximate, final line filtering is exact.
    """

    output: list[bytes] = []
    entered = False
    for raw in content.splitlines():
        timestamp = _parse_timestamp_bytes(raw)
        if timestamp is not None:
            if timestamp < start:
                entered = False
                continue
            if timestamp > end:
                # Ordered logs cannot re-enter an earlier time window.  Stop at
                # the first record after ``end`` even when the requested window
                # is entirely before this file's first timestamp.
                break
            entered = True
        if entered:
            output.append(raw)
    return b"\n".join(output) + (b"\n" if output else b"")


def _probe_timestamp_range(content: bytes) -> tuple[datetime, datetime] | None:
    values: list[datetime] = []
    for raw in content.splitlines():
        timestamp = _parse_timestamp_bytes(raw)
        if timestamp is not None:
            values.append(timestamp)
    if not values:
        return None
    return min(values), max(values)


def _find_http_start_offset(url: str, size: int, target: datetime, operation_id: str = "-") -> tuple[int | None, bool]:
    if size < _URL_SMALL_FILE_BYTES:
        return None, True
    low = 0
    high = max(0, size - 1)
    best_before = 0
    ranged_supported = True
    successful_probes = 0
    for _ in range(_URL_SEEK_MAX_PROBES):
        if operation_id and operation_id != "-":
            _progress_store().raise_if_cancelled(operation_id)
        if high <= low + _URL_PROBE_BYTES:
            break
        midpoint = low + (high - low) // 2
        probe_start = max(0, midpoint - _URL_PROBE_BYTES // 2)
        probe_end = min(size - 1, probe_start + _URL_PROBE_BYTES - 1)
        content, ranged = _fetch_range(url, probe_start, probe_end)
        if not ranged:
            ranged_supported = False
            break
        timestamp_range = _probe_timestamp_range(content)
        if timestamp_range is None:
            low = min(size - 1, midpoint + _URL_PROBE_BYTES // 2)
            continue
        successful_probes += 1
        first, last = timestamp_range
        if target < first:
            high = midpoint
        elif target > last:
            best_before = max(best_before, probe_start)
            low = midpoint + 1
        else:
            best_before = probe_start
            break
    if not ranged_supported:
        return None, False
    if successful_probes == 0:
        return None, True
    return max(0, best_before - _URL_SEEK_SAFETY_BYTES), True


def select_url_log_names_for_window(
    names: Iterable[str],
    start: datetime | None,
    end: datetime | None,
    *,
    fm_hint: str = "",
    subsystem: str = "",
    source_category: str = "debug",
) -> list[str]:
    """Select URL-backed physical logs with the log-positioning selector.

    ATLog owns only URL/path discovery.  Once a directory has been resolved,
    physical file selection must follow exactly the same close-boundary/current
    semantics as environment log positioning.  ``fm_hint`` is important for
    executor trees: many ``<module>_cp_*`` physical names are one logical module
    stream and must not become dozens of independent current-file candidates.
    """
    values = [
        str(name) for name in names
        if str(name) and not str(name).endswith("/") and re.search(r"\.(?:log|txt|out)$", str(name), re.I)
    ]
    if start is None or end is None:
        return values

    reference = end or start
    artifacts: list[LogArtifact] = []
    for name in values:
        parsed = parse_log_name(name, reference) if name.lower().endswith(".log") else None
        if parsed:
            parsed_identity, boundary = parsed
        else:
            basename = PurePosixPath(name).name
            stem = re.sub(r"\.(?:txt|out)$", "", basename, flags=re.I)
            match = re.match(r"^(?P<identity>.+?)[_-](?P<stamp>\d[\dT_:\-.]*)$", stem)
            boundary = None
            parsed_identity = stem
            if match:
                from apps.logsources.services.archive_selector import parse_archive_timestamp
                parsed_boundary = parse_archive_timestamp(match.group("stamp"), reference)
                if parsed_boundary is not None:
                    parsed_identity, boundary = match.group("identity"), parsed_boundary
        logical_fm = str(fm_hint or parsed_identity or PurePosixPath(name).name).strip()
        artifacts.append(LogArtifact(
            machine_id=0,
            machine_name="url",
            subsystem=str(subsystem or "url"),
            fm=logical_fm,
            path=name,
            kind="archived" if boundary is not None else "current",
            boundary_time=boundary,
            source_category=source_category,
            source_name="ATLog",
        ))

    selected_paths = {item.path for item in select_artifacts_for_window(artifacts, start, end)}
    return [name for name in values if name in selected_paths]


def _url_index_key(artifact: UrlLogArtifact) -> str:
    digest = hashlib.sha256(artifact.url.encode("utf-8")).hexdigest()[:32]
    return f"tracelens:v2:url-log-index:{digest}"


def _first_last_timestamp(content: bytes) -> tuple[datetime | None, datetime | None]:
    first = None
    last = None
    lines = content.splitlines()
    for raw in lines:
        first = _parse_timestamp_bytes(raw)
        if first is not None:
            break
    for raw in reversed(lines):
        last = _parse_timestamp_bytes(raw)
        if last is not None:
            break
    return first, last


def probe_url_log_time_range(artifact: UrlLogArtifact, *, operation_id: str = "-") -> tuple[datetime | None, datetime | None, int | None]:
    """Index one URL log using the same head/tail idea as the SSH file index.

    Only small head/tail byte ranges are read. The result is cached by URL +
    content length, so repeated case searches do not reopen every current log.
    A missing timestamp is conservative and never causes the file to be dropped.
    """
    if operation_id and operation_id != "-":
        _progress_store().raise_if_cancelled(operation_id)
    size = _head_size(artifact.url)
    if operation_id and operation_id != "-":
        _progress_store().raise_if_cancelled(operation_id)
    key = _url_index_key(artifact)
    cached = _redis_store().get_json(key)
    if isinstance(cached, dict) and cached.get("size") == size:
        return _dt_load(cached.get("start_time")), _dt_load(cached.get("end_time")), size

    first = last = None
    probe_bytes = _URL_PROBE_BYTES
    if size is not None and size > 0:
        head_end = min(size - 1, probe_bytes - 1)
        head, head_ranged = _fetch_range(artifact.url, 0, head_end)
        if operation_id and operation_id != "-":
            _progress_store().raise_if_cancelled(operation_id)
        if head_ranged or size <= probe_bytes:
            first, head_last = _first_last_timestamp(head)
            last = head_last
        if size > probe_bytes:
            tail_start = max(0, size - probe_bytes)
            tail, tail_ranged = _fetch_range(artifact.url, tail_start, size - 1)
            if operation_id and operation_id != "-":
                _progress_store().raise_if_cancelled(operation_id)
            if tail_ranged:
                tail_first, tail_last = _first_last_timestamp(tail)
                first = first or tail_first
                last = tail_last or last
    elif size is None:
        # Unknown length: do not perform an unbounded read just to index it.
        first = last = None

    _redis_store().set_json(key, {
        "url": artifact.url,
        "size": size,
        "start_time": _dt_dump(first),
        "end_time": _dt_dump(last),
    }, 900)
    return first, last, size


def filter_url_artifacts_for_window(
    artifacts: Iterable[UrlLogArtifact],
    start: datetime | None,
    end: datetime | None,
    *,
    operation_id: str = "-",
) -> list[UrlLogArtifact]:
    """Drop URL artifacts whose real head/tail time range misses the query.

    Filename rotations are already pruned before this function. This second
    stage is specifically for boundary-less/current files, which otherwise all
    look relevant and can inflate a narrow case query to dozens of targets.
    """
    values = list(artifacts)
    if start is None or end is None:
        return values
    selected: list[UrlLogArtifact] = []
    reference = end or start
    for index, artifact in enumerate(values, start=1):
        if operation_id and operation_id != "-":
            _progress_store().raise_if_cancelled(operation_id)
            _progress_store().patch(
                operation_id,
                stage="indexing",
                current_file=artifact.source_path,
                current_action="正在复用日志定位时间索引筛选文件",
                message=f"正在按真实日志时间筛选候选 {index}/{len(values)}",
            )
        parsed = parse_log_name(artifact.display_name, reference) if artifact.display_name.lower().endswith(".log") else None
        boundary = parsed[1] if parsed else None
        # Timestamped rotations were selected by close-boundary semantics; avoid
        # redundant HTTP probes for them. Current/boundary-less files need the
        # same real first/last timestamp index used by environment positioning.
        if boundary is not None:
            selected.append(artifact)
            continue
        try:
            first, last, _size = probe_url_log_time_range(artifact, operation_id=operation_id)
        except (UnifiedLogSearchError, FileNotFoundError):
            # Be conservative on transport/index failures: keep the artifact and
            # let the exact range reader decide, never silently lose evidence.
            selected.append(artifact)
            continue
        if first is None or last is None or (first <= end and last >= start):
            selected.append(artifact)
    if operation_id and operation_id != "-":
        _progress_store().patch(
            operation_id,
            current_file="",
            selected_files=len(selected),
            message=f"时间索引筛选完成：{len(values)} 个候选中 {len(selected)} 个与时间范围相交",
        )
    return selected


def read_url_log_window(
    artifact: UrlLogArtifact,
    start: datetime | None,
    end: datetime | None,
    *,
    operation_id: str = "-",
) -> UrlWindowResult:
    """Read one URL artifact using the same search-layer time-window contract.

    For large ordered logs, HTTP Range + timestamp bisection skips the prefix and
    scans forward only around the requested interval. For small files or servers
    without Range support, a bounded fallback read is used and exact timestamp
    filtering is still applied.
    """

    if operation_id and operation_id != "-":
        _progress_store().raise_if_cancelled(operation_id)
        _progress_store().artifact_started(
            operation_id,
            identity=artifact.identity,
            display_name=artifact.display_name,
            full_path=artifact.url,
            subsystem=artifact.subsystem,
            module=artifact.fm,
        )

    try:
        size = _head_size(artifact.url)
        if operation_id and operation_id != "-":
            _progress_store().raise_if_cancelled(operation_id)
        if start is None or end is None:
            limit = _URL_FULL_READ_LIMIT if size is None else min(max(size, 1), _URL_FULL_READ_LIMIT)
            _status, content, _content_length, _content_range = _request(artifact.url, max_bytes=limit)
            if operation_id and operation_id != "-":
                _progress_store().raise_if_cancelled(operation_id)
            truncated = bool(size is not None and size > len(content))
            return UrlWindowResult(_decode(content), truncated, "bounded-full", size, 0, len(content))

        if size is None or size <= _URL_SMALL_FILE_BYTES:
            limit = _URL_FULL_READ_LIMIT if size is None else min(max(size, 1), _URL_FULL_READ_LIMIT)
            _status, content, _content_length, _content_range = _request(artifact.url, max_bytes=limit)
            if operation_id and operation_id != "-":
                _progress_store().raise_if_cancelled(operation_id)
            filtered = _filter_window_lines(content, start, end)
            truncated = bool(size is not None and size > len(content))
            return UrlWindowResult(_decode(filtered), truncated, "small-full", size, 0, len(content))

        start_offset, ranged_supported = _find_http_start_offset(artifact.url, size, start, operation_id)
        if not ranged_supported:
            _status, content, _content_length, _content_range = _request(
                artifact.url,
                max_bytes=min(size, _URL_MAX_SCAN_BYTES),
            )
            if operation_id and operation_id != "-":
                _progress_store().raise_if_cancelled(operation_id)
            filtered = _filter_window_lines(content, start, end)
            return UrlWindowResult(
                _decode(filtered),
                size > len(content),
                "range-unsupported",
                size,
                0,
                len(content),
            )

        position = max(0, start_offset or 0)
        scanned = 0
        carry = b""
        output = bytearray()
        entered = False
        stop = False
        truncated = False
        while position < size and scanned < _URL_MAX_SCAN_BYTES and not stop:
            if operation_id and operation_id != "-":
                _progress_store().raise_if_cancelled(operation_id)
            end_byte = min(size - 1, position + _URL_CHUNK_BYTES - 1)
            content, ranged = _fetch_range(artifact.url, position, end_byte)
            if operation_id and operation_id != "-":
                _progress_store().raise_if_cancelled(operation_id)
            if not ranged:
                truncated = True
                break
            scanned += len(content)
            data = carry + content
            raw_lines = data.split(b"\n")
            carry = raw_lines.pop() if end_byte < size - 1 else b""
            for raw in raw_lines:
                timestamp = _parse_timestamp_bytes(raw)
                if timestamp is not None:
                    if timestamp < start:
                        entered = False
                        continue
                    if timestamp > end:
                        # The stream is chronological.  Once a timestamp passes
                        # the requested end there is no reason to scan the rest
                        # of this physical file, even if no line matched yet.
                        stop = True
                        break
                    entered = True
                if entered:
                    output.extend(raw)
                    output.extend(b"\n")
            position = end_byte + 1
        if scanned >= _URL_MAX_SCAN_BYTES and position < size:
            truncated = True
        return UrlWindowResult(
            _decode(bytes(output)),
            truncated,
            "range-binary-seek",
            size,
            max(0, start_offset or 0),
            scanned,
        )
    finally:
        if operation_id and operation_id != "-" and not _progress_store().is_cancelled(operation_id):
            _progress_store().artifact_done(operation_id, identity=artifact.identity)


def stream_url_log_window(
    artifacts: list[UrlLogArtifact],
    start: datetime,
    end: datetime,
    *,
    operation_id: str = "-",
    include_internal_markers: bool = True,
) -> Iterator[bytes]:
    """Stream URL artifacts using the same marker format as environment search."""

    if operation_id and operation_id != "-":
        _progress_store().plan_ready(operation_id, artifact_total=len(artifacts))
    total_lines = 0
    total_bytes = 0
    for artifact in artifacts:
        result = read_url_log_window(artifact, start, end, operation_id=operation_id)
        if include_internal_markers:
            markers = (
                f"__TRACELENS_LOG_CATEGORY__={artifact.source_category}\n"
                f"__TRACELENS_LOG_SUBSYSTEM__={artifact.subsystem}\n"
                f"__TRACELENS_LOG_MODULE__={artifact.fm}\n"
                f"__TRACELENS_LOG_SOURCE_PATH__={artifact.source_path}\n"
            ).encode("utf-8")
            total_bytes += len(markers)
            yield markers
        payload = result.text.encode("utf-8", errors="replace")
        total_lines += payload.count(b"\n")
        total_bytes += len(payload)
        if payload:
            yield payload
    if operation_id and operation_id != "-":
        _progress_store().patch(operation_id, result_count=total_lines, output_bytes=total_bytes)
        _progress_store().finish(operation_id)


# Environment log positioning uses the same facade. Lazy imports avoid a circular
# dependency because remote_logs itself owns SSH-specific planning/streaming.
def build_environment_search_plan(*args, **kwargs):
    from apps.logsources.services.remote_logs import build_log_plan

    return build_log_plan(*args, **kwargs)


def stream_environment_search_window(*args, **kwargs):
    from apps.logsources.services.remote_logs import stream_log_window

    return stream_log_window(*args, **kwargs)
