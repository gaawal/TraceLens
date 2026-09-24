from __future__ import annotations

import ast
import hashlib
import io
import logging
import os
import posixpath
import shlex
import time
import tokenize
from dataclasses import dataclass
from typing import Iterable

from django.conf import settings

from apps.common.services.ssh import SSH_SESSION_POOL
from apps.environments.models import Environment, ResourceSettings
from apps.logsources.services.redis_store import RedisLogStore

logger = logging.getLogger("tracelens.semantic_source")

DEFAULT_SOURCE_CACHE_TTL = 3600
MAX_SOURCE_BYTES = 2 * 1024 * 1024


class SemanticSourceError(RuntimeError):
    pass



@dataclass(frozen=True)
class SourceTarget:
    subsystem: str
    module: str


def _safe_segment(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise SemanticSourceError(f"{label} 不合法：{value!r}")
    return value


def _cache_ttl() -> int:
    return max(60, int(getattr(settings, "TRACELENS_SOURCE_CACHE_TTL", DEFAULT_SOURCE_CACHE_TTL)))


def _identity_prefix(environment: Environment) -> str:
    machine = environment.upper_machine
    host = (machine.host or "unknown").strip().lower()
    username = (machine.username or "unknown").strip().lower()
    return f"tracelens:{RedisLogStore.CACHE_SCHEMA}:source:host-{host}:user-{username}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:28]


def _render_root(template: str, *, username: str, subsystem: str, module: str) -> str:
    values = {
        "username": username,
        "subsystem": subsystem,
        "module": module,
    }
    rendered = str(template or "").strip()
    for name, value in values.items():
        rendered = rendered.replace("{" + name + "}", value)
    if not rendered:
        raise SemanticSourceError("源码目录模板为空，请先在设置中心配置。")
    if "{" in rendered or "}" in rendered:
        raise SemanticSourceError(f"源码目录模板仍包含未识别参数：{rendered}")
    return posixpath.normpath(rendered)


def _decode_python_source(raw: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except (SyntaxError, UnicodeDecodeError):
        encoding = "utf-8"
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _doc_summary(doc: str | None) -> str:
    """Only expose the first meaningful docstring line in the log UI."""
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        value = line.strip()
        if value:
            return value
    return ""


class _FunctionIndexVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.functions: list[dict] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = ".".join([*self.stack, node.name])
        self.functions.append({
            "name": node.name,
            "qualname": qualname,
            "line": int(getattr(node, "lineno", 0) or 0),
            "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
            "description": _doc_summary(ast.get_docstring(node, clean=True)),
        })
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def _build_function_index(source: str) -> list[dict]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SemanticSourceError(f"Python 源码 AST 解析失败：{exc.msg}（第 {exc.lineno or '?'} 行）") from exc
    visitor = _FunctionIndexVisitor()
    visitor.visit(tree)
    return visitor.functions


def _normalize_function_name(value: str | None) -> str:
    value = str(value or "").strip()
    if value.endswith("()"):
        value = value[:-2]
    if "::" in value:
        value = value.rsplit("::", 1)[-1]
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value.strip()


def _select_function(functions: list[dict], *, function_name: str | None, source_line: int | None) -> dict | None:
    wanted = _normalize_function_name(function_name)
    exact = [item for item in functions if item.get("name") == wanted] if wanted else []
    containing: list[dict] = []
    if source_line:
        containing = [
            item for item in functions
            if int(item.get("line") or 0) <= source_line <= int(item.get("end_line") or 0)
        ]
        containing.sort(key=lambda item: (int(item.get("end_line") or 0) - int(item.get("line") or 0), -int(item.get("line") or 0)))
    if exact and containing:
        exact_ids = {(item.get("qualname"), item.get("line")) for item in exact}
        for item in containing:
            if (item.get("qualname"), item.get("line")) in exact_ids:
                return item
    if containing:
        return containing[0]
    if exact:
        return exact[0]
    return None


def _read_remote_source(environment: Environment, root: str, basename: str) -> tuple[str, bytes] | None:
    machine = environment.upper_machine
    direct = posixpath.join(root, basename)
    with SSH_SESSION_POOL.acquire(machine) as lease:
        resolved = ""
        try:
            attrs = lease.sftp.stat(direct)
            if attrs and not str(direct).endswith("/"):
                resolved = direct
        except Exception:
            resolved = ""
        if not resolved:
            command = f"find {shlex.quote(root)} -type f -name {shlex.quote(basename)} -print -quit"
            _, stdout, stderr = lease.client.exec_command(command, timeout=30)
            output = stdout.read()
            error = stderr.read()
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            if isinstance(error, bytes):
                error = error.decode("utf-8", errors="replace")
            resolved = next((line.strip() for line in str(output).splitlines() if line.strip()), "")
            if not resolved:
                detail = str(error).strip()
                lower_detail = detail.lower()
                # Source absence is a normal semantic miss. Do not raise/log it.
                if not detail or any(token in lower_detail for token in (
                    "no such file", "not found", "cannot access", "not a directory"
                )):
                    return None
                # Permission/SSH/remote command failures are real errors and remain visible.
                raise SemanticSourceError(f"读取源码目录失败 {root}：{detail}")
        with lease.sftp.open(resolved, "rb") as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise SemanticSourceError(f"源码文件超过 {MAX_SOURCE_BYTES} 字节，拒绝自动解析：{resolved}")
        return resolved, bytes(raw)


def _load_index_for_target(environment: Environment, root: str, basename: str) -> tuple[str, list[dict], bool] | None:
    prefix = _identity_prefix(environment)
    version_identity = (environment.software_version or "unknown").strip()
    lookup_key = f"{prefix}:lookup:{_digest(root + '|' + basename + '|' + version_identity)}"
    cached_lookup = RedisLogStore.get_json(lookup_key)
    if isinstance(cached_lookup, dict):
        if cached_lookup.get("missing") is True:
            return None
        resolved = str(cached_lookup.get("path") or "")
        index_key = str(cached_lookup.get("index_key") or "")
        if resolved and index_key:
            cached_index = RedisLogStore.get_json(index_key)
            if isinstance(cached_index, dict) and isinstance(cached_index.get("functions"), list):
                logger.info("semantic.source.cache_hit host=%s file=%s path=%s functions=%d", environment.upper_machine.host, basename, resolved, len(cached_index["functions"]))
                return resolved, cached_index["functions"], True

    loaded = _read_remote_source(environment, root, basename)
    if loaded is None:
        RedisLogStore.set_json(lookup_key, {"missing": True, "cached_at": int(time.time())}, min(120, _cache_ttl()))
        return None
    resolved, raw = loaded
    source = _decode_python_source(raw)
    functions = _build_function_index(source)
    path_hash = _digest(resolved)
    index_key = f"{prefix}:index:{path_hash}"
    source_key = f"{prefix}:file:{path_hash}"
    ttl = _cache_ttl()
    cached_at = int(time.time())
    RedisLogStore.set_compressed(source_key, raw, ttl)
    RedisLogStore.set_json(index_key, {
        "path": resolved,
        "functions": functions,
        "cached_at": cached_at,
        "source_bytes": len(raw),
    }, ttl)
    RedisLogStore.set_json(lookup_key, {
        "path": resolved,
        "index_key": index_key,
        "source_key": source_key,
        "cached_at": cached_at,
    }, ttl)
    logger.info("semantic.source.cached host=%s file=%s path=%s bytes=%d functions=%d ttl=%d", environment.upper_machine.host, basename, resolved, len(raw), len(functions), ttl)
    return resolved, functions, False


def _public_source_templates(settings_obj: ResourceSettings) -> list[str]:
    values: list[str] = []
    for raw in str(getattr(settings_obj, "source_code_public_paths", "") or "").splitlines():
        value = raw.strip()
        if value and value not in values:
            values.append(value)
    return values


def recognize_semantic_description(
    environment: Environment,
    *,
    source_file: str,
    source_line: int | None,
    function_name: str | None,
    targets: Iterable[dict],
) -> dict | None:
    basename = posixpath.basename(str(source_file or "").strip().replace("\\", "/"))
    if not basename.lower().endswith(".py"):
        raise SemanticSourceError("自动识别语义仅支持 .py 源码日志。")
    if not basename or basename in {".", ".."}:
        raise SemanticSourceError("日志中没有可用的 Python 源文件名。")

    settings_obj = ResourceSettings.get_solo()
    template = settings_obj.source_code_path_template
    public_templates = _public_source_templates(settings_obj)
    username = (environment.upper_machine.username or "").strip()
    if not username:
        raise SemanticSourceError("当前上位机没有配置 SSH 用户名，无法展开源码目录。")

    normalized_targets: list[SourceTarget] = []
    seen: set[tuple[str, str]] = set()
    for item in targets:
        target = SourceTarget(
            subsystem=_safe_segment(item.get("subsystem", ""), "子系统"),
            module=_safe_segment(item.get("module", item.get("fm", "")), "模块"),
        )
        key = (target.subsystem, target.module)
        if key not in seen:
            seen.add(key)
            normalized_targets.append(target)
    if not normalized_targets:
        raise SemanticSourceError("当前日志任务没有子系统/模块上下文，无法定位源码。")

    candidates: list[tuple[str, str, str]] = []
    seen_roots: set[str] = set()
    for target in normalized_targets:
        specific = _render_root(template, username=username, subsystem=target.subsystem, module=target.module)
        if specific not in seen_roots:
            seen_roots.add(specific)
            candidates.append((specific, target.subsystem, target.module))
    # Public roots are searched after module-specific roots. They may still use the same placeholders.
    for raw_template in public_templates:
        for target in normalized_targets:
            public_root = _render_root(raw_template, username=username, subsystem=target.subsystem, module=target.module)
            if public_root not in seen_roots:
                seen_roots.add(public_root)
                candidates.append((public_root, "公共", "公共"))

    for root, subsystem, module in candidates:
        loaded = _load_index_for_target(environment, root, basename)
        if loaded is None:
            continue
        resolved, functions, cache_hit = loaded
        function = _select_function(functions, function_name=function_name, source_line=source_line)
        if function is None:
            continue
        description = str(function.get("description") or "").strip()
        if not description:
            continue
        return {
            "description": description,
            "function_name": function.get("name") or "",
            "qualified_name": function.get("qualname") or function.get("name") or "",
            "function_line": function.get("line"),
            "function_end_line": function.get("end_line"),
            "source_path": resolved,
            "source_file": basename,
            "subsystem": subsystem,
            "module": module,
            "cache_hit": cache_hit,
            "cache_ttl": _cache_ttl(),
        }

    # No matching file/function/docstring is expected for many log lines.
    # Return an empty hit and let callers skip it silently.
    return None


def recognize_semantic_batch(
    environment: Environment,
    *,
    items: Iterable[dict],
    targets: Iterable[dict],
) -> dict:
    results: list[dict] = []
    misses: list[dict] = []
    for item in items:
        source_file = str(item.get("source_file") or "")
        function_name = str(item.get("function_name") or "")
        source_line = item.get("source_line")
        key = str(item.get("key") or f"{source_file}|{function_name}")
        try:
            payload = recognize_semantic_description(
                environment,
                source_file=source_file,
                source_line=int(source_line) if source_line else None,
                function_name=function_name,
                targets=targets,
            )
            if payload is None:
                continue
            payload["key"] = key
            results.append(payload)
        except SemanticSourceError as exc:
            misses.append({"key": key, "source_file": source_file, "function_name": function_name, "message": str(exc)})
    return {"results": results, "misses": misses, "count": len(results)}

