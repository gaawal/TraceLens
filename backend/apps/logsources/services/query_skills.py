from __future__ import annotations

import fnmatch
import logging
import posixpath
import stat
from datetime import datetime, timedelta
from typing import Any

from apps.common.services.ssh import SshOperationError, ssh_session
from apps.environments.models import Environment
from apps.logsources.models import LogQuerySkill
from apps.logsources.services.file_index import parse_line_time

logger = logging.getLogger("tracelens.log_query_skill")


class LogQuerySkillError(RuntimeError):
    pass


def _is_dhh_relation(relation) -> bool:
    machine = relation.target_machine
    metadata = relation.metadata or {}
    station_name = str(metadata.get("station_name") or machine.station_name or machine.name or "").strip().lower()
    station_type = str(metadata.get("station_type") or machine.station_type or "").strip().upper()
    return station_name == "dhh" or station_type == "DHH"


def _machines_for_scope(environment: Environment, scope: str) -> list[Any]:
    scope = str(scope or "upper").strip().lower()
    if scope == "upper":
        return [environment.upper_machine]
    relations = list(
        environment.machine_relations.select_related("target_machine").filter(
            is_active=True,
            target_machine__is_active=True,
        )
    )
    if scope == "dhh":
        return [relation.target_machine for relation in relations if _is_dhh_relation(relation)][:1]
    lowers = [relation.target_machine for relation in relations if not _is_dhh_relation(relation)]
    if scope == "lower":
        return lowers[:1]
    if scope == "all_lower":
        return lowers[:8]
    return []


def _expand_path(template: str, *, environment: Environment, machine: Any, skill: LogQuerySkill, module: str) -> str:
    values = {
        "username": str(machine.username or ""),
        "host": str(machine.host or ""),
        "machine_ip": str(machine.host or ""),
        "upper_machine_ip": str(environment.upper_machine.host or ""),
        "environment": str(environment.name or ""),
        "subsystem": str(skill.subsystem.name or ""),
        "module": str(module or ""),
    }
    try:
        path = str(template or "").format_map(values).strip()
    except KeyError as exc:
        raise LogQuerySkillError(f"日志查询 Skill 路径包含未知参数：{exc.args[0]}") from exc
    if ".." in path.replace("\\", "/").split("/"):
        raise LogQuerySkillError("日志查询 Skill 路径不能包含 ..。")
    return path


def _candidate_paths(sftp, path: str, pattern: str) -> list[tuple[str, Any]]:
    if path.startswith("~/"):
        path = posixpath.join(sftp.normalize("."), path[2:])
    try:
        attrs = sftp.stat(path)
    except OSError as exc:
        raise LogQuerySkillError(f"补充日志路径不存在：{path}") from exc
    if stat.S_ISREG(attrs.st_mode):
        return [(path, attrs)]
    if not stat.S_ISDIR(attrs.st_mode):
        raise LogQuerySkillError(f"补充日志路径不是文件或目录：{path}")
    try:
        children = sftp.listdir_attr(path)
    except OSError as exc:
        raise LogQuerySkillError(f"无法读取补充日志目录：{path}") from exc
    rows: list[tuple[str, Any]] = []
    for item in children:
        if not stat.S_ISREG(item.st_mode):
            continue
        if fnmatch.fnmatch(item.filename, pattern or "*.log*"):
            rows.append((posixpath.join(path, item.filename), item))
    rows.sort(key=lambda pair: (getattr(pair[1], "st_mtime", 0), pair[0]), reverse=True)
    return rows[:12]


def _read_tail(sftp, path: str, *, max_bytes: int) -> bytes:
    with sftp.open(path, "rb") as handle:
        try:
            size = int(handle.stat().st_size)
        except Exception:
            size = 0
        if size > max_bytes:
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
            newline = data.find(b"\n")
            if newline >= 0:
                data = data[newline + 1 :]
            return data
        return handle.read(max_bytes)


def match_query_skills(*, subsystem: str, module: str = "", text: str = "", limit: int = 6) -> list[dict[str, Any]]:
    subsystem = str(subsystem or "").strip()
    if not subsystem:
        return []
    queryset = LogQuerySkill.objects.select_related("subsystem").filter(enabled=True).filter(
        subsystem__name__iexact=subsystem
    )
    if not queryset.exists():
        queryset = LogQuerySkill.objects.select_related("subsystem").filter(enabled=True).filter(
            subsystem__display_name__iexact=subsystem
        )
    module_cf = str(module or "").strip().casefold()
    text_cf = str(text or "").casefold()
    ranked: list[tuple[int, LogQuerySkill, list[str]]] = []
    for skill in queryset[:100]:
        modules = [str(value).strip() for value in (skill.trigger_modules or []) if str(value).strip()]
        keywords = [str(value).strip() for value in (skill.trigger_keywords or []) if str(value).strip()]
        score = int(skill.priority or 0)
        reasons: list[str] = []
        if modules:
            matched_module = next((value for value in modules if module_cf and value.casefold() == module_cf), "")
            if matched_module:
                score += 200
                reasons.append(f"模块={matched_module}")
            elif module_cf:
                # A module-scoped skill should not be used for an unrelated module.
                continue
        matched_keywords = [value for value in keywords if value.casefold() in text_cf]
        if keywords and matched_keywords:
            score += 40 * min(len(matched_keywords), 4)
            reasons.append("关键字=" + "、".join(matched_keywords[:4]))
        elif keywords and text_cf:
            # Keep it as a weaker subsystem fallback only when no module constraint exists.
            score -= 40
        if not modules and not keywords:
            reasons.append("子系统默认策略")
        ranked.append((score, skill, reasons))
    ranked.sort(key=lambda item: (-item[0], item[1].name.casefold(), item[1].id))
    result = []
    for score, skill, reasons in ranked[: max(1, min(limit, 20))]:
        result.append({
            "id": skill.id,
            "name": skill.name,
            "subsystem": skill.subsystem.name,
            "subsystem_display_name": skill.subsystem.display_name,
            "priority": skill.priority,
            "trigger_modules": list(skill.trigger_modules or [])[:16],
            "trigger_keywords": list(skill.trigger_keywords or [])[:24],
            "description": str(skill.description or "")[:1000],
            "step_count": len(skill.steps or []),
            "score": score,
            "matched_by": reasons,
            "steps": list(skill.steps or [])[:8],
        })
    return result


