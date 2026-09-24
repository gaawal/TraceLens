from __future__ import annotations

import errno
import hashlib
import logging
import re
import shlex
import stat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from apps.common.services.ssh import SshOperationError, ssh_session
from django.conf import settings

from apps.environments.models import Environment
from apps.logsources.services.cache_identity import safe_component
from apps.logsources.services.redis_store import RedisLogStore
from apps.reports.cache_store import CpdReportDiskCache
from apps.reports.parser import parse_report_summary

logger = logging.getLogger("tracelens.cpd_reports")

_REPORT_ROOT = "/data/{username}/report/cpd_report"
_SAFE_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _is_missing_path_message(value: str) -> bool:
    text = str(value or "").lower()
    return "no such file or directory" in text or "not a directory" in text or "path not found" in text


def _is_missing_path_error(exc: BaseException) -> bool:
    code = getattr(exc, "errno", None)
    return code in {errno.ENOENT, errno.ENOTDIR} or _is_missing_path_message(str(exc))


def report_root(environment: Environment) -> str:
    return _REPORT_ROOT.format(username=environment.upper_machine.username).rstrip("/")


def _safe_part(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value or not _SAFE_PART_RE.match(value) or value in {".", ".."}:
        raise ValueError(f"{label}不合法。")
    return value


def _read_text_head(sftp, path: str, max_bytes: int = 128 * 1024) -> str:
    with sftp.open(path, "rb") as handle:
        data = handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def scan_report_tree(environment: Environment) -> dict:
    root = report_root(environment)
    machine = environment.upper_machine
    started = time.monotonic()
    command = f"find {shlex.quote(root)} -mindepth 3 -maxdepth 3 -type f -name '*.rpt' -print"
    logger.info("cpd.tree.start environment=%s host=%s root=%s", environment.id, machine.host, root)
    with ssh_session(machine) as lease:
        _, stdout, stderr = lease.client.exec_command(command, timeout=45)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            if _is_missing_path_message(error):
                logger.info("cpd.tree.skip_missing environment=%s root=%s error=%s", environment.id, root, error.strip())
                return {
                    "environment_id": environment.id, "root": root, "subsystems": [],
                    "subsystem_count": 0, "module_count": 0, "report_count": 0,
                }
            raise SshOperationError(f"扫描 CPD 报告目录失败 {root}：{error.strip() or f'exit={code}'}")
    grouped: dict[str, dict[str, int]] = {}
    ignored = 0
    prefix = f"{root}/"
    for line in output.splitlines():
        raw_path = line.strip()
        relative = raw_path[len(prefix):] if raw_path.startswith(prefix) else raw_path
        parts = PurePosixPath(relative).parts
        if len(parts) != 3:
            ignored += 1
            continue
        subsystem, module, filename = parts
        if not filename.endswith(".rpt"):
            ignored += 1
            continue
        grouped.setdefault(subsystem, {}).setdefault(module, 0)
        grouped[subsystem][module] += 1
    subsystems = [
        {
            "name": subsystem,
            "module_count": len(modules),
            "report_count": sum(modules.values()),
            "modules": [
                {"name": module, "report_count": count}
                for module, count in sorted(modules.items())
            ],
        }
        for subsystem, modules in sorted(grouped.items())
    ]
    logger.info(
        "cpd.tree.finish environment=%s subsystems=%d modules=%d reports=%d ignored=%d elapsed_ms=%d",
        environment.id,
        len(subsystems),
        sum(item["module_count"] for item in subsystems),
        sum(item["report_count"] for item in subsystems),
        ignored,
        int((time.monotonic() - started) * 1000),
    )
    return {
        "environment_id": environment.id,
        "root": root,
        "subsystems": subsystems,
        "subsystem_count": len(subsystems),
        "module_count": sum(item["module_count"] for item in subsystems),
        "report_count": sum(item["report_count"] for item in subsystems),
    }


def list_reports(environment: Environment, subsystem: str, module: str, *, page: int = 1, page_size: int = 100) -> dict:
    subsystem = _safe_part(subsystem, "子系统")
    module = _safe_part(module, "模块")
    page = max(1, int(page))
    page_size = max(1, min(300, int(page_size)))
    root = report_root(environment)
    directory = f"{root}/{subsystem}/{module}"
    machine = environment.upper_machine
    logger.info("cpd.reports.list.start environment=%s subsystem=%s module=%s page=%d size=%d", environment.id, subsystem, module, page, page_size)
    started = time.monotonic()
    with ssh_session(machine) as lease:
        try:
            rows = [item for item in lease.sftp.listdir_attr(directory) if stat.S_ISREG(item.st_mode) and item.filename.endswith(".rpt")]
        except OSError as exc:
            if _is_missing_path_error(exc):
                logger.info("cpd.reports.list.skip_missing environment=%s directory=%s error=%s", environment.id, directory, exc)
                return {
                    "environment_id": environment.id, "subsystem": subsystem, "module": module,
                    "page": page, "page_size": page_size, "count": 0, "results": [],
                }
            raise SshOperationError(f"读取 CPD 报告目录失败 {directory}：{exc}") from exc
        rows.sort(key=lambda item: (item.st_mtime, item.filename), reverse=True)
        total = len(rows)
        start_index = (page - 1) * page_size
        selected = rows[start_index:start_index + page_size]
        results = []
        for index, item in enumerate(selected, start=1):
            path = f"{directory}/{item.filename}"
            try:
                text = _read_text_head(lease.sftp, path)
                summary = parse_report_summary(text, file_name=item.filename, full_path=path, modified_at=item.st_mtime)
                summary.update({"subsystem": subsystem, "module": module, "parse_status": "success"})
                results.append(summary)
            except Exception as exc:
                logger.exception("cpd.report.parse_failed environment=%s path=%s", environment.id, path)
                results.append({
                    "file_name": item.filename,
                    "full_path": path,
                    "parse_status": "error",
                    "parse_message": str(exc),
                    "modified_at": datetime.fromtimestamp(item.st_mtime).isoformat(timespec="seconds"),
                })
            if index == 1 or index % 50 == 0:
                logger.info("cpd.reports.list.progress environment=%s subsystem=%s module=%s parsed=%d/%d", environment.id, subsystem, module, index, len(selected))
    logger.info("cpd.reports.list.finish environment=%s subsystem=%s module=%s total=%d returned=%d elapsed_ms=%d", environment.id, subsystem, module, total, len(results), int((time.monotonic() - started) * 1000))
    return {
        "environment_id": environment.id,
        "subsystem": subsystem,
        "module": module,
        "page": page,
        "page_size": page_size,
        "count": total,
        "results": results,
    }


def list_all_reports(environment: Environment, *, page: int = 1, page_size: int = 100) -> dict:
    """Return reports across every subsystem/module, newest first."""
    page = max(1, int(page))
    page_size = max(1, min(300, int(page_size)))
    root = report_root(environment)
    machine = environment.upper_machine
    logger.info("cpd.reports.all.start environment=%s host=%s page=%d size=%d", environment.id, machine.host, page, page_size)
    started = time.monotonic()
    command = f"find {shlex.quote(root)} -mindepth 3 -maxdepth 3 -type f -name '*.rpt' -printf '%T@\t%p\n'"
    with ssh_session(machine) as lease:
        _, stdout, stderr = lease.client.exec_command(command, timeout=45)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            if _is_missing_path_message(error):
                logger.info("cpd.reports.all.skip_missing environment=%s root=%s error=%s", environment.id, root, error.strip())
                output = ""
            else:
                raise SshOperationError(f"扫描 CPD 报告失败 {root}：{error.strip() or f'exit={code}'}")
        rows: list[tuple[float, str, str, str, str]] = []
        prefix = f"{root}/"
        for line in output.splitlines():
            try:
                mtime_text, path = line.split("\t", 1)
                relative = path[len(prefix):] if path.startswith(prefix) else path
                parts = PurePosixPath(relative).parts
                if len(parts) != 3:
                    continue
                subsystem, module, filename = parts
                if not filename.endswith(".rpt"):
                    continue
                rows.append((float(mtime_text), path, subsystem, module, filename))
            except (ValueError, TypeError):
                continue
        rows.sort(key=lambda item: (item[0], item[4]), reverse=True)
        total = len(rows)
        start_index = (page - 1) * page_size
        selected = rows[start_index:start_index + page_size]
        results = []
        for index, (mtime, path, subsystem, module, filename) in enumerate(selected, start=1):
            try:
                text = _read_text_head(lease.sftp, path)
                summary = parse_report_summary(text, file_name=filename, full_path=path, modified_at=mtime)
                summary.update({"subsystem": subsystem, "module": module, "parse_status": "success"})
                results.append(summary)
            except Exception as exc:
                logger.exception("cpd.report.parse_failed environment=%s path=%s", environment.id, path)
                results.append({
                    "file_name": filename, "full_path": path, "subsystem": subsystem, "module": module,
                    "parse_status": "error", "parse_message": str(exc),
                    "modified_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                })
            if index == 1 or index % 50 == 0:
                logger.info("cpd.reports.all.progress environment=%s parsed=%d/%d", environment.id, index, len(selected))
    logger.info("cpd.reports.all.finish environment=%s total=%d returned=%d elapsed_ms=%d", environment.id, total, len(results), int((time.monotonic() - started) * 1000))
    return {
        "environment_id": environment.id, "subsystem": "", "module": "",
        "page": page, "page_size": page_size, "count": total, "results": results,
    }



def _report_catalog_cache_key(environment: Environment) -> str:
    machine = environment.upper_machine
    return (
        f"tracelens:{RedisLogStore.CACHE_SCHEMA}:cpd:"
        f"host-{safe_component(machine.host)}:user-{safe_component(machine.username)}:catalog"
    )


def _report_summary_cache_key(environment: Environment, row: dict) -> str:
    machine = environment.upper_machine
    identity = hashlib.sha1(str(row["full_path"]).encode("utf-8")).hexdigest()[:20]
    fingerprint = f"{int(row.get('size') or 0)}-{int(float(row.get('mtime') or 0) * 1000)}"
    return (
        f"tracelens:{RedisLogStore.CACHE_SCHEMA}:cpd:"
        f"host-{safe_component(machine.host)}:user-{safe_component(machine.username)}:"
        f"summary:{identity}:{fingerprint}"
    )




def _report_content_cache_key(environment: Environment, path: str, size: int = 0, mtime: float = 0.0) -> str:
    machine = environment.upper_machine
    identity = hashlib.sha1(path.encode("utf-8", errors="replace")).hexdigest()[:20]
    fingerprint = f"{int(size)}-{int(float(mtime) * 1000)}"
    return (
        f"tracelens:{RedisLogStore.CACHE_SCHEMA}:cpd:"
        f"host-{safe_component(machine.host)}:user-{safe_component(machine.username)}:"
        f"content:{identity}:{fingerprint}"
    )


def _parse_filename_report_time(filename: str, mtime: float) -> datetime:
    # Report names normally contain the execution timestamp, for example
    # DSPWSFT_20260805153922.rpt.  Do not require the timestamp to be the exact
    # final token: some environments append extra markers after it.  The last
    # valid 14/17-digit timestamp wins; mtime is only a compatibility fallback.
    stem = PurePosixPath(filename).stem
    matches = re.findall(r"(?<!\d)(\d{17}|\d{14})(?!\d)", stem)
    for raw in reversed(matches):
        try:
            if len(raw) == 17:
                return datetime.strptime(raw, "%Y%m%d%H%M%S%f").astimezone()
            return datetime.strptime(raw, "%Y%m%d%H%M%S").astimezone()
        except ValueError:
            continue
    return datetime.fromtimestamp(mtime).astimezone()


def _report_tree_from_catalog(root: str, rows: list[dict]) -> dict:
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        grouped.setdefault(row["subsystem"], {}).setdefault(row["module"], 0)
        grouped[row["subsystem"]][row["module"]] += 1
    subsystems = [
        {
            "name": subsystem,
            "module_count": len(modules),
            "report_count": sum(modules.values()),
            "modules": [{"name": module, "report_count": count} for module, count in sorted(modules.items())],
        }
        for subsystem, modules in sorted(grouped.items())
    ]
    return {
        "root": root,
        "subsystems": subsystems,
        "subsystem_count": len(subsystems),
        "module_count": sum(item["module_count"] for item in subsystems),
        "report_count": len(rows),
    }


def _hydrate_catalog(environment: Environment, cached: dict, cache_status: str) -> dict:
    tree = dict(cached.get("tree") or {})
    tree["environment_id"] = environment.id
    return {
        "environment_id": environment.id,
        "tree": tree,
        # Kept for frontend/backward compatibility. v0.16 no longer parses every
        # report while creating the snapshot; summaries are loaded per query/page.
        "reports": [],
        "cache_status": cache_status,
        "fingerprint": str(cached.get("fingerprint") or ""),
        "cached_at": cached.get("cached_at"),
        "catalog_count": len(cached.get("catalog") or []),
    }



def _decode_stream(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _scan_catalog_parallel(environment: Environment) -> tuple[list[dict], str]:
    """Scan CPD report metadata with bounded per-subsystem SSH channels.

    The expensive part is directory walking on environments with thousands of
    reports.  We first list only top-level subsystem directories, then scan each
    subsystem concurrently over separate SSH channels.  No .rpt content is read
    here; only size/mtime/path are collected.
    """
    root = report_root(environment)
    machine = environment.upper_machine
    scan_workers = max(1, min(12, int(getattr(settings, "TRACELENS_CPD_SCAN_WORKERS", 4))))
    prefix = f"{root}/"

    with ssh_session(machine) as lease:
        list_command = (
            f"find {shlex.quote(root)} -mindepth 1 -maxdepth 1 -type d "
            "-printf '%f\\n'"
        )
        _, stdout, stderr = lease.client.exec_command(list_command, timeout=45)
        subsystem_output = _decode_stream(stdout.read())
        subsystem_error = _decode_stream(stderr.read())
        code = stdout.channel.recv_exit_status()
        if code != 0:
            if _is_missing_path_message(subsystem_error):
                logger.info("cpd.catalog.skip_missing environment=%s root=%s error=%s", environment.id, root, subsystem_error.strip())
                return [], hashlib.sha256(b"").hexdigest()
            raise SshOperationError(
                f"扫描 CPD 子系统目录失败 {root}：{subsystem_error.strip() or f'exit={code}'}"
            )
        subsystems = sorted({line.strip() for line in subsystem_output.splitlines() if line.strip()})
        logger.info(
            "cpd.catalog.subsystems environment=%s host=%s count=%d workers=%d",
            environment.id, machine.host, len(subsystems), scan_workers,
        )

        def scan_one(subsystem: str) -> tuple[str, str]:
            directory = f"{root}/{subsystem}"
            command = (
                f"find {shlex.quote(directory)} -mindepth 2 -maxdepth 2 -type f -name '*.rpt' "
                "-printf '%s\\t%T@\\t%p\\n'"
            )
            _, sub_stdout, sub_stderr = lease.client.exec_command(command, timeout=60)
            output = _decode_stream(sub_stdout.read())
            error = _decode_stream(sub_stderr.read())
            status = sub_stdout.channel.recv_exit_status()
            if status != 0:
                if _is_missing_path_message(error):
                    logger.info("cpd.catalog.subsystem_skip_missing environment=%s directory=%s error=%s", environment.id, directory, error.strip())
                    return subsystem, ""
                raise SshOperationError(
                    f"扫描 CPD 子系统失败 {directory}：{error.strip() or f'exit={status}'}"
                )
            return subsystem, output

        outputs: list[tuple[str, str]] = []
        if len(subsystems) <= 1:
            for subsystem in subsystems:
                outputs.append(scan_one(subsystem))
        else:
            with ThreadPoolExecutor(max_workers=min(scan_workers, len(subsystems)), thread_name_prefix="cpd-scan") as executor:
                futures = {executor.submit(scan_one, subsystem): subsystem for subsystem in subsystems}
                for future in as_completed(futures):
                    outputs.append(future.result())

    rows: list[dict] = []
    fingerprint_rows: list[str] = []
    for _, output in outputs:
        for line in output.splitlines():
            try:
                size_text, mtime_text, path = line.split("\t", 2)
                relative = path[len(prefix):] if path.startswith(prefix) else path
                parts = PurePosixPath(relative).parts
                if len(parts) != 3 or not parts[2].endswith(".rpt"):
                    continue
                subsystem, module, filename = parts
                size = int(size_text)
                mtime = float(mtime_text)
                report_time = _parse_filename_report_time(filename, mtime)
                rows.append({
                    "file_name": filename,
                    "full_path": path,
                    "subsystem": subsystem,
                    "module": module,
                    "size": size,
                    "mtime": mtime,
                    "modified_at": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
                    "catalog_time": report_time.isoformat(timespec="microseconds"),
                })
                fingerprint_rows.append(f"{path}\t{size}\t{mtime_text}")
            except (ValueError, TypeError):
                logger.warning("cpd.catalog.bad_record line=%r", line[:300])
    rows.sort(key=lambda item: (item["catalog_time"], item["file_name"]), reverse=True)
    fingerprint = hashlib.sha256("\n".join(sorted(fingerprint_rows)).encode("utf-8")).hexdigest()
    return rows, fingerprint


def _load_report_catalog(environment: Environment, *, refresh: bool = False) -> tuple[dict, str]:
    """Load a metadata-only CPD catalog.

    The catalog contains only path/size/mtime/subsystem/module/file and an inferred
    report timestamp. It never opens .rpt files. This makes the first screen cheap
    even when a resource contains thousands of historical reports.
    """
    root = report_root(environment)
    machine = environment.upper_machine
    cache_key = _report_catalog_cache_key(environment)
    cached = RedisLogStore.get_json(cache_key)
    cache_source = "redis"
    if not isinstance(cached, dict) and not refresh:
        cached = CpdReportDiskCache.get_json(cache_key)
        cache_source = "disk"
        if isinstance(cached, dict):
            # Warm Redis opportunistically; disk cache remains authoritative fallback.
            RedisLogStore.set_json(cache_key, cached, CpdReportDiskCache.ttl())
    if isinstance(cached, dict) and not refresh:
        logger.info(
            "cpd.catalog.cache_hit environment=%s host=%s user=%s files=%d source=%s",
            environment.id, machine.host, machine.username, len(cached.get("catalog") or []), cache_source,
        )
        return cached, f"hit-{cache_source}"

    started = time.monotonic()
    logger.info(
        "cpd.catalog.scan_start environment=%s host=%s user=%s root=%s refresh=%s",
        environment.id, machine.host, machine.username, root, refresh,
    )
    rows, fingerprint = _scan_catalog_parallel(environment)

    if isinstance(cached, dict) and str(cached.get("fingerprint") or "") == fingerprint:
        logger.info(
            "cpd.catalog.refresh_unchanged environment=%s host=%s files=%d elapsed_ms=%d",
            environment.id, machine.host, len(cached.get("catalog") or []), int((time.monotonic() - started) * 1000),
        )
        CpdReportDiskCache.set_json(cache_key, cached)
        return cached, "unchanged"

    stored = {
        "fingerprint": fingerprint,
        "tree": _report_tree_from_catalog(root, rows),
        "catalog": rows,
        "cached_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    ttl = int(getattr(settings, "TRACELENS_CPD_SNAPSHOT_TTL", 30 * 24 * 60 * 60))
    redis_stored = RedisLogStore.set_json(cache_key, stored, ttl)
    disk_stored = CpdReportDiskCache.set_json(cache_key, stored)
    logger.info(
        "cpd.catalog.finish environment=%s host=%s files=%d redis_store=%s disk_store=%s elapsed_ms=%d",
        environment.id, machine.host, len(rows), redis_stored, disk_stored, int((time.monotonic() - started) * 1000),
    )
    return stored, "rebuilt"


def load_report_snapshot(environment: Environment, *, refresh: bool = False) -> dict:
    """Return only the lightweight CPD directory snapshot.

    v0.16 deliberately does not parse all reports here. The report table uses
    query_report_summaries(), which parses only the page/range that is needed and
    stores every parsed report summary separately in Redis.
    """
    catalog, status = _load_report_catalog(environment, refresh=refresh)
    return _hydrate_catalog(environment, catalog, status)


def _parse_query_time(value: str | None) -> datetime | None:
    value = (value or "").strip().replace("T", " ")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("时间格式应为 YYYY-MM-DD HH:mm:ss。") from exc
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _summary_from_cache(environment: Environment, row: dict) -> dict | None:
    key = _report_summary_cache_key(environment, row)
    cached = RedisLogStore.get_json(key)
    if not isinstance(cached, dict):
        cached = CpdReportDiskCache.get_json(key)
        if isinstance(cached, dict):
            RedisLogStore.set_json(key, cached, CpdReportDiskCache.ttl())
    return dict(cached) if isinstance(cached, dict) else None


def _summaries_from_cache(environment: Environment, rows: list[dict]) -> list[dict | None]:
    keys = [_report_summary_cache_key(environment, row) for row in rows]
    values = RedisLogStore.get_json_many(keys)
    disk_values: list[object | None] | None = None
    result: list[dict | None] = []
    for index, (key, value) in enumerate(zip(keys, values)):
        if isinstance(value, dict):
            result.append(dict(value))
            continue
        if disk_values is None:
            disk_values = CpdReportDiskCache.get_json_many(keys)
        disk_value = disk_values[index]
        if isinstance(disk_value, dict):
            RedisLogStore.set_json(key, disk_value, CpdReportDiskCache.ttl())
            result.append(dict(disk_value))
        else:
            result.append(None)
    return result


def _parse_and_cache_summary(environment: Environment, lease, row: dict) -> tuple[dict, bool]:
    cached = _summary_from_cache(environment, row)
    if cached is not None:
        return cached, False
    path = row["full_path"]
    try:
        text = _read_text_head(lease.sftp, path)
        summary = parse_report_summary(text, file_name=row["file_name"], full_path=path, modified_at=float(row["mtime"]))
        summary.update({"subsystem": row["subsystem"], "module": row["module"], "parse_status": "success"})
    except Exception as exc:
        logger.exception("cpd.summary.parse_failed environment=%s path=%s", environment.id, path)
        summary = {
            "file_name": row["file_name"], "full_path": path, "subsystem": row["subsystem"], "module": row["module"],
            "parse_status": "error", "parse_message": str(exc), "modified_at": row.get("modified_at"),
        }
    ttl = int(getattr(settings, "TRACELENS_CPD_SNAPSHOT_TTL", 30 * 24 * 60 * 60))
    summary_key = _report_summary_cache_key(environment, row)
    RedisLogStore.set_json(summary_key, summary, ttl)
    CpdReportDiskCache.set_json(summary_key, summary)
    return summary, True


def _summary_matches(summary: dict, *, result: str, validation: str, quality: str, mcs: str, duration: str, query: str) -> bool:
    if result and str(summary.get("test_run_result") or "") != result:
        return False
    if validation and str(summary.get("results_validation") or "") != validation:
        return False
    if quality and str(summary.get("measurement_quality") or "") != quality:
        return False
    if mcs and str(summary.get("mcs_status") or "") != mcs:
        return False
    if duration and duration.lower() not in str(summary.get("execution_time") or "").lower():
        return False
    if query:
        needle = query.lower()
        values = [
            summary.get("subsystem"), summary.get("module"), summary.get("file_name"), summary.get("cpd_name"),
            summary.get("operator"), summary.get("software_ver"), summary.get("test_run_result"),
            summary.get("results_validation"), summary.get("measurement_quality"), summary.get("mcs_status"),
        ]
        if not any(needle in str(value or "").lower() for value in values):
            return False
    return True



def _report_dataset_cache_key(environment: Environment, catalog_fingerprint: str, start_dt: datetime | None, end_dt: datetime | None) -> str:
    machine = environment.upper_machine
    range_text = f"{_dt_for_cache(start_dt)}|{_dt_for_cache(end_dt)}|{catalog_fingerprint}"
    identity = hashlib.sha1(range_text.encode("utf-8")).hexdigest()[:24]
    return (
        f"tracelens:{RedisLogStore.CACHE_SCHEMA}:cpd:"
        f"host-{safe_component(machine.host)}:user-{safe_component(machine.username)}:dataset:{identity}"
    )


def _dt_for_cache(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _parse_summary_row(environment: Environment, sftp, row: dict) -> dict:
    path = row["full_path"]
    try:
        text = _read_text_head(sftp, path)
        summary = parse_report_summary(
            text,
            file_name=row["file_name"],
            full_path=path,
            modified_at=float(row["mtime"]),
        )
        summary.update({"subsystem": row["subsystem"], "module": row["module"], "parse_status": "success"})
    except Exception as exc:
        logger.exception("cpd.summary.parse_failed environment=%s path=%s", environment.id, path)
        summary = {
            "file_name": row["file_name"],
            "full_path": path,
            "subsystem": row["subsystem"],
            "module": row["module"],
            "parse_status": "error",
            "parse_message": str(exc),
            "modified_at": row.get("modified_at"),
        }
    return summary


def _parse_missing_summaries_parallel(environment: Environment, rows: list[dict]) -> list[dict]:
    """Parse uncached report headers concurrently over bounded SFTP channels."""
    if not rows:
        return []
    machine = environment.upper_machine
    worker_count = max(1, min(12, int(getattr(settings, "TRACELENS_CPD_PARSE_WORKERS", 6)), len(rows)))
    buckets: list[list[tuple[int, dict]]] = [[] for _ in range(worker_count)]
    for index, row in enumerate(rows):
        buckets[index % worker_count].append((index, row))
    result: list[dict | None] = [None] * len(rows)
    ttl = int(getattr(settings, "TRACELENS_CPD_SNAPSHOT_TTL", 30 * 24 * 60 * 60))
    started = time.monotonic()
    logger.info(
        "cpd.dataset.parallel_parse.start environment=%s host=%s files=%d workers=%d",
        environment.id, machine.host, len(rows), worker_count,
    )
    with ssh_session(machine) as lease:
        def parse_bucket(bucket: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
            channel = lease.client.open_sftp()
            parsed: list[tuple[int, dict]] = []
            try:
                for index, row in bucket:
                    summary = _parse_summary_row(environment, channel, row)
                    parsed.append((index, summary))
            finally:
                channel.close()
            return parsed

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="cpd-parse") as executor:
            futures = [executor.submit(parse_bucket, bucket) for bucket in buckets if bucket]
            completed = 0
            for future in as_completed(futures):
                for index, summary in future.result():
                    result[index] = summary
                    completed += 1
                    if completed == 1 or completed % 100 == 0 or completed == len(rows):
                        logger.info(
                            "cpd.dataset.parallel_parse.progress environment=%s parsed=%d/%d",
                            environment.id, completed, len(rows),
                        )
    for row, summary in zip(rows, result):
        if summary is not None:
            summary_key = _report_summary_cache_key(environment, row)
            RedisLogStore.set_json(summary_key, summary, ttl)
            CpdReportDiskCache.set_json(summary_key, summary)
    logger.info(
        "cpd.dataset.parallel_parse.finish environment=%s files=%d elapsed_ms=%d",
        environment.id, len(rows), int((time.monotonic() - started) * 1000),
    )
    return [summary if summary is not None else {} for summary in result]


def load_report_dataset(
    environment: Environment,
    *,
    start_time: str = "",
    end_time: str = "",
    refresh: bool = False,
) -> dict:
    """Build a complete, filterable CPD summary dataset for one time range.

    The catalog scan is cached by physical resource (host + username).  File-name
    timestamps reduce the candidate set before any report body is opened.  Every
    candidate summary is then loaded by one Redis MGET; only cache misses are
    parsed concurrently.  The completed range dataset is cached as a whole so
    revisiting the page does not repeat directory scans or report reads.
    """
    start_dt = _parse_query_time(start_time)
    end_dt = _parse_query_time(end_time)
    if start_dt and end_dt and end_dt < start_dt:
        raise ValueError("结束时间不能早于开始时间。")
    if start_dt and end_dt and end_dt - start_dt > timedelta(days=30):
        raise ValueError("CPD 报告查询时间范围最多支持 30 天。")
    catalog, catalog_status = _load_report_catalog(environment, refresh=refresh)
    rows = list(catalog.get("catalog") or [])
    candidates: list[dict] = []
    for row in rows:
        try:
            row_time = datetime.fromisoformat(str(row.get("catalog_time") or ""))
        except ValueError:
            row_time = datetime.fromtimestamp(float(row.get("mtime") or 0)).astimezone()
        if start_dt and row_time < start_dt:
            continue
        if end_dt and row_time > end_dt:
            continue
        candidates.append(row)

    fingerprint = str(catalog.get("fingerprint") or "")
    dataset_key = _report_dataset_cache_key(environment, fingerprint, start_dt, end_dt)
    cached_dataset = RedisLogStore.get_json(dataset_key)
    dataset_source = "redis"
    if not isinstance(cached_dataset, dict):
        cached_dataset = CpdReportDiskCache.get_json(dataset_key)
        dataset_source = "disk"
        if isinstance(cached_dataset, dict):
            RedisLogStore.set_json(dataset_key, cached_dataset, CpdReportDiskCache.ttl())
    if isinstance(cached_dataset, dict):
        payload = dict(cached_dataset)
        payload["environment_id"] = environment.id
        payload["dataset_cache_status"] = f"hit-{dataset_source}"
        payload["catalog_cache_status"] = catalog_status
        logger.info(
            "cpd.dataset.cache_hit environment=%s candidates=%d start=%s end=%s source=%s",
            environment.id, len(candidates), _dt_for_cache(start_dt), _dt_for_cache(end_dt), dataset_source,
        )
        return payload

    started = time.monotonic()
    cached_summaries = _summaries_from_cache(environment, candidates)
    missing_rows: list[dict] = []
    missing_indexes: list[int] = []
    summary_cache_hits = 0
    for index, summary in enumerate(cached_summaries):
        if summary is None:
            missing_indexes.append(index)
            missing_rows.append(candidates[index])
        else:
            summary_cache_hits += 1
    parsed = _parse_missing_summaries_parallel(environment, missing_rows) if missing_rows else []
    for index, summary in zip(missing_indexes, parsed):
        cached_summaries[index] = summary
    summaries = [dict(item) for item in cached_summaries if isinstance(item, dict)]
    summaries.sort(
        key=lambda item: (str(item.get("start_time") or item.get("modified_at") or ""), str(item.get("file_name") or "")),
        reverse=True,
    )
    root = str((catalog.get("tree") or {}).get("root") or report_root(environment))
    range_tree = _report_tree_from_catalog(root, candidates)
    range_tree["environment_id"] = environment.id
    all_history_tree = dict(catalog.get("tree") or {})
    all_history_tree["environment_id"] = environment.id
    payload = {
        "environment_id": environment.id,
        "start_time": _dt_for_cache(start_dt),
        "end_time": _dt_for_cache(end_dt),
        "count": len(summaries),
        "candidate_count": len(candidates),
        "results": summaries,
        "range_tree": range_tree,
        "all_history_tree": all_history_tree,
        "parsed_now": len(missing_rows),
        "summary_cache_hits": summary_cache_hits,
        "catalog_cache_status": catalog_status,
        "dataset_cache_status": "rebuilt",
        "fingerprint": fingerprint,
        "cached_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    ttl = int(getattr(settings, "TRACELENS_CPD_SNAPSHOT_TTL", 30 * 24 * 60 * 60))
    RedisLogStore.set_json(dataset_key, payload, ttl)
    CpdReportDiskCache.set_json(dataset_key, payload)
    logger.info(
        "cpd.dataset.finish environment=%s candidates=%d parsed_now=%d cache_hits=%d elapsed_ms=%d",
        environment.id, len(candidates), len(missing_rows), summary_cache_hits,
        int((time.monotonic() - started) * 1000),
    )
    return payload


def query_report_summaries(
    environment: Environment,
    *,
    page: int = 1,
    page_size: int = 100,
    start_time: str = "",
    end_time: str = "",
    subsystem: str = "",
    module: str = "",
    file_name: str = "",
    result: str = "",
    validation: str = "",
    quality: str = "",
    mcs: str = "",
    duration: str = "",
    query: str = "",
) -> dict:
    """Query CPD summaries without rebuilding a full report snapshot.

    Metadata-only filters (time/subsystem/module/file) are applied against the
    cheap catalog. With no content-field filter, only the requested page is
    parsed. When a content-field filter is active, every still-unindexed report
    inside *the current time range only* is parsed once so filtering stays exact;
    each result is then reusable from Redis on later queries.
    """
    page = max(1, int(page))
    page_size = max(1, min(300, int(page_size)))
    start_dt = _parse_query_time(start_time)
    end_dt = _parse_query_time(end_time)
    if start_dt and end_dt and end_dt < start_dt:
        raise ValueError("结束时间不能早于开始时间。")

    catalog, catalog_status = _load_report_catalog(environment, refresh=False)
    rows = list(catalog.get("catalog") or [])
    file_needle = file_name.strip().lower()

    # Build a metadata-only tree for the *active time/file range* before applying
    # the subsystem/module selection.  The previous implementation displayed
    # all-history counts from the catalog while the query itself defaulted to
    # the most recent seven days.  That made a module look non-empty and then
    # return zero rows when clicked.  This range tree keeps the clickable counts
    # on exactly the same metadata time boundary as the query without opening
    # any .rpt files.
    range_rows: list[dict] = []
    for row in rows:
        if file_needle and file_needle not in str(row.get("file_name") or "").lower():
            continue
        try:
            row_time = datetime.fromisoformat(str(row.get("catalog_time") or ""))
        except ValueError:
            row_time = datetime.fromtimestamp(float(row.get("mtime") or 0)).astimezone()
        if start_dt and row_time < start_dt:
            continue
        if end_dt and row_time > end_dt:
            continue
        range_rows.append(row)

    range_tree = _report_tree_from_catalog(str((catalog.get("tree") or {}).get("root") or report_root(environment)), range_rows)
    range_tree["environment_id"] = environment.id

    candidates: list[dict] = []
    for row in range_rows:
        if subsystem and row.get("subsystem") != subsystem:
            continue
        if module and row.get("module") != module:
            continue
        candidates.append(row)

    content_filter = any(value.strip() for value in (result, validation, quality, mcs, duration, query))
    started = time.monotonic()
    parsed_now = 0
    cache_hits = 0
    results: list[dict] = []
    machine = environment.upper_machine

    if content_filter:
        logger.info(
            "cpd.query.range_index.start environment=%s candidates=%d page=%d size=%d",
            environment.id, len(candidates), page, page_size,
        )
        summaries = _summaries_from_cache(environment, candidates)
        missing: list[tuple[int, dict]] = []
        for index, (row, summary) in enumerate(zip(candidates, summaries)):
            if summary is None:
                missing.append((index, row))
            else:
                cache_hits += 1
        if missing:
            logger.info(
                "cpd.query.range_index.cache_miss environment=%s missing=%d cached=%d",
                environment.id, len(missing), cache_hits,
            )
            with ssh_session(machine) as lease:
                for parsed_index, (index, row) in enumerate(missing, start=1):
                    summary, was_parsed = _parse_and_cache_summary(environment, lease, row)
                    summaries[index] = summary
                    parsed_now += int(was_parsed)
                    if parsed_index == 1 or parsed_index % 100 == 0 or parsed_index == len(missing):
                        logger.info(
                            "cpd.query.range_index.progress environment=%s parsed=%d/%d parsed_now=%d",
                            environment.id, parsed_index, len(missing), parsed_now,
                        )
        matched = [
            summary for summary in summaries
            if summary is not None and _summary_matches(
                summary, result=result, validation=validation, quality=quality, mcs=mcs, duration=duration, query=query.strip()
            )
        ]
        count = len(matched)
        start_index = (page - 1) * page_size
        results = matched[start_index:start_index + page_size]
        index_mode = "range"
    else:
        count = len(candidates)
        start_index = (page - 1) * page_size
        selected = candidates[start_index:start_index + page_size]
        if selected:
            summaries = _summaries_from_cache(environment, selected)
            missing: list[tuple[int, dict]] = []
            for index, (row, summary) in enumerate(zip(selected, summaries)):
                if summary is None:
                    missing.append((index, row))
                else:
                    cache_hits += 1
            if missing:
                logger.info(
                    "cpd.query.page.cache_miss environment=%s page=%d missing=%d cached=%d",
                    environment.id, page, len(missing), cache_hits,
                )
                with ssh_session(machine) as lease:
                    for index, row in missing:
                        summary, was_parsed = _parse_and_cache_summary(environment, lease, row)
                        summaries[index] = summary
                        parsed_now += int(was_parsed)
            results = [summary for summary in summaries if summary is not None]
        index_mode = "page"

    logger.info(
        "cpd.query.finish environment=%s candidates=%d matched=%d returned=%d parsed_now=%d cache_hits=%d mode=%s elapsed_ms=%d",
        environment.id, len(candidates), count, len(results), parsed_now, cache_hits, index_mode,
        int((time.monotonic() - started) * 1000),
    )
    return {
        "environment_id": environment.id,
        "page": page,
        "page_size": page_size,
        "count": count,
        "candidate_count": len(candidates),
        "range_tree": range_tree,
        "range_report_count": len(range_rows),
        "results": results,
        "parsed_now": parsed_now,
        "summary_cache_hits": cache_hits,
        "index_mode": index_mode,
        "cache_status": catalog_status,
        "fingerprint": str(catalog.get("fingerprint") or ""),
        "cached_at": catalog.get("cached_at"),
    }


def read_report_content(environment: Environment, subsystem: str, module: str, file_name: str) -> dict:
    subsystem = _safe_part(subsystem, "子系统")
    module = _safe_part(module, "模块")
    file_name = _safe_part(file_name, "报告文件")
    if not file_name.endswith(".rpt"):
        raise ValueError("仅支持 .rpt 报告。")
    path = f"{report_root(environment)}/{subsystem}/{module}/{file_name}"

    # Use the cached catalog fingerprint so reopening a report can avoid SSH too.
    catalog, _ = _load_report_catalog(environment, refresh=False)
    row = next((item for item in (catalog.get("catalog") or []) if str(item.get("full_path") or "") == path), None)
    size = int((row or {}).get("size") or 0)
    mtime = float((row or {}).get("mtime") or 0.0)
    content_key = _report_content_cache_key(environment, path, size, mtime)
    cached = RedisLogStore.get_json(content_key)
    cache_source = "redis"
    if not isinstance(cached, dict):
        cached = CpdReportDiskCache.get_json(content_key)
        cache_source = "disk"
        if isinstance(cached, dict):
            RedisLogStore.set_json(content_key, cached, CpdReportDiskCache.ttl())
    if isinstance(cached, dict) and isinstance(cached.get("content"), str):
        logger.info("cpd.report.content.cache_hit environment=%s path=%s source=%s", environment.id, path, cache_source)
        return {"file_name": file_name, "path": path, "content": cached["content"], "cache_status": f"hit-{cache_source}"}

    logger.info("cpd.report.content.start environment=%s path=%s", environment.id, path)
    with ssh_session(environment.upper_machine) as lease:
        with lease.sftp.open(path, "rb") as handle:
            data = handle.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("报告文件超过 2 MiB，拒绝直接展示。")
    text = data.decode("utf-8", errors="replace")
    payload = {"file_name": file_name, "path": path, "content": text}
    RedisLogStore.set_json(content_key, payload, CpdReportDiskCache.ttl())
    CpdReportDiskCache.set_json(content_key, payload)
    logger.info("cpd.report.content.finish environment=%s path=%s bytes=%d", environment.id, path, len(data))
    return {**payload, "cache_status": "rebuilt"}
