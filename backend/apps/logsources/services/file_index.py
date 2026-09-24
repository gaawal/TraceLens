from __future__ import annotations

import errno
import hashlib
import logging
import re
import shlex
import stat as statmod
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Iterable

from django.conf import settings

from apps.common.services.ssh import SshOperationError
from apps.logsources.services.archive_selector import LogArtifact, parse_archive_timestamp, parse_log_name
from apps.logsources.services.cache_identity import LogCacheScope
from apps.logsources.services.redis_store import RedisLogStore
from apps.logsources.services.search_progress import LogSearchProgressStore

logger = logging.getLogger("tracelens.log_file_index")


def _is_missing_directory_error(exc: BaseException) -> bool:
    code = getattr(exc, "errno", None)
    if code in {errno.ENOENT, errno.ENOTDIR}:
        return True
    text = str(exc or "").lower()
    return "no such file" in text or "not a directory" in text or "path not found" in text


def _find_output_missing_directory(error: str) -> bool:
    text = str(error or "").lower()
    return "no such file or directory" in text or "not a directory" in text
LINE_TIME_RE_BYTES = re.compile(rb"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")
LINE_TIME_RE_TEXT = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")


def parse_line_time(line: bytes | bytearray | memoryview | str) -> datetime | None:
    """Parse TraceLens log timestamps from either binary or text streams.

    SFTP file reads normally yield bytes, while Paramiko ``exec_command``
    streams may yield either bytes or str depending on the channel/file wrapper.
    Keeping both forms here prevents individual readers from having to guess.
    """
    if isinstance(line, str):
        match = LINE_TIME_RE_TEXT.match(line)
        if not match:
            return None
        text = match.group(1)
    else:
        raw = bytes(line)
        match = LINE_TIME_RE_BYTES.match(raw)
        if not match:
            return None
        text = match.group(1).decode("ascii", errors="ignore")
    if "." in text:
        head, fraction = text.split(".", 1)
        text = f"{head}.{fraction[:6].ljust(6, '0')}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _dt_dump(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds") if value else None


