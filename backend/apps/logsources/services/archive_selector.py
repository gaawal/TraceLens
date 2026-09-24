from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Iterable

STAMP_FORMATS = (
    "%Y%m%d%H%M%S%f",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%Y%m%d",
    "%Y%m%d_%H%M%S",
    "%Y%m%d-%H%M%S",
    "%Y-%m-%d_%H-%M-%S",
    "%Y-%m-%d_%H%M%S",
    "%m%d%H%M%S",
    "%m%d%H%M",
    "%m%d%H",
)


@dataclass(frozen=True, slots=True)
class LogArtifact:
    machine_id: int
    machine_name: str
    subsystem: str
    fm: str
    path: str
    kind: str  # current | archived | tar_member
    boundary_time: datetime | None
    member_name: str = ""
    size: int = 0
    source_category: str = "debug"
    source_name: str = "调试日志"
    start_time: datetime | None = None
    end_time: datetime | None = None
    fingerprint: str = ""
    content_version: str = ""

    @property
    def identity(self) -> str:
        physical = f"machine-{self.machine_id}:{self.path}"
        return f"{physical}::{self.member_name}" if self.member_name else physical


def parse_archive_timestamp(value: str, reference: datetime | None = None) -> datetime | None:
    token = value.strip("_-. ")
    digits = re.sub(r"\D", "", token)
    expected_lengths = {
        "%Y%m%d%H%M%S%f": 17, "%Y%m%d%H%M%S": 14, "%Y%m%d%H%M": 12,
        "%Y%m%d": 8,
        "%Y%m%d_%H%M%S": 14, "%Y%m%d-%H%M%S": 14,
        "%Y-%m-%d_%H-%M-%S": 14, "%Y-%m-%d_%H%M%S": 14,
        "%m%d%H%M%S": 10, "%m%d%H%M": 8, "%m%d%H": 6,
    }
    for fmt in STAMP_FORMATS:
        if len(digits) != expected_lengths[fmt]:
            continue
        # Eight digits are ambiguous in legacy data: YYYYMMDD vs MMDDHHMM.
        # Production daily archives use a real four-digit year (20xx/19xx),
        # while legacy MMDDHHMM never does.
        if len(digits) == 8:
            looks_like_year = 1900 <= int(digits[:4]) <= 2199
            if fmt == "%Y%m%d" and not looks_like_year:
                continue
            if fmt == "%m%d%H%M" and looks_like_year:
                continue
        try:
            parse_token = token
            # Production archives also use YYYYMMDDHHMMSSmmm (17 digits).
            # datetime %f accepts 1-6 digits, so the 3-digit millisecond suffix is valid.
            if fmt in {"%Y%m%d%H%M%S%f", "%Y%m%d"}:
                parse_token = digits
            result = datetime.strptime(parse_token, fmt)
            if fmt.startswith("%m"):
                result = result.replace(year=(reference or datetime.now()).year)
            return result
        except ValueError:
            continue
    fallback_formats = [(17, "%Y%m%d%H%M%S%f"), (14, "%Y%m%d%H%M%S"), (12, "%Y%m%d%H%M"), (10, "%m%d%H%M%S"), (6, "%m%d%H")]
    if len(digits) == 8:
        fallback_formats.append((8, "%Y%m%d" if 1900 <= int(digits[:4]) <= 2199 else "%m%d%H%M"))
    for length, fmt in fallback_formats:
        if len(digits) == length:
            try:
                result = datetime.strptime(digits, fmt)
                if fmt.startswith("%m"):
                    result = result.replace(year=(reference or datetime.now()).year)
                return result
            except ValueError:
                pass
    return None


def parse_log_name(name: str, reference: datetime | None = None) -> tuple[str, datetime | None] | None:
    basename = PurePosixPath(name).name
    if not basename.endswith(".log"):
        return None
    stem = basename[:-4]
    # Prefer a real year-prefixed trailing timestamp and keep the FM portion
    # non-greedy.  This correctly parses both FM_20260910120000.log and
    # FM_20260910_120000.log; the previous greedy pattern could consume the
    # date into the FM name and misclassify the file as a current log.
    match = re.match(r"^(?P<fm>.+?)_(?P<stamp>(?:19|20)\d{6}[\dT_:\-.]*)$", stem)
    if not match:
        # Legacy month/day based suffixes still use the older permissive form.
        match = re.match(r"^(?P<fm>.+)_(?P<stamp>\d[\dT_:\-.]*)$", stem)
    if match:
        timestamp = parse_archive_timestamp(match.group("stamp"), reference)
        if timestamp:
            return match.group("fm"), timestamp
    return stem, None


def select_artifacts_for_window(
    artifacts: Iterable[LogArtifact], start: datetime, end: datetime
) -> list[LogArtifact]:
    """Select only artifacts whose indexed time range overlaps the query.

    New file-indexed artifacts carry actual first/last valid timestamps. Legacy
    artifacts without them retain the previous filename-boundary fallback so
    Redis remains an optional acceleration layer rather than a hard dependency.
    """
    if end < start:
        raise ValueError("结束时间不能早于开始时间。")

    indexed: list[LogArtifact] = []
    legacy: list[LogArtifact] = []
    for artifact in artifacts:
        if artifact.start_time is not None and artifact.end_time is not None:
            if artifact.start_time <= end and artifact.end_time >= start:
                indexed.append(artifact)
        else:
            legacy.append(artifact)

    grouped: dict[tuple[int, str, str, str], list[LogArtifact]] = {}
    for artifact in legacy:
        grouped.setdefault(
            (artifact.machine_id, artifact.source_category, artifact.subsystem, artifact.fm), []
        ).append(artifact)

    selected = list(indexed)
    for group in grouped.values():
        archives = sorted(
            (item for item in group if item.boundary_time is not None),
            key=lambda item: item.boundary_time,
        )
        current = next((item for item in group if item.kind == "current"), None)
        started = False
        covered_end = False
        for artifact in archives:
            boundary = artifact.boundary_time
            if boundary is None or boundary < start:
                continue
            started = True
            selected.append(artifact)
            if boundary >= end:
                covered_end = True
                break
        latest_boundary = archives[-1].boundary_time if archives else None
        if current and (not covered_end) and (latest_boundary is None or end > latest_boundary):
            selected.append(current)
        elif current and not started and not archives:
            selected.append(current)

    # Deduplicate in case mixed legacy/indexed metadata references the same member.
    deduped = {item.identity: item for item in selected}
    return sorted(
        deduped.values(),
        key=lambda item: (
            item.machine_name,
            item.subsystem,
            item.fm,
            item.start_time or item.boundary_time or datetime.max,
        ),
    )