def query_skill_step(
    *,
    environment: Environment,
    skill: LogQuerySkill,
    step_index: int,
    start: datetime,
    end: datetime,
    max_lines: int = 160,
    max_bytes_per_file: int = 192_000,
) -> dict[str, Any]:
    steps = list(skill.steps or [])
    if step_index < 0 or step_index >= len(steps):
        raise LogQuerySkillError("日志查询 Skill 步骤不存在。")
    step = steps[step_index]
    if not isinstance(step, dict):
        raise LogQuerySkillError("日志查询 Skill 步骤配置无效。")
    if str(step.get("source_type") or "standard") != "custom_path":
        raise LogQuerySkillError("该步骤不是自定义路径日志，请使用标准日志查询能力。")

    module = str(step.get("module") or "").strip()
    before = max(0, min(int(step.get("time_before_seconds") or 5), 600))
    after = max(0, min(int(step.get("time_after_seconds") or 5), 600))
    query_start = start - timedelta(seconds=before)
    query_end = end + timedelta(seconds=after)
    keywords = [str(value).strip() for value in (step.get("keywords") or []) if str(value).strip()]
    machine_scope = str(step.get("machine_scope") or "upper")
    machines = _machines_for_scope(environment, machine_scope)
    if not machines:
        return {
            "status": "source_not_found",
            "skill_id": skill.id,
            "skill_name": skill.name,
            "subsystem": skill.subsystem.name,
            "step_index": step_index,
            "step_name": str(step.get("name") or f"步骤 {step_index + 1}"),
            "machine_scope": machine_scope,
            "line_count": 0,
            "lines": [],
            "sources": [],
            "message": "当前环境没有符合该 Skill 机器范围的资源。",
        }

    max_lines = max(20, min(int(max_lines), 500))
    all_lines: list[str] = []
    sources: list[dict[str, Any]] = []
    missing_paths: list[str] = []
    for machine in machines:
        path = _expand_path(str(step.get("path_template") or ""), environment=environment, machine=machine, skill=skill, module=module)
        pattern = str(step.get("file_pattern") or "*.log*")
        try:
            with ssh_session(machine) as lease:
                try:
                    candidates = _candidate_paths(lease.sftp, path, pattern)
                except LogQuerySkillError as exc:
                    missing_paths.append(str(exc))
                    continue
                for candidate_path, attrs in candidates:
                    if len(all_lines) >= max_lines:
                        break
                    # Skip obviously unrelated archived files by mtime, but keep the newest/current file.
                    mtime = datetime.fromtimestamp(float(getattr(attrs, "st_mtime", 0) or 0)) if getattr(attrs, "st_mtime", 0) else None
                    if mtime and mtime < query_start - timedelta(days=2):
                        continue
                    raw = _read_tail(lease.sftp, candidate_path, max_bytes=max_bytes_per_file)
                    matched_in_file = 0
                    for decoded in raw.decode("utf-8", errors="replace").splitlines():
                        line = decoded.rstrip("\r\n")
                        if not line:
                            continue
                        timestamp = parse_line_time(line)
                        if timestamp is not None and not (query_start <= timestamp <= query_end):
                            continue
                        if keywords and not any(keyword.casefold() in line.casefold() for keyword in keywords):
                            continue
                        all_lines.append(line[:8000])
                        matched_in_file += 1
                        if len(all_lines) >= max_lines:
                            break
                    sources.append({
                        "machine_id": machine.id,
                        "host": machine.host,
                        "path": candidate_path,
                        "matched_lines": matched_in_file,
                    })
        except SshOperationError as exc:
            sources.append({"machine_id": machine.id, "host": machine.host, "path": path, "error": str(exc)[:500], "matched_lines": 0})

    if all_lines:
        status = "found_evidence"
        message = f"补充日志命中 {len(all_lines)} 行。"
    elif sources:
        status = "no_match"
        message = "补充日志源可访问，但当前时间窗/关键字没有匹配内容。"
    else:
        status = "source_not_found"
        message = missing_paths[0] if missing_paths else "没有找到补充日志源。"
    logger.info(
        "log_query_skill.query skill=%s subsystem=%s step=%s status=%s lines=%s sources=%s",
        skill.id, skill.subsystem.name, step_index, status, len(all_lines), len(sources),
    )
    return {
        "status": status,
        "skill_id": skill.id,
        "skill_name": skill.name,
        "subsystem": skill.subsystem.name,
        "step_index": step_index,
        "step_name": str(step.get("name") or f"步骤 {step_index + 1}"),
        "machine_scope": machine_scope,
        "start_time": query_start.isoformat(),
        "end_time": query_end.isoformat(),
        "keywords": keywords,
        "line_count": len(all_lines),
        "lines": all_lines,
        "sources": sources[:12],
        "message": message,
    }