def _dt_load(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RemoteFileStat:
    path: str
    name: str
    inode: int
    size: int
    mtime: str

    @property
    def fingerprint(self) -> str:
        # Fingerprint intentionally avoids full-file hashing: one cheap metadata
        # command can collect inode + size + mtime for every candidate.
        return f"{self.inode}:{self.size}:{self.mtime}"

    @property
    def mtime_datetime(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(self.mtime))
        except (TypeError, ValueError, OSError):
            return None


def _generation_for(stat: RemoteFileStat, *, salt: str = "") -> str:
    raw = f"{stat.path}|{stat.inode}|{stat.size}|{stat.mtime}|{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _read_head_tail(sftp, path: str, *, head_bytes: int, tail_bytes: int) -> tuple[datetime | None, datetime | None]:
    with sftp.open(path, "rb") as handle:
        size = handle.stat().st_size
        handle.seek(0)
        head = handle.read(min(size, head_bytes))
        tail_offset = max(0, size - tail_bytes)
        handle.seek(tail_offset)
        tail = handle.read(min(size, tail_bytes))

    first = None
    for line in head.splitlines():
        first = parse_line_time(line)
        if first is not None:
            break

    last = None
    for line in reversed(tail.splitlines()):
        last = parse_line_time(line)
        if last is not None:
            break
    return first, last


def _read_tail_time(sftp, path: str, *, tail_bytes: int) -> datetime | None:
    with sftp.open(path, "rb") as handle:
        size = handle.stat().st_size
        handle.seek(max(0, size - tail_bytes))
        tail = handle.read(min(size, tail_bytes))
    for line in reversed(tail.splitlines()):
        value = parse_line_time(line)
        if value is not None:
            return value
    return None


def _parse_find_output(raw: bytes) -> list[RemoteFileStat]:
    result: list[RemoteFileStat] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            inode_raw, size_raw, mtime_raw, path_raw = record.split(b"\t", 3)
            path = path_raw.decode("utf-8", errors="replace")
            result.append(
                RemoteFileStat(
                    path=path,
                    name=PurePosixPath(path).name,
                    inode=int(inode_raw),
                    size=int(size_raw),
                    mtime=mtime_raw.decode("ascii", errors="ignore").strip(),
                )
            )
        except (ValueError, TypeError):
            logger.warning("log.file_index.bad_find_record record=%r", record[:200])
    return result


def _candidate_expression(fms: Iterable[str], match_rules: Iterable[str] | None = None) -> str:
    rules = set(match_rules or ("fm", "fm_timestamp", "archive"))
    clauses: list[str] = []
    for fm in sorted(set(fms)):
        if "fm" in rules:
            clauses.append(f"-iname {shlex.quote(fm + '.log')}")
        if "fm_timestamp" in rules:
            clauses.append(f"-iname {shlex.quote(fm + '_*.log')}")
    if "archive" in rules:
        # Normal debug archives are FM-scoped: <fm>_YYYYMMDD.tar.gz.
        # Never pull unrelated FM archives from the same subsystem directory.
        for fm in sorted(set(fms)):
            clauses.append(f"-iname {shlex.quote(fm + '_*.tar.gz')}")
    return " -o ".join(clauses) or "-false"


def _name_is_candidate(name: str, fms: Iterable[str], match_rules: Iterable[str] | None = None) -> bool:
    """Match physical log names case-insensitively.

    The global module dictionary is merged with ``__iexact`` semantics, while
    Linux filenames are case-sensitive. A discovered ``spwsp.log`` can
    therefore legitimately be selected as ``SPWSP`` in the UI. Physical file
    resolution must follow the dictionary semantics or the current live file
    is silently missed.
    """
    rules = set(match_rules or ("fm", "fm_timestamp", "archive"))
    lowered = name.casefold()
    fm_values = [str(fm).strip() for fm in fms if str(fm).strip()]
    if lowered.endswith(".tar.gz"):
        if "archive" not in rules:
            return False
        return any(lowered.startswith(f"{fm.casefold()}_") for fm in fm_values)
    for fm in fm_values:
        prefix = fm.casefold()
        if "fm" in rules and lowered == f"{prefix}.log":
            return True
        if "fm_timestamp" in rules and lowered.startswith(f"{prefix}_") and lowered.endswith(".log"):
            return True
    return False


def _match_direct_fm(name: str, fms: Iterable[str], reference: datetime | None) -> tuple[str, datetime | None] | None:
    """Resolve a physical filename to the requested FM ignoring case.

    Return the requested/canonical FM spelling so cache identity, progress and
    UI metadata remain stable even when the remote filesystem uses a different
    case.
    """
    fm_values = [str(fm).strip() for fm in fms if str(fm).strip()]
    by_fold = {fm.casefold(): fm for fm in fm_values}
    parsed = parse_log_name(name, reference)
    if parsed:
        canonical = by_fold.get(parsed[0].casefold())
        if canonical is not None:
            return canonical, parsed[1]
    if not name.lower().endswith(".log"):
        return None
    stem = name[:-4]
    stem_fold = stem.casefold()
    for fm in sorted(fm_values, key=len, reverse=True):
        fm_fold = fm.casefold()
        if stem_fold == fm_fold:
            return fm, None
        if stem_fold.startswith(f"{fm_fold}_"):
            # Unknown/legacy suffix: metadata mtime will be used as the cheap
            # inferred close boundary instead of opening the file.
            return fm, None
    return None


def _window_days(start: datetime, end: datetime, *, include_next_day: bool = False) -> list[datetime]:
    """Return midnight datetimes for the requested calendar days.

    A one-day look-ahead is useful for close-boundary rotated logs: a query near
    midnight can be closed by the first rotated file on the following day.
    """
    day = datetime.combine(start.date(), datetime.min.time())
    last = datetime.combine(end.date(), datetime.min.time())
    if include_next_day:
        last += timedelta(days=1)
    result: list[datetime] = []
    while day <= last:
        result.append(day)
        day += timedelta(days=1)
    return result


def _window_candidate_expression(
    fms: Iterable[str],
    *,
    start: datetime,
    end: datetime,
    match_rules: Iterable[str] | None = None,
) -> str:
    """Build a time-scoped filename expression for first-hit discovery.

    Production naming is authoritative:
      - current: ``fm.log``
      - rotated: ``fm_YYYYMMDD*.log``
      - daily archive: ``fm_YYYYMMDD.tar.gz``

    This deliberately does *not* enumerate ``fm_*.tar.gz`` across the whole
    history.  Only exact archive days intersecting the requested window are
    admitted.  Rotated logs include the following day because their suffix is a
    close boundary and can close a segment that started before midnight.
    """
    rules = set(match_rules or ("fm", "fm_timestamp", "archive"))
    fm_set = sorted(set(fms))
    clauses: list[str] = []
    query_days = _window_days(start, end)
    rotated_days = _window_days(start, end, include_next_day=True)
    for fm in fm_set:
        if "fm" in rules:
            clauses.append(f"-iname {shlex.quote(fm + '.log')}")
        if "fm_timestamp" in rules:
            for day in rotated_days:
                clauses.append(f"-iname {shlex.quote(f'{fm}_{day:%Y%m%d}*.log')}")
        if "archive" in rules:
            for day in query_days:
                clauses.append(f"-iname {shlex.quote(f'{fm}_{day:%Y%m%d}.tar.gz')}")
    return " -o ".join(clauses) or "-false"


def discover_window_candidate_stats(
    client,
    directory: str,
    fms: Iterable[str],
    *,
    start: datetime,
    end: datetime,
    sftp=None,
    match_rules: Iterable[str] | None = None,
) -> list[RemoteFileStat]:
    """Discover only files whose *names* can cover the requested window.

    This is the normal/debug fast path used on a cold cache.  It avoids the old
    first-search behavior where every historical ``fm_*.tar.gz`` metadata entry
    was collected before later pruning.  No archive is opened here.
    """
    fm_set = set(fms)
    expr = _window_candidate_expression(
        fm_set, start=start, end=end, match_rules=match_rules,
    )
    command = (
        f"find {shlex.quote(directory)} -maxdepth 1 -type f \\( {expr} \\) "
        r"-printf '%i\t%s\t%T@\t%p\0'"
    )
    logger.info(
        "log.file_index.window_find.start directory=%s fms=%s start=%s end=%s",
        directory, sorted(fm_set), start, end,
    )
    _, stdout, stderr = client.exec_command(command, timeout=45)
    raw = stdout.read()
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code == 0:
        result = _parse_find_output(raw)
        logger.info(
            "log.file_index.window_find.finish directory=%s files=%d mode=find_printf",
            directory, len(result),
        )
        return result

    if _find_output_missing_directory(error):
        logger.info("log.file_index.window_find.skip_missing directory=%s error=%s", directory, error.strip())
        return []
    if sftp is None:
        raise SshOperationError(
            f"读取时间窗口日志文件指纹失败 {directory}：{error.strip() or f'exit={code}'}"
        )

    # Fallback remains a single directory metadata listing.  Filtering is still
    # filename-only and never opens tar contents.
    allowed_names: set[str] = set()
    rules = set(match_rules or ("fm", "fm_timestamp", "archive"))
    query_days = _window_days(start, end)
    rotated_days = _window_days(start, end, include_next_day=True)
    for fm in fm_set:
        if "fm" in rules:
            allowed_names.add(f"{fm}.log".casefold())
        if "archive" in rules:
            allowed_names.update(f"{fm}_{day:%Y%m%d}.tar.gz".casefold() for day in query_days)
    rotated_prefixes = tuple(
        f"{fm}_{day:%Y%m%d}".casefold()
        for fm in fm_set for day in rotated_days
    ) if "fm_timestamp" in rules else tuple()
    result: list[RemoteFileStat] = []
    try:
        for child in sftp.listdir_attr(directory):
            if not statmod.S_ISREG(child.st_mode):
                continue
            name = child.filename
            lowered_name = name.casefold()
            if lowered_name not in allowed_names and not (
                lowered_name.endswith(".log") and rotated_prefixes and lowered_name.startswith(rotated_prefixes)
            ):
                continue
            result.append(RemoteFileStat(
                path=f"{directory.rstrip('/')}/{name}",
                name=name,
                inode=int(getattr(child, "st_ino", 0) or 0),
                size=int(child.st_size),
                mtime=str(int(child.st_mtime)),
            ))
    except OSError as exc:
        if _is_missing_directory_error(exc):
            logger.info("log.file_index.window_find.skip_missing directory=%s error=%s", directory, exc)
            return []
        raise SshOperationError(f"SFTP 读取日志目录失败 {directory}：{exc}") from exc
    logger.info(
        "log.file_index.window_find.finish directory=%s files=%d mode=sftp_listdir_attr",
        directory, len(result),
    )
    return result

def discover_candidate_stats(client, directory: str, fms: Iterable[str], sftp=None, match_rules: Iterable[str] | None = None) -> list[RemoteFileStat]:
    fm_set = set(fms)
    expr = _candidate_expression(fm_set, match_rules)
    command = (
        f"find {shlex.quote(directory)} -maxdepth 1 -type f \\( {expr} \\) "
        r"-printf '%i\t%s\t%T@\t%p\0'"
    )
    logger.info("log.file_index.find.start directory=%s fms=%s", directory, sorted(fm_set))
    _, stdout, stderr = client.exec_command(command, timeout=45)
    raw = stdout.read()
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code == 0:
        result = _parse_find_output(raw)
        logger.info("log.file_index.find.finish directory=%s files=%d mode=find_printf", directory, len(result))
        return result

    if _find_output_missing_directory(error):
        logger.info("log.file_index.find.skip_missing directory=%s error=%s", directory, error.strip())
        return []
    if sftp is None:
        raise SshOperationError(f"读取日志文件指纹失败 {directory}：{error.strip() or f'exit={code}'}")

    # BusyBox/trimmed find variants may not support -printf. One SFTP directory
    # listing is still a bounded metadata operation and avoids per-file stat RTTs.
    logger.warning(
        "log.file_index.find_printf_unsupported directory=%s error=%s fallback=sftp_listdir_attr",
        directory, error.strip() or f"exit={code}",
    )
    result: list[RemoteFileStat] = []
    try:
        for child in sftp.listdir_attr(directory):
            if not statmod.S_ISREG(child.st_mode) or not _name_is_candidate(child.filename, fm_set, match_rules):
                continue
            path = f"{directory.rstrip('/')}/{child.filename}"
            result.append(
                RemoteFileStat(
                    path=path,
                    name=child.filename,
                    inode=int(getattr(child, "st_ino", 0) or 0),
                    size=int(child.st_size),
                    mtime=str(int(child.st_mtime)),
                )
            )
    except OSError as exc:
        if _is_missing_directory_error(exc):
            logger.info("log.file_index.find.skip_missing directory=%s error=%s", directory, exc)
            return []
        raise SshOperationError(f"SFTP 读取日志目录失败 {directory}：{exc}") from exc
    logger.info("log.file_index.find.finish directory=%s files=%d mode=sftp_listdir_attr", directory, len(result))
    return result


def _match_tar_fm(name: str, fms: Iterable[str], reference: datetime | None) -> tuple[str, datetime | None] | None:
    if not name.lower().endswith(".tar.gz"):
        return None
    stem = name[:-7]
    stem_fold = stem.casefold()
    for fm in sorted({str(item).strip() for item in fms if str(item).strip()}, key=len, reverse=True):
        prefix = f"{fm}_"
        prefix_fold = prefix.casefold()
        if stem_fold.startswith(prefix_fold):
            return fm, parse_archive_timestamp(stem[len(prefix):], reference)
    return None


def _parse_tar_boundary(name: str, reference: datetime | None) -> datetime | None:
    if not name.endswith(".tar.gz"):
        return None
    stem = name[:-7]
    direct = parse_archive_timestamp(stem, reference)
    if direct is not None:
        return direct
    # Generic names such as debug_080102.tar.gz / archive-202608070930.tar.gz.
    tokens = re.findall(r"\d{6,17}", stem)
    for token in reversed(tokens):
        parsed = parse_archive_timestamp(token, reference)
        if parsed is not None:
            return parsed
    return None

def _executor_log_identity(name: str, reference: datetime | None) -> tuple[str, datetime | None] | None:
    """Return an executor source identity without assuming any FM naming rule.

    Executor files are arbitrary names such as ``rspme_cp_xx.log``. Archived
    variants commonly append a timestamp, but the executor prefix itself is not
    used as a filter. A pure timestamp filename is also accepted.
    """
    basename = PurePosixPath(name).name
    if not basename.endswith(".log"):
        return None
    stem = basename[:-4]
    pure_stamp = parse_archive_timestamp(stem, reference)
    if pure_stamp is not None:
        return "executor", pure_stamp
    parsed = parse_log_name(basename, reference)
    if parsed:
        identity, boundary = parsed
        return identity or "executor", boundary
    return stem or "executor", None


def executor_module_name(name: str, reference: datetime | None = None) -> str | None:
    """Return module part from ``<executor>_cp_xx[stamp].log``."""
    parsed = _executor_log_identity(name, reference)
    if not parsed:
        return None
    identity, _boundary = parsed
    match = re.match(r"^(?P<module>.+?)_cp(?:_|$)", identity, re.IGNORECASE)
    if not match:
        return None
    module = match.group("module").strip("_- ")
    return module or None


def _run_log_identity(name: str, reference: datetime | None) -> tuple[str, datetime | None] | None:
    """Recognize flat run logs without subsystem/FM assumptions.

    Supported current/archives:
    - event.log
    - event_<timestamp>.log
    - <timestamp>.log
    Pure timestamp variants are accepted because deployed run-log rotation may
    drop the ``event_`` prefix while keeping the same archive semantics.
    """
    basename = PurePosixPath(name).name
    if not basename.endswith(".log"):
        return None
    stem = basename[:-4]
    if stem == "event":
        return "event", None
    event_archive = re.match(r"^event[_\-.]?(?P<stamp>\d[\dT_:\-.]*)$", stem)
    if event_archive:
        boundary = parse_archive_timestamp(event_archive.group("stamp"), reference)
        if boundary is not None:
            return "event", boundary
    boundary = parse_archive_timestamp(stem, reference)
    if boundary is not None:
        return "event", boundary
    return None


def _run_archive_boundary(name: str, reference: datetime | None) -> datetime | None:
    if not name.endswith(".tar.gz"):
        return None
    stem = name[:-7]
    event_archive = re.match(r"^event[_\-.]?(?P<stamp>\d[\dT_:\-.]*)$", stem)
    if event_archive:
        boundary = parse_archive_timestamp(event_archive.group("stamp"), reference)
        if boundary is not None:
            return boundary
    return parse_archive_timestamp(stem, reference)


def discover_run_window_candidate_stats(
    client, directory: str, *, start: datetime, end: datetime, sftp=None
) -> list[RemoteFileStat]:
    """Cold-cache run-log discovery limited to the requested calendar window."""
    day_tokens = [day.strftime("%Y%m%d") for day in _window_days(start, end, include_next_day=True)]
    clauses = ["-name 'event.log'"]
    for token in day_tokens:
        clauses.extend([
            f"-name 'event_{token}*.log'",
            f"-name '{token}*.log'",
            f"-name '*{token}*.tar.gz'",
        ])
    expr = " -o ".join(clauses)
    command = (
        f"find {shlex.quote(directory)} -maxdepth 1 -type f \\( {expr} \\) "
        r"-printf '%i\t%s\t%T@\t%p\0'"
    )
    logger.info("log.run.window_find.start directory=%s start=%s end=%s", directory, start, end)
    _, stdout, stderr = client.exec_command(command, timeout=45)
    raw = stdout.read()
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code == 0:
        result = [
            item for item in _parse_find_output(raw)
            if _run_log_identity(item.name, None) is not None or _run_archive_boundary(item.name, None) is not None
        ]
        logger.info("log.run.window_find.finish directory=%s files=%d mode=find_printf", directory, len(result))
        return result
    if sftp is None:
        raise SshOperationError(f"读取运行日志时间窗口指纹失败 {directory}：{error.strip() or f'exit={code}'}")
    result: list[RemoteFileStat] = []
    try:
        children = sftp.listdir_attr(directory)
    except OSError as exc:
        logger.warning("log.run.directory_unavailable directory=%s error=%s", directory, exc)
        return []
    for child in children:
        if not statmod.S_ISREG(child.st_mode):
            continue
        name = child.filename
        if name != "event.log" and not any(token in name for token in day_tokens):
            continue
        if _run_log_identity(name, None) is None and _run_archive_boundary(name, None) is None:
            continue
        result.append(RemoteFileStat(
            path=f"{directory.rstrip('/')}/{name}", name=name,
            inode=int(getattr(child, "st_ino", 0) or 0), size=int(child.st_size),
            mtime=str(int(child.st_mtime)),
        ))
    logger.info("log.run.window_find.finish directory=%s files=%d mode=sftp_listdir_attr", directory, len(result))
    return result


def discover_run_candidate_stats(client, directory: str, sftp=None) -> list[RemoteFileStat]:
    """Collect metadata only for flat run event logs in one run directory."""
    command = (
        f"find {shlex.quote(directory)} -maxdepth 1 -type f "
        r"\( -name 'event.log' -o -name 'event_*.log' -o -name 'event_*.tar.gz' -o -name '*.log' -o -name '*.tar.gz' \) "
        r"-printf '%i\t%s\t%T@\t%p\0'"
    )
    logger.info("log.run.find.start directory=%s", directory)
    _, stdout, stderr = client.exec_command(command, timeout=45)
    raw = stdout.read()
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code == 0:
        result = [
            item for item in _parse_find_output(raw)
            if _run_log_identity(item.name, None) is not None or _run_archive_boundary(item.name, None) is not None
        ]
        logger.info("log.run.find.finish directory=%s files=%d mode=find_printf", directory, len(result))
        return result
    if sftp is None:
        raise SshOperationError(f"读取运行日志文件指纹失败 {directory}：{error.strip() or f'exit={code}'}")
    result: list[RemoteFileStat] = []
    try:
        children = sftp.listdir_attr(directory)
    except OSError as exc:
        logger.warning("log.run.directory_unavailable directory=%s error=%s", directory, exc)
        return []
    for child in children:
        if not statmod.S_ISREG(child.st_mode):
            continue
        if _run_log_identity(child.filename, None) is None and _run_archive_boundary(child.filename, None) is None:
            continue
        result.append(RemoteFileStat(
            path=f"{directory.rstrip('/')}/{child.filename}",
            name=child.filename,
            inode=int(getattr(child, "st_ino", 0) or 0),
            size=int(child.st_size),
            mtime=str(int(child.st_mtime)),
        ))
    logger.info("log.run.find.finish directory=%s files=%d mode=sftp_listdir_attr", directory, len(result))
    return result


def discover_executor_candidate_stats(
    client, directories: Iterable[str], sftp=None, *,
    modules: Iterable[str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[RemoteFileStat]:
    """Collect executor metadata with module/date pruning when a window is known.

    Current and rotated direct logs are module-prefixed, while daily tar names
    are commonly date-based.  On a cold search this keeps months of unrelated
    tar files out of even the metadata/fingerprint stage.
    """
    unique_dirs = [item.rstrip("/") for item in dict.fromkeys(directories) if item]
    if not unique_dirs:
        return []
    module_set = {str(item or "").strip() for item in (modules or []) if str(item or "").strip()}
    day_tokens = (
        [day.strftime("%Y%m%d") for day in _window_days(window_start, window_end, include_next_day=True)]
        if window_start is not None and window_end is not None else []
    )
    clauses: list[str] = []
    if module_set:
        for module_name in sorted(module_set):
            clauses.append(f"-name {shlex.quote(module_name + '_cp*.log')}")
    else:
        clauses.append("-name '*.log'")
    if day_tokens:
        for token in day_tokens:
            clauses.append(f"-name '*{token}*.tar.gz'")
    quoted = " ".join(shlex.quote(item) for item in unique_dirs)
    expr = " -o ".join(clauses)
    command = (
        f"find {quoted} -maxdepth 1 -type f \\( {expr} \\) "
        r"-printf '%i\t%s\t%T@\t%p\0'"
    )
    logger.info(
        "log.executor.find.start directories=%d modules=%s days=%s",
        len(unique_dirs), sorted(module_set), day_tokens,
    )
    _, stdout, stderr = client.exec_command(command, timeout=60)
    raw = stdout.read()
    _ = stderr.read()
    code = stdout.channel.recv_exit_status()
    if code == 0:
        result = _parse_find_output(raw)
        logger.info("log.executor.find.finish directories=%d files=%d mode=find_printf", len(unique_dirs), len(result))
        return result

    if sftp is None:
        raise SshOperationError("读取执行器日志文件指纹失败。")

    result: list[RemoteFileStat] = []
    for directory in unique_dirs:
        try:
            children = sftp.listdir_attr(directory)
        except OSError:
            continue
        for child in children:
            if not statmod.S_ISREG(child.st_mode):
                continue
            name = child.filename
            if name.endswith(".log"):
                module_name = executor_module_name(name, window_start)
                if module_set and module_name not in module_set:
                    continue
            elif name.endswith(".tar.gz"):
                if day_tokens and not any(token in name for token in day_tokens):
                    continue
            else:
                continue
            result.append(RemoteFileStat(
                path=f"{directory}/{name}", name=name,
                inode=int(getattr(child, "st_ino", 0) or 0), size=int(child.st_size),
                mtime=str(int(child.st_mtime)),
            ))
    logger.info("log.executor.find.finish directories=%d files=%d mode=sftp_listdir_attr", len(unique_dirs), len(result))
    return result


def _interval_overlaps(
    candidate_start: datetime | None,
    candidate_end: datetime | None,
    query_start: datetime,
    query_end: datetime,
) -> bool:
    left = candidate_start or datetime.min
    right = candidate_end or datetime.max
    return left <= query_end and right >= query_start


def _indexed_range_overlaps(index: dict, query_start: datetime, query_end: datetime) -> bool:
    """Return True when a cached/probed real log-time range overlaps the query.

    Missing first/last timestamps are treated conservatively as a match so an
    unusual log format can never be silently discarded by metadata heuristics.
    """
    first = _dt_load(index.get("start_time"))
    last = _dt_load(index.get("end_time"))
    if first is None or last is None:
        return True
    return first <= query_end and last >= query_start


@dataclass(frozen=True, slots=True)
class PhysicalCandidate:
    stat: RemoteFileStat
    boundary: datetime | None
    kind: str
    fms: frozenset[str]


def _is_daily_tar_candidate(item: PhysicalCandidate) -> bool:
    if item.kind != "tar" or item.boundary is None:
        return False
    # Standard daily archives may be FM-prefixed (rspmp_20260801.tar.gz),
    # run-log prefixed (event_20260801.tar.gz), or a bare date
    # (20260801.tar.gz).  The four-digit year keeps legacy MMDDHHMM names
    # from being mistaken for calendar-day ownership.
    return bool(re.search(r"(?:^|[_\-.])(?:19|20)\d{6}\.tar\.gz$", item.stat.name))


def _select_boundary_archives(
    items: list[PhysicalCandidate], start: datetime, end: datetime
) -> list[PhysicalCandidate]:
    """Select rotated files by filename close-boundary semantics.

    ``fm_YYYYMMDDHHMMSSmmm.log`` is the file closed at that timestamp, so it
    contains the interval after the previous close boundary and through its own
    boundary. For a query we therefore need the first boundary >= query start,
    then continue only until one boundary covers query end.
    """
    ordered = sorted((item for item in items if item.boundary is not None), key=lambda item: item.boundary)
    selected: list[PhysicalCandidate] = []
    for item in ordered:
        boundary = item.boundary
        if boundary is None or boundary < start:
            continue
        selected.append(item)
        if boundary >= end:
            break
    return selected


def _select_physical_candidates(
    candidates: list[PhysicalCandidate],
    *,
    fms: set[str],
    start: datetime,
    end: datetime,
) -> list[PhysicalCandidate]:
    """Use the production filename convention as the authoritative fast index.

    Rules for normal debug logs:
    - ``fm_YYYYMMDD.tar.gz`` covers that calendar day.
    - ``fm_YYYYMMDDHHMMSSmmm.log`` is a rotated file whose suffix is its close
      boundary; only the boundary files required to cover the query are kept.
    - ``fm.log`` is the current, not-yet-archived interval and is considered only
      when the query extends beyond the newest archive boundary.

    No unrelated timestamped archive is opened merely to discover its real log
    range. The final stream still filters each returned line by the exact query
    timestamps.
    """
    relevant = [item for item in candidates if item.fms & fms]
    selected: list[PhysicalCandidate] = []

    for fm in sorted(fms):
        group = [item for item in relevant if fm in item.fms]
        currents = [item for item in group if item.kind == "current"]
        daily_tars = [item for item in group if _is_daily_tar_candidate(item)]
        rotated_logs = [item for item in group if item.kind == "archived" and item.boundary is not None]
        other_archives = [
            item for item in group
            if item.kind == "tar" and item not in daily_tars and item.boundary is not None
        ]

        # Daily tar names are exact calendar coverage, so only intersecting days
        # can possibly contain the requested timestamps.
        matching_tar_days = [
            item for item in daily_tars
            if item.boundary is not None and start.date() <= item.boundary.date() <= end.date()
        ]
        selected.extend(matching_tar_days)

        # Loose rotated logs are needed only for query-day segments that do not
        # already have an authoritative daily tar. Split the requested window by
        # calendar day so a next-day rotation is not pulled for a day already
        # covered by fm_YYYYMMDD.tar.gz.
        tar_days = {item.boundary.date() for item in matching_tar_days if item.boundary is not None}
        loose = [item for item in rotated_logs if item.boundary is not None]
        day = start.date()
        while day <= end.date():
            if day not in tar_days:
                day_start = datetime.combine(day, datetime.min.time())
                day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
                segment_start = max(start, day_start)
                segment_end = min(end, day_end)
                selected.extend(_select_boundary_archives(loose, segment_start, segment_end))
            day += timedelta(days=1)

        # Legacy FM-prefixed tar names with a parseable non-daily timestamp keep
        # the same close-boundary fallback, but no generic/unrelated tar is ever
        # admitted at discovery time.
        selected.extend(_select_boundary_archives(other_archives, start, end))

        archive_ends: list[datetime] = []
        archive_ends.extend(item.boundary for item in rotated_logs if item.boundary is not None)
        archive_ends.extend(item.boundary for item in other_archives if item.boundary is not None)
        # A YYYYMMDD tar owns the full named day; its effective end is next-day midnight.
        archive_ends.extend(
            item.boundary + timedelta(days=1)
            for item in daily_tars if item.boundary is not None
        )
        latest_archive_end = max(archive_ends, default=None)
        if currents and (latest_archive_end is None or end > latest_archive_end):
            selected.append(currents[0])

    deduped = {item.stat.path: item for item in selected}
    return sorted(
        deduped.values(),
        key=lambda item: (item.boundary or datetime.max, 1 if item.kind == "current" else 0, item.stat.path),
    )


def _select_tar_members_for_window(
    members: list[dict], *, fms: set[str], start: datetime, end: datetime
) -> list[dict]:
    """Select archive members from their own rotated-log filename timestamps."""
    selected: list[dict] = []
    for fm in sorted(fms):
        group = [item for item in members if str(item.get("fm") or "") == fm]
        boundary_items = [
            (item, _dt_load(item.get("boundary_time")))
            for item in group
            if _dt_load(item.get("boundary_time")) is not None
        ]
        boundary_items.sort(key=lambda pair: pair[1])
        fm_selected: list[dict] = []
        for item, boundary in boundary_items:
            if boundary < start:
                continue
            fm_selected.append(item)
            if boundary >= end:
                break
        # A boundary-less fm.log inside a daily tar is the final unrotated
        # segment captured by that archive. Include it only when no timestamped
        # member reaches the query end.
        covered_end = bool(fm_selected and _dt_load(fm_selected[-1].get("boundary_time")) >= end)
        if not covered_end:
            fm_selected.extend(item for item in group if _dt_load(item.get("boundary_time")) is None)
        selected.extend(fm_selected)
    deduped = {(str(item.get("fm") or ""), str(item.get("member_name") or "")): item for item in selected}
    return list(deduped.values())


_ARCHIVE_CHAIN_SEPARATOR = "::"
_DEFAULT_ARCHIVE_MAX_NESTED_DEPTH = 3


def _archive_max_nested_depth() -> int:
    try:
        return max(0, min(8, int(getattr(settings, "TRACELENS_ARCHIVE_MAX_NESTED_DEPTH", _DEFAULT_ARCHIVE_MAX_NESTED_DEPTH))))
    except (TypeError, ValueError):
        return _DEFAULT_ARCHIVE_MAX_NESTED_DEPTH


def _encode_archive_member_chain(parts: Iterable[str]) -> str:
    return _ARCHIVE_CHAIN_SEPARATOR.join(str(item).strip() for item in parts if str(item).strip())


def _tar_extract_pipeline(path: str, archive_chain: Iterable[str]) -> str:
    chain = [str(item).strip() for item in archive_chain if str(item).strip()]
    if not chain:
        raise ValueError("archive_chain 不能为空")
    command = f"tar -xOzf {shlex.quote(path)} {shlex.quote(chain[0])}"
    for member in chain[1:]:
        command += f" | tar -xOzf - {shlex.quote(member)}"
    return command


def _list_tar_members(client, path: str, archive_chain: Iterable[str] = ()) -> list[str]:
    chain = [str(item).strip() for item in archive_chain if str(item).strip()]
    if chain:
        command = f"{_tar_extract_pipeline(path, chain)} | tar -tzf -"
        display_path = f"{path}{_ARCHIVE_CHAIN_SEPARATOR}{_encode_archive_member_chain(chain)}"
    else:
        command = f"tar -tzf {shlex.quote(path)}"
        display_path = path
    _, stdout, stderr = client.exec_command(command, timeout=120)
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise SshOperationError(f"读取压缩包目录失败 {display_path}：{error.strip() or f'exit={code}'}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def _walk_nested_tar_members(
    client, path: str, *, max_nested_depth: int | None = None, operation_id: str = ""
) -> list[tuple[tuple[str, ...], str]]:
    """Return every file member plus its containing nested-archive chain.

    ``chain`` contains only archive members.  A direct outer member has an
    empty chain.  For ``outer.tar.gz -> inner.tar.gz -> RSRCP_x.log`` the
    returned item is ``(("inner.tar.gz",), "RSRCP_x.log")``.  Directory
    entries are ignored.  Recursion is deliberately bounded so a malformed or
    unexpectedly deep archive cannot make a query unbounded.
    """
    limit = _archive_max_nested_depth() if max_nested_depth is None else max(0, int(max_nested_depth))
    results: list[tuple[tuple[str, ...], str]] = []
    visited_archives: set[tuple[str, ...]] = set()

    def visit(chain: tuple[str, ...]) -> None:
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        if chain in visited_archives:
            return
        visited_archives.add(chain)
        members = _list_tar_members(client, path, chain)
        for member in members:
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            clean = str(member or "").strip()
            if not clean or clean.endswith("/"):
                continue
            if clean.lower().endswith(".tar.gz"):
                # ``limit`` is the number of nested archive layers below the
                # selected physical outer archive.  We still record no tar as a
                # log artifact; only its descendants are candidates.
                if len(chain) < limit:
                    nested_chain = (*chain, clean)
                    try:
                        visit(nested_chain)
                    except SshOperationError as exc:
                        # A member may merely have a .tar.gz suffix but be
                        # corrupted. Keep the rest of the day archive usable.
                        logger.warning(
                            "log.archive_index.nested_list_failed path=%s chain=%s error=%s",
                            path, _encode_archive_member_chain(nested_chain), exc,
                        )
                continue
            results.append((chain, clean))

    visit(())
    return results


def _probe_tar_member_range(client, archive_path: str, member: str) -> tuple[datetime | None, datetime | None]:
    # Scan on the remote host and transfer only the first/last valid log lines.
    # This is intentionally paid only when the outer tar fingerprint changes.
    awk = r'''awk '/^\[[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { if (first == "") first=$0; last=$0 } END { if (first != "") print first; if (last != "") print last }' '''
    command = (
        f"tar -xOzf {shlex.quote(archive_path)} {shlex.quote(member)} | {awk}"
    )
    _, stdout, stderr = client.exec_command(command, timeout=300)
    lines = stdout.read().splitlines()
    error = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise SshOperationError(
            f"探测压缩包成员时间失败 {archive_path}::{member}：{error.strip() or f'exit={code}'}"
        )
    parsed = [value for value in (parse_line_time(line) for line in lines) if value is not None]
    if not parsed:
        return None, None
    return parsed[0], parsed[-1]


class LogFileIndexService:
    @property
    def index_ttl(self) -> int:
        return int(getattr(settings, "TRACELENS_LOG_INDEX_TTL", 30 * 24 * 3600))

    @property
    def head_bytes(self) -> int:
        return int(getattr(settings, "TRACELENS_LOG_INDEX_HEAD_BYTES", 128 * 1024))

    @property
    def tail_bytes(self) -> int:
        return int(getattr(settings, "TRACELENS_LOG_INDEX_TAIL_BYTES", 256 * 1024))

    def _scope(self, environment, target, subsystem: str, fm: str) -> LogCacheScope:
        return LogCacheScope(
            host=target.machine.host,
            username=target.machine.username,
            source_category=target.source_category,
            subsystem=subsystem,
            fm=fm,
        )

    def _load_or_probe_direct(self, *, environment, target, lease, subsystem: str, fm: str, stat: RemoteFileStat, kind: str, operation_id: str = "") -> dict:
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        scope = self._scope(environment, target, subsystem, fm)
        key = scope.file_index_key(stat.path)
        cached = RedisLogStore.get_json(key)
        cached = cached if isinstance(cached, dict) else None
        current = kind == "current"

        if cached and cached.get("fingerprint") == stat.fingerprint:
            logger.info("log.file_index.hit path=%s fingerprint=%s", stat.path, stat.fingerprint)
            return cached

        old_inode = int(cached.get("inode", -1)) if cached else -1
        old_size = int(cached.get("size", -1)) if cached else -1
        append_only = bool(current and cached and old_inode == stat.inode and stat.size > old_size)

        if append_only:
            first_time = _dt_load(cached.get("start_time"))
            last_time = _read_tail_time(lease.sftp, stat.path, tail_bytes=self.tail_bytes)
            generation = str(cached.get("generation") or _generation_for(stat, salt="current"))
            logger.info(
                "log.file_index.current_append path=%s old_size=%d new_size=%d generation=%s",
                stat.path,
                old_size,
                stat.size,
                generation,
            )
        else:
            first_time, last_time = _read_head_tail(
                lease.sftp,
                stat.path,
                head_bytes=self.head_bytes,
                tail_bytes=self.tail_bytes,
            )
            generation = _generation_for(stat, salt="current-generation" if current else "archive")
            logger.info(
                "log.file_index.probed path=%s kind=%s start=%s end=%s generation=%s",
                stat.path,
                kind,
                first_time,
                last_time,
                generation,
            )

        payload = {
            "path": stat.path,
            "kind": kind,
            "inode": stat.inode,
            "size": stat.size,
            "mtime": stat.mtime,
            "fingerprint": stat.fingerprint,
            "generation": generation,
            "start_time": _dt_dump(first_time),
            "end_time": _dt_dump(last_time),
            "indexed_at": datetime.now().isoformat(timespec="seconds"),
            "environment_id": environment.id,
            "host": target.machine.host,
            "username": target.machine.username,
            "source_category": target.source_category,
            "subsystem": subsystem,
            "fm": fm,
        }
        RedisLogStore.set_json(key, payload, self.index_ttl)
        return payload

    def _load_or_probe_tar(
        self, *, environment, target, lease, subsystem: str, stat: RemoteFileStat,
        reference: datetime | None, fms: set[str], operation_id: str = "",
    ) -> list[dict]:
        """Cache one normal archive directly below its owning FM namespace.

        Production normal archives are FM-scoped (``<fm>_YYYYMMDD.tar.gz``), so
        the archive filename already tells us which FM owns the file.  Do not
        keep a shared archive bucket or a cross-FM bookkeeping collection:
        each FM keeps only its own archive/member index.
        """
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)

        matched_archive = _match_tar_fm(stat.name, fms, reference)
        if not matched_archive:
            logger.info("log.archive_index.skip_unmatched path=%s requested_fms=%s", stat.path, sorted(fms))
            return []
        archive_fm, _archive_boundary = matched_archive

        scope = self._scope(environment, target, subsystem, archive_fm)
        key = scope.archive_members_key(stat.path)
        cached = RedisLogStore.get_json(key)
        cached = cached if isinstance(cached, dict) else None
        if (
            cached
            and cached.get("fingerprint") == stat.fingerprint
            and cached.get("fm") == archive_fm
            and cached.get("index_mode") == "nested_filename_only_v2"
        ):
            members = [item for item in (cached.get("members") or []) if isinstance(item, dict)]
            logger.info(
                "log.archive_index.hit path=%s fm=%s members=%d mode=nested_filename_only_v2",
                stat.path, archive_fm, len(members),
            )
            return members

        generation = _generation_for(stat, salt=f"tar-nested-v2:{archive_fm}")
        members: list[dict] = []
        seen: set[str] = set()
        nested_archives: set[str] = set()
        for archive_chain, member in _walk_nested_tar_members(
            lease.client, stat.path, operation_id=operation_id
        ):
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            parsed = parse_log_name(member, reference)
            if not parsed:
                continue
            member_fm, boundary = parsed
            if member_fm != archive_fm:
                continue
            # A boundary-less ``FM.log`` inside a nested timestamp archive
            # inherits that immediate archive's close boundary.  This keeps an
            # outer daily package containing many historical inner archives
            # from treating every nested current-name log as one giant
            # boundary-less segment.
            if boundary is None and archive_chain:
                nested_match = _match_tar_fm(PurePosixPath(archive_chain[-1]).name, {archive_fm}, reference)
                if nested_match is not None:
                    _nested_fm, nested_boundary = nested_match
                    boundary = nested_boundary
            full_member_name = _encode_archive_member_chain((*archive_chain, member))
            if full_member_name in seen:
                continue
            seen.add(full_member_name)
            if archive_chain:
                nested_archives.add(_encode_archive_member_chain(archive_chain))
            members.append(
                {
                    "fm": archive_fm,
                    "member_name": full_member_name,
                    "boundary_time": _dt_dump(boundary),
                    "archive_depth": len(archive_chain),
                    "generation": f"{generation}-{hashlib.sha256(full_member_name.encode('utf-8')).hexdigest()[:10]}",
                }
            )

        payload = {
            "path": stat.path,
            "inode": stat.inode,
            "size": stat.size,
            "mtime": stat.mtime,
            "fingerprint": stat.fingerprint,
            "generation": generation,
            "fm": archive_fm,
            "members": members,
            "nested_archive_count": len(nested_archives),
            "max_nested_depth": _archive_max_nested_depth(),
            "index_mode": "nested_filename_only_v2",
            "indexed_at": datetime.now().isoformat(timespec="seconds"),
            "environment_id": environment.id,
            "host": target.machine.host,
            "username": target.machine.username,
            "source_category": target.source_category,
            "subsystem": subsystem,
        }
        RedisLogStore.set_json(key, payload, self.index_ttl)
        logger.info(
            "log.archive_index.store path=%s fm=%s members=%d nested_archives=%d mode=nested_filename_only_v2",
            stat.path, archive_fm, len(members), len(nested_archives),
        )
        return members

    def _load_or_probe_run_tar(
        self, *, environment, target, lease, stat: RemoteFileStat,
        reference: datetime | None, operation_id: str = "",
    ) -> list[dict]:
        """Index selected run-log tar members from filenames only.

        The outer archive has already passed the query-window filename filter.
        Listing its member names is enough to choose the required event segments;
        never decompress every member just to discover first/last timestamps.
        Exact line timestamps are still enforced by the final stream reader.
        """
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        subsystem = "运行日志"
        scope = self._scope(environment, target, subsystem, "event")
        key = scope.archive_members_key(stat.path)
        cached = RedisLogStore.get_json(key)
        cached = cached if isinstance(cached, dict) else None
        if (
            cached
            and cached.get("fingerprint") == stat.fingerprint
            and cached.get("mode") == "run_nested_filename_only_v2"
        ):
            members = [item for item in (cached.get("members") or []) if isinstance(item, dict)]
            logger.info("log.run.archive_index.hit path=%s members=%d mode=nested_filename_only_v2", stat.path, len(members))
            return members

        generation = _generation_for(stat, salt="run-tar-nested-v2")
        members: list[dict] = []
        seen: set[str] = set()
        nested_archives: set[str] = set()
        for archive_chain, member in _walk_nested_tar_members(
            lease.client, stat.path, operation_id=operation_id
        ):
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            parsed = _run_log_identity(member, reference)
            if not parsed:
                continue
            _identity, member_boundary = parsed
            if member_boundary is None and archive_chain:
                member_boundary = _run_archive_boundary(PurePosixPath(archive_chain[-1]).name, reference)
            full_member_name = _encode_archive_member_chain((*archive_chain, member))
            if full_member_name in seen:
                continue
            seen.add(full_member_name)
            if archive_chain:
                nested_archives.add(_encode_archive_member_chain(archive_chain))
            members.append({
                "fm": "event",
                "member_name": full_member_name,
                "boundary_time": _dt_dump(member_boundary),
                "archive_depth": len(archive_chain),
                "generation": f"{generation}-{hashlib.sha256(full_member_name.encode('utf-8')).hexdigest()[:10]}",
            })
        payload = {
            "mode": "run_nested_filename_only_v2",
            "path": stat.path,
            "inode": stat.inode,
            "size": stat.size,
            "mtime": stat.mtime,
            "fingerprint": stat.fingerprint,
            "generation": generation,
            "members": members,
            "nested_archive_count": len(nested_archives),
            "max_nested_depth": _archive_max_nested_depth(),
            "indexed_at": datetime.now().isoformat(timespec="seconds"),
            "environment_id": environment.id,
            "host": target.machine.host,
            "username": target.machine.username,
            "source_category": target.source_category,
        }
        RedisLogStore.set_json(key, payload, self.index_ttl)
        logger.info("log.run.archive_index.store path=%s members=%d nested_archives=%d mode=nested_filename_only_v2", stat.path, len(members), len(nested_archives))
        return members

    def collect_run_flat_artifacts(
        self, *, environment, target, lease, root: str, reference: datetime | None,
        window_start: datetime, window_end: datetime, operation_id: str = "",
    ) -> list[LogArtifact]:
        """Collect /run event logs with outer-file time pruning before tar work."""
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        started = time.monotonic()
        root = root.rstrip("/")
        if operation_id:
            LogSearchProgressStore.directory_started(
                operation_id, directory=root, subsystem="运行日志", modules={"event"},
                action="正在扫描运行日志目录",
            )
        stats = discover_run_window_candidate_stats(
            lease.client, root, start=window_start, end=window_end, sftp=lease.sftp
        )
        physical: list[PhysicalCandidate] = []
        for stat in stats:
            parsed = _run_log_identity(stat.name, reference)
            if parsed is not None:
                _identity, boundary = parsed
                physical.append(PhysicalCandidate(
                    stat=stat,
                    boundary=boundary,
                    kind="current" if stat.name == "event.log" else "archived",
                    fms=frozenset({"event"}),
                ))
                continue
            boundary = _run_archive_boundary(stat.name, reference)
            if boundary is not None:
                physical.append(PhysicalCandidate(stat, boundary, "tar", frozenset({"event"})))

        selected_physical = _select_physical_candidates(
            physical, fms={"event"}, start=window_start, end=window_end
        )
        if operation_id:
            LogSearchProgressStore.candidates_found(
                operation_id, directory=root, subsystem="运行日志",
                discovered=len(stats), selected=len(selected_physical),
                action="正在按时间筛选运行日志文件",
            )
            LogSearchProgressStore.add_files(
                operation_id, subsystem="运行日志", module_count=0,
                discovered=len(stats), checked=0, selected=0,
            )
        logger.info(
            "log.run.outer_select root=%s metadata_files=%d selected_physical=%d start=%s end=%s",
            root, len(stats), len(selected_physical), window_start, window_end,
        )

        artifacts: list[LogArtifact] = []
        for candidate in selected_physical:
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            stat = candidate.stat
            if candidate.kind in {"current", "archived"}:
                if candidate.kind == "current":
                    index = self._load_or_probe_direct(
                        environment=environment, target=target, lease=lease, subsystem="运行日志",
                        fm="event", stat=stat, kind="current", operation_id=operation_id,
                    )
                    overlaps = _indexed_range_overlaps(index, window_start, window_end)
                    if overlaps:
                        artifacts.append(LogArtifact(
                            target.machine.id, target.machine.name, "运行日志", "event", stat.path,
                            "current", None, size=stat.size,
                            source_category=target.source_category, source_name=target.source_name,
                            start_time=_dt_load(index.get("start_time")), end_time=_dt_load(index.get("end_time")),
                            fingerprint=stat.fingerprint, content_version=str(index.get("generation") or stat.fingerprint),
                        ))
                else:
                    # Rotated run logs are selected from the close timestamp in
                    # their filename; no first/last content probe is required.
                    overlaps = True
                    artifacts.append(LogArtifact(
                        target.machine.id, target.machine.name, "运行日志", "event", stat.path,
                        "archived", candidate.boundary, size=stat.size,
                        source_category=target.source_category, source_name=target.source_name,
                        fingerprint=stat.fingerprint, content_version=stat.fingerprint,
                    ))
                if operation_id:
                    LogSearchProgressStore.artifact_checked(
                        operation_id, count=1, selected_increment=1 if overlaps else 0
                    )
                continue

            # Only tar files that survived the outer filename-time selection are
            # allowed to list their members.  Member selection is also filename-only.
            members = self._load_or_probe_run_tar(
                environment=environment, target=target, lease=lease, stat=stat,
                reference=reference, operation_id=operation_id,
            )
            selected_members = _select_tar_members_for_window(
                members, fms={"event"}, start=window_start, end=window_end
            )
            for member in selected_members:
                member_boundary = _dt_load(member.get("boundary_time"))
                if member_boundary is None and _is_daily_tar_candidate(candidate) and candidate.boundary is not None:
                    member_boundary = candidate.boundary + timedelta(days=1)
                artifacts.append(LogArtifact(
                    target.machine.id, target.machine.name, "运行日志", "event", stat.path,
                    "tar_member", member_boundary,
                    member_name=str(member.get("member_name") or ""), size=stat.size,
                    source_category=target.source_category, source_name=target.source_name,
                    fingerprint=stat.fingerprint, content_version=str(member.get("generation") or stat.fingerprint),
                ))
            if operation_id:
                LogSearchProgressStore.artifact_checked(
                    operation_id, count=1, selected_increment=1 if selected_members else 0
                )
        logger.info(
            "log.run.collect.finish environment=%s root=%s files=%d selected_physical=%d artifacts=%d elapsed_ms=%d strategy=outer_filename_time_first",
            environment.id, root, len(stats), len(selected_physical), len(artifacts),
            int((time.monotonic() - started) * 1000),
        )
        return artifacts

    def _load_or_probe_executor_tar(
        self, *, environment, target, lease, subsystem: str, lower_ip: str,
        stat: RemoteFileStat, reference: datetime | None, modules: set[str], operation_id: str = "",
    ) -> list[dict]:
        """Cache executor archive members per lower-machine/module.

        One physical executor archive may contain many module logs.  Redis keys
        are therefore partitioned by ``lower_ip + module``.  On a cache miss we
        list the tar once, probe only members belonging to selected missing
        modules, and store one compact member list per module.
        """
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        selected_modules = {str(item or "").strip() for item in modules if str(item or "").strip()}
        if not selected_modules:
            return []

        cached_members: list[dict] = []
        missing_modules: set[str] = set()
        cache_slots: dict[str, tuple[LogCacheScope, str]] = {}
        for module_name in sorted(selected_modules):
            scope = self._scope(environment, target, subsystem, f"{lower_ip}/{module_name}")
            key = scope.archive_members_key(stat.path)
            cache_slots[module_name] = (scope, key)
            cached = RedisLogStore.get_json(key)
            cached = cached if isinstance(cached, dict) else None
            if (
                cached
                and cached.get("fingerprint") == stat.fingerprint
                and cached.get("mode") == "executor_tree_module_nested_filename_only_v2"
                and cached.get("module") == module_name
                and cached.get("lower_ip") == lower_ip
            ):
                members = [item for item in (cached.get("members") or []) if isinstance(item, dict)]
                cached_members.extend(members)
                logger.info(
                    "log.executor.archive_index.hit path=%s lower=%s module=%s members=%d mode=nested_filename_only_v2",
                    stat.path, lower_ip, module_name, len(members),
                )
            else:
                missing_modules.add(module_name)

        if not missing_modules:
            return cached_members

        generation = _generation_for(stat, salt="executor-tar-nested-v2")
        new_members: dict[str, list[dict]] = {module_name: [] for module_name in missing_modules}
        nested_archives: set[str] = set()
        for archive_chain, member in _walk_nested_tar_members(
            lease.client, stat.path, operation_id=operation_id
        ):
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            parsed = _executor_log_identity(member, reference)
            if not parsed:
                continue
            identity, member_boundary = parsed
            module_name = executor_module_name(member, reference)
            if not module_name or module_name not in missing_modules:
                continue
            if member_boundary is None and archive_chain:
                member_boundary = _parse_tar_boundary(PurePosixPath(archive_chain[-1]).name, reference)
            full_member_name = _encode_archive_member_chain((*archive_chain, member))
            if archive_chain:
                nested_archives.add(_encode_archive_member_chain(archive_chain))
            new_members[module_name].append({
                "fm": module_name,
                "executor": identity,
                "module": module_name,
                "member_name": full_member_name,
                "boundary_time": _dt_dump(member_boundary),
                "archive_depth": len(archive_chain),
                "generation": f"{generation}-{hashlib.sha256(full_member_name.encode('utf-8')).hexdigest()[:10]}",
            })

        indexed_at = datetime.now().isoformat(timespec="seconds")
        for module_name in sorted(missing_modules):
            _scope, key = cache_slots[module_name]
            members = new_members[module_name]
            payload = {
                "mode": "executor_tree_module_nested_filename_only_v2",
                "path": stat.path,
                "inode": stat.inode,
                "size": stat.size,
                "mtime": stat.mtime,
                "fingerprint": stat.fingerprint,
                "generation": generation,
                "module": module_name,
                "members": members,
                "nested_archive_count": len(nested_archives),
                "max_nested_depth": _archive_max_nested_depth(),
                "indexed_at": indexed_at,
                "environment_id": environment.id,
                "host": target.machine.host,
                "username": target.machine.username,
                "source_category": target.source_category,
                "subsystem": subsystem,
                "lower_ip": lower_ip,
            }
            RedisLogStore.set_json(key, payload, self.index_ttl)
            cached_members.extend(members)
            logger.info(
                "log.executor.archive_index.store path=%s lower=%s module=%s members=%d mode=nested_filename_only_v2",
                stat.path, lower_ip, module_name, len(members),
            )
        return cached_members

    def collect_executor_tree_artifacts(
        self, *, environment, target, lease, root: str, subsystems: set[str],
        modules_by_subsystem: dict[str, set[str]], reference: datetime | None, window_start: datetime, window_end: datetime,
        operation_id: str = "",
    ) -> list[LogArtifact]:
        """Collect selected executor modules under ``elog/<lower-ip>/<subsystem>``."""
        if not subsystems:
            return []
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        started = time.monotonic()
        root = root.rstrip("/")
        # Executor logs are grouped by lower-machine folder on the upper
        # machine.  The environment topology is the source of truth: check
        # every active lower machine instead of scanning whatever IP folders
        # happen to exist below elog.
        lower_dirs = sorted({
            str(relation.target_machine.host or "").strip()
            for relation in environment.machine_relations.select_related("target_machine").filter(
                is_active=True, target_machine__is_active=True
            )
            if str(relation.target_machine.host or "").strip()
            and str((relation.metadata or {}).get("station_name") or relation.target_machine.station_name or relation.target_machine.name or "").strip().lower() != "dhh"
            and str((relation.metadata or {}).get("station_type") or relation.target_machine.station_type or "").strip().upper() != "DHH"
        })
        if not lower_dirs:
            logger.info("log.executor.collect.skip_no_lower_machine environment=%s root=%s", environment.id, root)
            return []

        # Only selected subsystem directories become file-search roots. Every
        # configured lower-machine folder is checked independently; a missing
        # folder/subsystem on one lower machine never prevents the others from
        # being searched.
        directory_context: dict[str, tuple[str, str]] = {}
        for lower_index, lower_ip in enumerate(lower_dirs, start=1):
            lower_root = f"{root}/{lower_ip}"
            if operation_id:
                LogSearchProgressStore.directory_started(
                    operation_id, directory=lower_root, subsystem="执行器日志",
                    modules={fm for values in modules_by_subsystem.values() for fm in values},
                    action=f"正在检查执行器目录 {lower_index}/{len(lower_dirs)}",
                )
            try:
                subsystem_items = lease.sftp.listdir_attr(lower_root)
            except OSError as exc:
                logger.info(
                    "log.executor.lower_folder_missing environment=%s lower=%s path=%s error=%s",
                    environment.id, lower_ip, lower_root, exc,
                )
                continue
            available = {
                item.filename for item in subsystem_items
                if statmod.S_ISDIR(item.st_mode) and item.filename in subsystems
            }
            logger.info(
                "log.executor.lower_folder.ready environment=%s lower=%s selected_subsystems=%s",
                environment.id, lower_ip, sorted(available),
            )
            for subsystem in sorted(available):
                directory_context[f"{lower_root}/{subsystem}"] = (lower_ip, subsystem)

        if operation_id:
            LogSearchProgressStore.directory_started(
                operation_id, directory=root, subsystem="执行器日志",
                modules={fm for values in modules_by_subsystem.values() for fm in values},
                action=f"正在扫描 {len(directory_context)} 个执行器模块目录",
            )
        stats = discover_executor_candidate_stats(
            lease.client, directory_context.keys(), lease.sftp,
            modules={fm for values in modules_by_subsystem.values() for fm in values},
            window_start=window_start, window_end=window_end,
        )
        logger.info(
            "log.executor.collect.start environment=%s root=%s lower_dirs=%d selected_subsystems=%s selected_modules=%s files=%d",
            environment.id, root, len(lower_dirs), sorted(subsystems),
            {key: sorted(value) for key, value in sorted(modules_by_subsystem.items())}, len(stats),
        )
        if operation_id:
            LogSearchProgressStore.add_files(
                operation_id, subsystem="执行器日志", module_count=sum(len(items) for items in modules_by_subsystem.values()),
                discovered=len(stats), checked=0, selected=0,
            )

        # Prune executor files by filename time *before* any log-content probe or
        # tar member listing.  This is especially important on a cold cache: a
        # directory may contain months of tar files, but a five-minute query may
        # touch only one daily archive.
        selected_direct_paths: set[str] = set()
        selected_tar_paths: set[str] = set()
        for directory, (_lower_ip, directory_subsystem) in directory_context.items():
            directory_stats = [item for item in stats if str(PurePosixPath(item.path).parent) == directory]
            requested_modules = modules_by_subsystem.get(directory_subsystem, set())
            for module_name in sorted(requested_modules):
                direct_candidates: list[PhysicalCandidate] = []
                for item in directory_stats:
                    if not item.name.endswith(".log") or executor_module_name(item.name, reference) != module_name:
                        continue
                    parsed = _executor_log_identity(item.name, reference)
                    if not parsed:
                        continue
                    _identity, boundary = parsed
                    direct_candidates.append(PhysicalCandidate(
                        item, boundary, "archived" if boundary is not None else "current",
                        frozenset({module_name}),
                    ))
                for candidate in _select_physical_candidates(
                    direct_candidates, fms={module_name}, start=window_start, end=window_end
                ):
                    selected_direct_paths.add(candidate.stat.path)

            tar_candidates: list[PhysicalCandidate] = []
            for item in directory_stats:
                if not item.name.endswith(".tar.gz"):
                    continue
                boundary = _parse_tar_boundary(item.name, reference)
                if boundary is None:
                    continue
                tar_candidates.append(PhysicalCandidate(item, boundary, "tar", frozenset({"executor"})))
            for candidate in _select_physical_candidates(
                tar_candidates, fms={"executor"}, start=window_start, end=window_end
            ):
                selected_tar_paths.add(candidate.stat.path)

        logger.info(
            "log.executor.outer_select metadata_files=%d selected_direct=%d selected_tar=%d start=%s end=%s",
            len(stats), len(selected_direct_paths), len(selected_tar_paths), window_start, window_end,
        )
        if operation_id:
            LogSearchProgressStore.candidates_found(
                operation_id, directory=root, subsystem="执行器日志", discovered=len(stats),
                selected=len(selected_direct_paths) + len(selected_tar_paths),
                action="正在按时间筛选执行器日志文件",
            )

        artifacts: list[LogArtifact] = []
        for stat in stats:
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            parent = str(PurePosixPath(stat.path).parent)
            context = directory_context.get(parent)
            if not context:
                continue
            lower_ip, subsystem = context
            if stat.name.endswith(".log"):
                if stat.path not in selected_direct_paths:
                    continue
                parsed = _executor_log_identity(stat.name, reference)
                if not parsed:
                    continue
                executor_name, boundary = parsed
                module_name = executor_module_name(stat.name, reference)
                if not module_name or module_name not in modules_by_subsystem.get(subsystem, set()):
                    continue
                kind = "archived" if boundary is not None else "current"
                if kind == "current":
                    cache_fm = f"{lower_ip}/{module_name}/{executor_name}"
                    index = self._load_or_probe_direct(
                        environment=environment, target=target, lease=lease, subsystem=subsystem,
                        fm=cache_fm, stat=stat, kind=kind, operation_id=operation_id,
                    )
                    overlaps = _indexed_range_overlaps(index, window_start, window_end)
                    if overlaps:
                        artifacts.append(LogArtifact(
                            target.machine.id, target.machine.name, subsystem, module_name, stat.path,
                            kind, boundary, size=stat.size,
                            source_category=target.source_category, source_name=target.source_name,
                            start_time=_dt_load(index.get("start_time")),
                            end_time=_dt_load(index.get("end_time")),
                            fingerprint=stat.fingerprint,
                            content_version=str(index.get("generation") or stat.fingerprint),
                        ))
                else:
                    overlaps = True
                    artifacts.append(LogArtifact(
                        target.machine.id, target.machine.name, subsystem, module_name, stat.path,
                        kind, boundary, size=stat.size,
                        source_category=target.source_category, source_name=target.source_name,
                        fingerprint=stat.fingerprint, content_version=stat.fingerprint,
                    ))
                if operation_id:
                    LogSearchProgressStore.artifact_checked(operation_id, count=1, selected_increment=1 if overlaps else 0)
                continue

            if not stat.name.endswith(".tar.gz") or stat.path not in selected_tar_paths:
                continue
            members = self._load_or_probe_executor_tar(
                environment=environment, target=target, lease=lease, subsystem=subsystem,
                lower_ip=lower_ip, stat=stat, reference=reference,
                modules=modules_by_subsystem.get(subsystem, set()), operation_id=operation_id,
            )
            selected_member_rows = _select_tar_members_for_window(
                members, fms=modules_by_subsystem.get(subsystem, set()),
                start=window_start, end=window_end,
            )
            selected_members = 0
            for member in selected_member_rows:
                selected_members += 1
                executor_name = str(member.get("executor") or "executor")
                module_name = str(member.get("module") or "")
                if not module_name or module_name not in modules_by_subsystem.get(subsystem, set()):
                    continue
                member_boundary = _dt_load(member.get("boundary_time"))
                # A boundary-less current member inside a selected daily tar is
                # the final segment captured by that day.
                if member_boundary is None:
                    outer = _parse_tar_boundary(stat.name, reference)
                    if outer is not None and re.search(r"(?:^|[_\-.])(?:19|20)\d{6}\.tar\.gz$", stat.name):
                        member_boundary = outer + timedelta(days=1)
                artifacts.append(LogArtifact(
                    target.machine.id, target.machine.name, subsystem, module_name, stat.path,
                    "tar_member", member_boundary,
                    member_name=str(member.get("member_name") or ""), size=stat.size,
                    source_category=target.source_category, source_name=target.source_name,
                    fingerprint=stat.fingerprint,
                    content_version=str(member.get("generation") or stat.fingerprint),
                ))
            if operation_id:
                LogSearchProgressStore.artifact_checked(
                    operation_id, count=1, selected_increment=1 if selected_members else 0
                )

        logger.info(
            "log.executor.collect.finish environment=%s root=%s artifacts=%d elapsed_ms=%d strategy=outer_filename_time_first",
            environment.id, root, len(artifacts), int((time.monotonic() - started) * 1000),
        )
        return artifacts

    def collect_artifacts(
        self,
        *,
        environment,
        target,
        lease,
        directory: str,
        subsystem: str,
        fms: set[str],
        reference: datetime | None,
        window_start: datetime,
        window_end: datetime,
        operation_id: str = "",
    ) -> list[LogArtifact]:
        if not fms:
            return []
        if operation_id:
            LogSearchProgressStore.raise_if_cancelled(operation_id)
        started = time.monotonic()
        if operation_id:
            LogSearchProgressStore.directory_started(
                operation_id, directory=directory, subsystem=subsystem, modules=fms,
                action="正在扫描子系统日志目录",
            )
        stats = discover_window_candidate_stats(
            lease.client, directory, fms, start=window_start, end=window_end,
            sftp=lease.sftp, match_rules=target.match_rules,
        )
        # Legacy compatibility is paid only when the narrow production-name
        # lookup found no historical candidate and the live file is clearly
        # newer than the requested window.  Standard deployments never enter
        # this broad metadata fallback.
        has_historical_named_candidate = any(item.name != f"{fm}.log" for item in stats for fm in fms if item.name.startswith(fm))
        current_mtimes = [
            item.mtime_datetime for item in stats
            if any(item.name == f"{fm}.log" for fm in fms) and item.mtime_datetime is not None
        ]
        should_legacy_fallback = bool(
            not has_historical_named_candidate
            and current_mtimes
            and window_end.date() < max(current_mtimes).date()
        )
        if should_legacy_fallback:
            logger.info(
                "log.file_index.window_find.legacy_fallback directory=%s start=%s end=%s",
                directory, window_start, window_end,
            )
            stats = discover_candidate_stats(
                lease.client, directory, fms, lease.sftp, target.match_rules
            )
        physical: list[PhysicalCandidate] = []
        for stat in stats:
            if operation_id:
                LogSearchProgressStore.raise_if_cancelled(operation_id)
            direct = _match_direct_fm(stat.name, fms, reference)
            if direct:
                fm, filename_boundary = direct
                is_current = stat.name == f"{fm}.log"
                # Production filename timestamps are authoritative for rotated
                # normal logs. Do not fall back to mtime for an unparseable
                # archive name because that would reintroduce false candidates.
                if not is_current and filename_boundary is None:
                    logger.info("log.file_index.skip_unparseable_archive path=%s", stat.path)
                    continue
                physical.append(
                    PhysicalCandidate(
                        stat=stat,
                        boundary=None if is_current else filename_boundary,
                        kind="current" if is_current else "archived",
                        fms=frozenset({fm}),
                    )
                )
                continue

            if stat.name.endswith(".tar.gz"):
                match = _match_tar_fm(stat.name, fms, reference)
                if not match:
                    # Discovery is already FM-prefixed, but reject malformed
                    # archive names instead of guessing from mtime.
                    logger.info("log.file_index.skip_unparseable_tar path=%s", stat.path)
                    continue
                fm, filename_boundary = match
                if filename_boundary is None:
                    logger.info("log.file_index.skip_unparseable_tar_time path=%s", stat.path)
                    continue
                physical.append(PhysicalCandidate(stat, filename_boundary, "tar", frozenset({fm})))

        ordered_physical = _select_physical_candidates(
            physical, fms=fms, start=window_start, end=window_end
        )
        if operation_id:
            LogSearchProgressStore.candidates_found(
                operation_id, directory=directory, subsystem=subsystem,
                discovered=len(stats), selected=len(ordered_physical),
                action="正在按文件名时间筛选候选日志",
            )
            LogSearchProgressStore.add_files(
                operation_id,
                subsystem=subsystem,
                module_count=len(fms),
                discovered=len(stats),
                checked=0,
                selected=0,
            )
        logger.info(
            "log.file_index.index_order environment=%s subsystem=%s metadata_files=%d selected_physical=%d start=%s end=%s strategy=filename_time_fast_index",
            environment.id, subsystem, len(physical), len(ordered_physical), window_start, window_end,
        )

        artifacts: list[LogArtifact] = []
        for candidate in ordered_physical:
            stat = candidate.stat
            if candidate.kind in {"current", "archived"}:
                parsed = _match_direct_fm(stat.name, fms, reference)
                if not parsed:
                    continue
                fm, filename_boundary = parsed
                if candidate.kind == "current":
                    # Current fm.log has no filename timestamp. Probe/cache only
                    # this one selected live file so append generations and
                    # historical content-window cache semantics remain intact.
                    index = self._load_or_probe_direct(
                        environment=environment, target=target, lease=lease, subsystem=subsystem,
                        fm=fm, stat=stat, kind="current", operation_id=operation_id,
                    )
                    if _indexed_range_overlaps(index, window_start, window_end):
                        artifacts.append(
                            LogArtifact(
                                target.machine.id, target.machine.name, subsystem, fm, stat.path,
                                "current", None, size=stat.size,
                                source_category=target.source_category, source_name=target.source_name,
                                start_time=_dt_load(index.get("start_time")),
                                end_time=_dt_load(index.get("end_time")),
                                fingerprint=stat.fingerprint,
                                content_version=str(index.get("generation") or stat.fingerprint),
                            )
                        )
                else:
                    # Rotated fm_<timestamp>.log is already selected by its
                    # filename boundary; never open it merely to probe range.
                    artifacts.append(
                        LogArtifact(
                            target.machine.id, target.machine.name, subsystem, fm, stat.path,
                            "archived", filename_boundary, size=stat.size,
                            source_category=target.source_category, source_name=target.source_name,
                            fingerprint=stat.fingerprint, content_version=stat.fingerprint,
                        )
                    )
                if operation_id:
                    LogSearchProgressStore.artifact_checked(operation_id, count=1, selected_increment=1)
                continue

            members = self._load_or_probe_tar(
                environment=environment, target=target, lease=lease, subsystem=subsystem,
                stat=stat, reference=reference, fms=set(candidate.fms) & fms, operation_id=operation_id,
            )
            selected_members = _select_tar_members_for_window(
                members, fms=set(candidate.fms) & fms, start=window_start, end=window_end
            )
            for member in selected_members:
                fm = str(member.get("fm") or "")
                if fm not in fms:
                    continue
                member_boundary = _dt_load(member.get("boundary_time"))
                if member_boundary is None and _is_daily_tar_candidate(candidate) and candidate.boundary is not None:
                    member_boundary = candidate.boundary + timedelta(days=1)
                artifacts.append(
                    LogArtifact(
                        target.machine.id, target.machine.name, subsystem, fm, stat.path,
                        "tar_member", member_boundary,
                        member_name=str(member.get("member_name") or ""), size=stat.size,
                        source_category=target.source_category, source_name=target.source_name,
                        fingerprint=stat.fingerprint,
                        content_version=str(member.get("generation") or stat.fingerprint),
                    )
                )
            if operation_id:
                LogSearchProgressStore.artifact_checked(
                    operation_id, count=1, selected_increment=1 if selected_members else 0
                )
        logger.info(
            "log.file_index.collect.finish environment=%s host=%s user=%s subsystem=%s fms=%d files=%d selected_physical=%d matched_artifacts=%d elapsed_ms=%d strategy=filename_time_fast_index",
            environment.id, target.machine.host, target.machine.username, subsystem, len(fms),
            len(stats), len(ordered_physical), len(artifacts), int((time.monotonic() - started) * 1000),
        )
        return artifacts



log_file_index = LogFileIndexService()
