"""Budgeted adapters over the existing ATLog/unified readers.

Cache exact requests, never traceId alone. Each component gets an equal row budget
so a noisy component cannot consume all of the evidence window.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from apps.tooling.workstation import bound_query

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="diagnostic-log")
_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
_LOCK = threading.Lock()
_CACHE_BYTES = 4 * 1024 * 1024


def correlate_row(row: dict[str, Any], scope: str, kind: str) -> dict[str, Any]:
    result = dict(row)
    raw = str(row.get("raw") or row.get("message") or "")
    try:
        structured = json.loads(raw)
        if not isinstance(structured, dict):
            structured = {}
    except (ValueError, TypeError):
        structured = {}
    def field(*names: str) -> str:
        for name in names:
            value = row.get(name) or structured.get(name)
            if value:
                return str(value)
        match = re.search(r"\b(?:" + "|".join(map(re.escape, names)) + r")[\s:=\"']+([\w.:-]+)", raw, re.I)
        return match.group(1) if match else ""
    result.update({
        "trace_id": field("trace_id", "traceId"), "span_id": field("span_id", "spanId"),
        "error_code": field("error_code", "errorCode", "display_code", "DisplayCode"),
        "component": field("component", "service.name"),
        "source_kind": row.get("source_kind") or kind,
        "source_path": row.get("source_path") or ("event.log" if kind == "event" else row.get("source", "")),
        "time": row.get("time") or structured.get("timestamp") or "",
        "observed_timestamp": structured.get("observed_timestamp") or None,
        # Existing range readers enumerate window-relative lines; do not claim file-absolute locations.
        "line_number_kind": "window",
    })
    identity = [scope, result["source_path"], result.get("line_number"), result["time"], raw]
    result["record_id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, default=str).encode()).hexdigest()[:24]
    result["correlation_kind"] = "trace" if result["trace_id"] else "candidate"
    return result


def _cached(key: str, read) -> dict[str, Any]:
    now = time.monotonic()
    with _LOCK:
        found = _CACHE.get(key)
        if found and found[0] > now:
            _CACHE.move_to_end(key)
            return {**json.loads(found[1]), "cache_hit": True}
        _CACHE.pop(key, None)
    result = read()
    if not result.get("partial_failure") and not result.get("truncated"):
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode()) <= _CACHE_BYTES:
            with _LOCK:
                _CACHE[key] = (now + (60 if result.get("rows") else 5), encoded)
                while len(_CACHE) > 32 or sum(len(v[1].encode()) for v in _CACHE.values()) > _CACHE_BYTES:
                    _CACHE.popitem(last=False)
    return {**result, "cache_hit": False}


def query(payload: dict[str, Any], *, debug: bool) -> dict[str, Any]:
    from apps.atlog import services
    args = bound_query(payload, debug=debug)
    args["url"] = services.normalize_base_url(str(args.get("url") or ""))
    args["include_event"] = not debug
    key = hashlib.sha256(json.dumps(["v1", debug, args], sort_keys=True, default=str).encode()).hexdigest()

    def read():
        if not debug:
            result = services.query_event_log_tool(args)
        else:
            targets = list({(t["subsystem"], t["module"]): t for t in args["targets"]}.values())
            per_component = max(100, args["max_lines"] // len(targets))
            futures = [(target, _POOL.submit(services.query_case_logs_tool, {**args, "targets": [target], "max_lines": per_component})) for target in targets]
            results, errors = [], []
            for target, future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    errors.append({"target": target, "error": str(exc)[:500]})
            if not results:
                raise services.AtLogError("所有目标组件检索失败：" + json.dumps(errors, ensure_ascii=False))
            rows = sorted([r for item in results for r in item.get("rows", [])], key=lambda r: str(r.get("time") or ""))
            result = {**results[0], "rows": rows[:args["max_lines"]], "count": min(len(rows), args["max_lines"]),
                      "partial_failure": bool(errors), "component_errors": errors,
                      "truncated": len(rows) > args["max_lines"] or any(r.get("truncated") for r in results)}
            for name in ("sources", "components", "levels", "matched_files"):
                result[name] = sorted({str(v) for r in results for v in r.get(name, [])})
            result["log_catalog"] = [{"subsystem": t["subsystem"], "modules": [t["module"]]} for t in targets]
            # Do not inherit the first component's compact AI summary.
            from apps.tooling.log_context import compact_log_rows_for_ai
            result.update(compact_log_rows_for_ai(result["rows"], max_chars=12000, max_nodes=40, max_groups=24))
        result["rows"] = [correlate_row(row, args["url"], "debug" if debug else "event") for row in result.get("rows", [])]
        return result

    return _cached(key, read)


def query_debug(payload: dict[str, Any]) -> dict[str, Any]:
    return query(payload, debug=True)


def query_events(payload: dict[str, Any]) -> dict[str, Any]:
    return query(payload, debug=False)
