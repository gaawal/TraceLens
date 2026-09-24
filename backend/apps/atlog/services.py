from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable

from apps.tooling.log_context import compact_log_rows_for_ai

from apps.atlog.anomaly_rules import compile_anomaly_rules, matching_compiled_anomaly_rules, normalize_anomaly_rules
from apps.logsources.services.archive_selector import parse_archive_timestamp
from apps.logsources.services.unified_search import (
    UnifiedLogSearchError,
    UrlLogArtifact,
    filter_url_artifacts_for_window,
    read_url_log_window,
    select_url_log_names_for_window,
)

logger = logging.getLogger("tracelens.atlog")


def _log_search_progress():
    # ATLog parsers stay standalone; Redis/Django is loaded only for live searches.
    from apps.logsources.services.search_progress import LogSearchProgressStore
    return LogSearchProgressStore

DEFAULT_TIMEOUT = 12
MAX_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 6 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_XYTEST_TAIL_BYTES = 4 * 1024 * 1024
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_TIMESTAMP_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)\]")
_XY_ERROR_RE = re.compile(
    r"^\[(?P<time>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)\]\s*"
    r"\[(?P<level>[^\]]+)\](?P<rest>.*)$"
)
_PY_LOCATION_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s\n:]+\.py):(?P<line>\d+)")
_PY_CALL_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s\n:]+\.py):(?P<line>\d+):\s+in\s+(?P<func>[A-Za-z_][\w<>]*)")

_ASSERTION_EXPECT_RE = re.compile(
    r"\bexpect\s*:?\s*(?P<expect>.*?)\s*,\s*real\s*:?\s*(?P<real>.*?)(?=\s*,\s*expect\s+to\s+be|\s*,\s*caller\s*:|\s*,\s*error\s+msg\s*:|$)",
    re.I,
)
_ASSERTION_RELATION_RE = re.compile(r"\b(expect\s+to\s+be\s+[^,]+)", re.I)
_ASSERTION_CALLER_RE = re.compile(r"\bcaller\s*:\s*(?P<caller>.*?)(?=\s*,\s*error\s+msg\s*:|$)", re.I)
_ASSERTION_ERROR_MSG_RE = re.compile(r"\berror\s+msg\s*:\s*(?P<error>.*)$", re.I)



class AtLogError(RuntimeError):
    pass


class DirectoryIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)
                break


class ResultTableTextParser(HTMLParser):
    """Extract rows from pytest-html's results table without assuming a fixed row index."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_results = False
        self.table_depth = 0
        self.in_row = False
        self.row_attrs: dict[str, str] = {}
        self.row_parts: list[str] = []
        self.rows: list[tuple[dict[str, str], str]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {key.lower(): str(value or '') for key, value in attrs}
        if tag == "table" and attrs_dict.get("id") == "results-table":
            self.in_results = True
            self.table_depth = 1
            return
        if self.in_results and tag == "table":
            self.table_depth += 1
        if not self.in_results:
            return
        if tag == "tr" and self.table_depth == 1 and not self.in_row:
            self.in_row = True
            self.row_attrs = attrs_dict
            self.row_parts = []
            self.skip_depth = 0
            return
        if not self.in_row:
            return
        if tag in {"script", "style"}:
            self.skip_depth += 1
        elif not self.skip_depth and tag in {"br", "p", "div", "li", "pre"}:
            self.row_parts.append("\n")
        elif not self.skip_depth and tag in {"td", "th"}:
            self.row_parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.in_row and tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        if self.in_results and tag == "tr" and self.table_depth == 1 and self.in_row:
            value = unescape("".join(self.row_parts)).replace("\xa0", " ")
            lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
            text = "\n".join(line for line in lines if line).strip()
            self.rows.append((dict(self.row_attrs), text))
            self.in_row = False
            self.row_attrs = {}
            self.row_parts = []
            self.skip_depth = 0
        if self.in_results and tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_results = False
                self.table_depth = 0

    def handle_data(self, data: str) -> None:
        if self.in_results and self.in_row and not self.skip_depth:
            self.row_parts.append(data)

    @staticmethod
    def _is_placeholder(text: str) -> bool:
        compact = re.sub(r"\s+", " ", text).strip().lower()
        return not compact or "no results found" in compact or "try to check the filters" in compact

    def text(self, case_id: str = "") -> str:
        rows = [(attrs, text) for attrs, text in self.rows if not self._is_placeholder(text)]
        if not rows:
            return ""

        target = str(case_id or "").strip().lower()
        if target:
            matched_indexes: list[int] = []
            for index, (attrs, text) in enumerate(rows):
                attrs_text = " ".join(attrs.values()).lower()
                haystack = f"{attrs_text} {text.lower()}"
                if target in haystack or f"test_{target}" in haystack:
                    matched_indexes.append(index)
            if matched_indexes:
                selected: list[str] = []
                selected_indexes: set[int] = set()
                for index in matched_indexes:
                    selected_indexes.add(index)
                    # pytest-html commonly stores the expanded captured log in the row immediately
                    # after the case summary row. Include it unless it clearly belongs to another case.
                    if index + 1 < len(rows):
                        next_text = rows[index + 1][1]
                        case_tokens = re.findall(r"\bSN_[A-Za-z0-9_]+", next_text, flags=re.I)
                        if not case_tokens or any(token.lower() == target for token in case_tokens):
                            selected_indexes.add(index + 1)
                for index in sorted(selected_indexes):
                    value = rows[index][1]
                    if value and value not in selected:
                        selected.append(value)
                if selected:
                    return "\n".join(selected).strip()

        # Never return pytest-html's filter placeholder as the report content. When exact case
        # matching is unavailable, prefer substantive result rows and leave the full-page parser
        # as the final fallback in parse_pytest_html_excerpt().
        substantive = [
            text for _, text in rows
            if re.search(r"AssertionError|FAILED|FAIL|ERROR|PASSED|PASS|test_|SN_", text, re.I)
        ]
        return "\n".join(substantive or [text for _, text in rows]).strip()


class VisibleHtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"}:
            self.skip_depth += 1
            return
        if not self.skip_depth and tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "pre"}:
            self.parts.append("\n")
        if not self.skip_depth and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = unescape("".join(self.parts)).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        result: list[str] = []
        for line in lines:
            if not line and (not result or result[-1] == ""):
                continue
            result.append(line)
        return "\n".join(result).strip()


@dataclass(frozen=True)
class RemoteFile:
    url: str
    status: int
    content: bytes
    content_length: int | None = None
    content_range: str = ""


def _is_private_host(hostname: str) -> bool:
    host = (hostname or "").strip("[]")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        pass
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    if not addresses:
        return False
    try:
        return all(
            ipaddress.ip_address(address).is_private
            or ipaddress.ip_address(address).is_loopback
            or ipaddress.ip_address(address).is_link_local
            for address in addresses
        )
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AtLogError("用例日志 URL 不能为空。")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AtLogError("仅支持 http/https ATLog 用例链接。")
    if not _is_private_host(parsed.hostname):
        raise AtLogError("ATLog URL 必须指向内网地址，已拒绝访问公网主机。")
    path = parsed.path
    # Allow users to paste a known report file and normalize back to the case directory.
    file_tail = path.rstrip("/").split("/")[-1]
    if "." in file_tail and not file_tail.startswith("task_"):
        path = path[: path.rfind("/") + 1]
    if not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _case_root_file_url(base_url: str, filename: str) -> str:
    """Build a URL for a file stored directly in the ATLog case root.

    Root files such as ``event.log`` are deliberately separate from the
    ``full_logs/log/...`` tree.
    """
    base = normalize_base_url(base_url)
    name = str(filename or "").strip().lstrip("/")
    if not name or "/" in name or "\\" in name:
        raise AtLogError("用例根目录文件名无效。")
    return _safe_child_url(base, name)


def case_id_from_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    parts = [urllib.parse.unquote(item) for item in parsed.path.rstrip("/").split("/") if item]
    return parts[-1] if parts else ""


def _ancestor_case_urls(base_url: str) -> list[str]:
    """Return the case directory and at most two parent directories.

    The published ATLog root directory name is intentionally not constrained.
    Different environments may expose cases below arbitrary path segments, so
    the current case URL is the trust boundary and ancestor lookup is bounded
    by depth rather than by a hard-coded ``/ATLog_*`` directory name.
    """
    parsed = urllib.parse.urlsplit(base_url)
    current = parsed.path.rstrip("/")
    values: list[str] = []
    for _ in range(3):
        if not current or current == "/":
            break
        values.append(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, current + "/", "", "")))
        parent = current.rsplit("/", 1)[0]
        if not parent or parent == current:
            break
        current = parent
    return values or [base_url]


def _request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, max_bytes: int | None = None) -> RemoteFile:
    req_headers = {"User-Agent": "TraceLens-ATLog/1.0", "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url=url, headers=req_headers, method=method)
    try:
        with _DIRECT_OPENER.open(request, timeout=DEFAULT_TIMEOUT) as response:
            status = int(getattr(response, "status", 200) or 200)
            length_value = response.headers.get("Content-Length")
            content_length = int(length_value) if length_value and length_value.isdigit() else None
            if method == "HEAD":
                content = b""
            elif max_bytes is None:
                content = response.read()
            else:
                range_ignored = "Range" in req_headers and status != 206 and not response.headers.get("Content-Range")
                # 大日志不再因为固定 MB 阈值直接失败。
                # ATLog 场景下需要保留异常上下文（event/xytest/debug/pytest），
                # 上层通过上下文窗口和 Range 检索控制 AI 输入量，而不是在下载阶段截断。
                content = response.read(max_bytes)
            return RemoteFile(
                url=url,
                status=status,
                content=content,
                content_length=content_length,
                content_range=response.headers.get("Content-Range", ""),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(url) from exc
        raise AtLogError(f"访问 ATLog 失败 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise AtLogError(f"无法访问 ATLog：{url} · {exc.reason}") from exc
    except TimeoutError as exc:
        raise AtLogError(f"访问 ATLog 超时：{url}") from exc


def _decode(content: bytes) -> str:
    if not content:
        return ""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_text(url: str, max_bytes: int = MAX_TEXT_BYTES) -> str:
    return _decode(_request(url, max_bytes=max_bytes).content)


def _head_size(url: str) -> int | None:
    """Return remote file size without requiring HEAD support.

    Some ATLog HTTP servers serve static files correctly with GET but reject HEAD or
    omit Content-Length on HEAD. Probe with a one-byte Range GET before giving up so
    large event.log files are not misclassified as unreadable merely because HEAD is
    unsupported.
    """
    try:
        result = _request(url, method="HEAD")
        if result.content_length is not None:
            return result.content_length
    except (AtLogError, FileNotFoundError):
        pass

    try:
        probe = _request(url, headers={"Range": "bytes=0-0"}, max_bytes=1024)
        content_range = str(probe.content_range or "")
        match = re.search(r"/(\d+)$", content_range)
        if match:
            return int(match.group(1))
        if probe.content_length is not None:
            return probe.content_length
    except (AtLogError, FileNotFoundError):
        return None
    return None


def _fetch_range(url: str, start: int, end: int) -> tuple[bytes, bool]:
    if start < 0:
        start = 0
    result = _request(url, headers={"Range": f"bytes={start}-{max(start, end)}"}, max_bytes=max(1, end - start + 1) + 1024)
    ranged = result.status == 206 or bool(result.content_range)
    return result.content, ranged


def fetch_tail_text(url: str, max_tail_bytes: int = MAX_XYTEST_TAIL_BYTES) -> str:
    size = _head_size(url)
    if size is None or size <= max_tail_bytes:
        return fetch_text(url, max_tail_bytes)
    content, ranged = _fetch_range(url, max(0, size - max_tail_bytes), size - 1)
    if not ranged and len(content) >= size:
        content = content[-max_tail_bytes:]
    text = _decode(content)
    # The first line of a byte range can be partial.
    newline = text.find("\n")
    return text[newline + 1 :] if newline >= 0 else text


def list_directory(url: str) -> list[str]:
    base = url if url.endswith("/") else url + "/"
    parser = DirectoryIndexParser()
    parser.feed(fetch_text(base, MAX_DIRECTORY_BYTES))
    values: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        href = unescape(href).strip()
        if not href or href.startswith("?") or href.startswith("#") or href in {"../", "./"}:
            continue
        absolute = urllib.parse.urljoin(base, href)
        parsed_base = urllib.parse.urlsplit(base)
        parsed_abs = urllib.parse.urlsplit(absolute)
        if parsed_abs.scheme != parsed_base.scheme or parsed_abs.netloc != parsed_base.netloc:
            continue
        relative = urllib.parse.unquote(parsed_abs.path[len(parsed_base.path) :]) if parsed_abs.path.startswith(parsed_base.path) else urllib.parse.unquote(href)
        relative = relative.lstrip("/")
        if relative and relative not in seen:
            seen.add(relative)
            values.append(relative)
    return values


def _safe_child_url(base_url: str, relative_path: str) -> str:
    rel = urllib.parse.unquote(str(relative_path or "")).replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("../") or "/../" in f"/{rel}":
        raise AtLogError("非法 ATLog 相对路径。")
    result = urllib.parse.urljoin(base_url, urllib.parse.quote(rel, safe="/._-()[]"))
    parsed_base = urllib.parse.urlsplit(base_url)
    parsed_result = urllib.parse.urlsplit(result)
    if parsed_result.netloc != parsed_base.netloc or not parsed_result.path.startswith(parsed_base.path):
        raise AtLogError("禁止跳出当前用例目录。")
    return result


def _parse_timestamp(value: str | None) -> datetime | None:
    raw = str(value or "").strip().replace("T", " ")
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1]
    raw = re.sub(r"([+-]\d{2}:?\d{2})$", "", raw).strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?", raw)
    if not match:
        return None
    fraction = (match.group(3) or "")[:6].ljust(6, "0")
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}.{fraction}", "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _xml_attr_int(root: ET.Element, name: str) -> int:
    try:
        return int(root.attrib.get(name, "0") or 0)
    except (TypeError, ValueError):
        return 0


def _failure_message(node: ET.Element) -> tuple[str, str]:
    failure = node.find("failure")
    if failure is None:
        failure = node.find("error")
    if failure is None:
        return "", ""
    return str(failure.attrib.get("message") or "").strip(), str(failure.text or "").strip()


def _is_pytest_wrapper(classname: str, name: str) -> bool:
    combined = f"{classname} {name}".strip().lower()
    return bool(re.search(r"(?:^|[\s./:_-])pytest(?:$|[\s./:_-])", combined))


def parse_summary_report(text: str, case_id: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AtLogError(f"summary_report.xml 解析失败：{exc}") from exc
    suite = root if root.tag.split("}")[-1] == "testsuite" else next((node for node in root.iter() if node.tag.split("}")[-1] == "testsuite"), root)
    reported_tests = _xml_attr_int(suite, "tests") or _xml_attr_int(root, "tests")
    reported_failures = _xml_attr_int(suite, "failures") or _xml_attr_int(root, "failures")
    reported_errors = _xml_attr_int(suite, "errors") or _xml_attr_int(root, "errors")
    ignored = _xml_attr_int(suite, "ignored") or _xml_attr_int(suite, "skipped") or _xml_attr_int(root, "ignored") or _xml_attr_int(root, "skipped")
    start = str(suite.attrib.get("starttime") or suite.attrib.get("start_time") or root.attrib.get("starttime") or root.attrib.get("start_time") or "").strip()
    end = str(suite.attrib.get("endtime") or suite.attrib.get("end_time") or root.attrib.get("endtime") or root.attrib.get("end_time") or "").strip()

    raw_rows: list[dict[str, Any]] = []
    for node in root.iter("testcase"):
        name = str(node.attrib.get("name") or "").strip()
        classname = str(node.attrib.get("classname") or "").strip()
        message, body = _failure_message(node)
        result = str(node.attrib.get("result") or ("failed" if message or body else "passed")).lower()
        raw_rows.append({
            "name": name,
            "classname": classname,
            "time": str(node.attrib.get("time") or ""),
            "result": result,
            "failure_message": message,
            "failure_text": body,
        })

    target = str(case_id or "").strip().lower()
    matching_rows = [row for row in raw_rows if target and target in f"{row['classname']} {row['name']}".lower()]
    matching_non_pytest = [row for row in matching_rows if not _is_pytest_wrapper(row["classname"], row["name"])]
    if matching_non_pytest:
        candidate_rows = matching_non_pytest
    elif matching_rows:
        candidate_rows = matching_rows
    else:
        non_pytest_rows = [row for row in raw_rows if not _is_pytest_wrapper(row["classname"], row["name"])]
        candidate_rows = non_pytest_rows or raw_rows

    testcase_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidate_rows:
        normalized_name = re.sub(r"\[[^\]]*\]$", "", row["name"])
        key = f"{normalized_name}|{row['failure_message'] or row['failure_text'][:160]}".lower()
        if key in seen:
            continue
        seen.add(key)
        testcase_rows.append(row)

    failed_rows = [row for row in testcase_rows if row["failure_message"] or row["failure_text"] or row["result"] in {"failed", "failure", "error"}]
    preferred = failed_rows[0] if failed_rows else None
    operational_tests = len(testcase_rows)
    operational_failures = len(failed_rows)
    operational_errors = sum(1 for row in testcase_rows if row["result"] == "error")
    operational_ignored = sum(1 for row in testcase_rows if row["result"] in {"ignored", "skipped"})
    return {
        "tests": operational_tests or reported_tests,
        "failures": operational_failures,
        "errors": operational_errors,
        "reported_tests": reported_tests,
        "reported_failures": reported_failures,
        "reported_errors": reported_errors,
        "ignored": operational_ignored if testcase_rows else ignored,
        "start_time": start,
        "end_time": end,
        "testcases": testcase_rows,
        "failure": preferred,
        "status": "failed" if operational_failures or operational_errors else ("passed" if testcase_rows else ("failed" if reported_failures or reported_errors else "passed")),
    }


def parse_pytest_xml(text: str, case_id: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AtLogError(f"pytest XML 解析失败：{exc}") from exc
    failed: list[tuple[ET.Element, str, str]] = []
    for node in root.iter("testcase"):
        message, body = _failure_message(node)
        if message or body:
            failed.append((node, message, body))
    target = str(case_id or "").strip().lower()
    matching = [item for item in failed if target and target in f"{item[0].attrib.get('classname', '')} {item[0].attrib.get('name', '')}".lower()]
    matching_non_pytest = [
        item for item in matching
        if not _is_pytest_wrapper(str(item[0].attrib.get('classname') or ''), str(item[0].attrib.get('name') or ''))
    ]
    selected = (matching_non_pytest or matching)[0] if (matching_non_pytest or matching) else None
    if selected is None and failed:
        non_pytest = [
            item for item in failed
            if not _is_pytest_wrapper(str(item[0].attrib.get('classname') or ''), str(item[0].attrib.get('name') or ''))
        ]
        selected = (non_pytest or failed)[0]
    if selected is None:
        return {"failure_message": "", "failure_text": "", "location": None, "call_chain": [], "test_name": ""}
    node, message, body = selected
    call_chain: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for match in _PY_CALL_RE.finditer(body):
        item = (match.group("path"), int(match.group("line")), match.group("func"))
        if item not in seen:
            seen.add(item)
            call_chain.append({"file": item[0], "line": item[1], "function": item[2]})
    location = call_chain[-1] if call_chain else None
    if location is None:
        locations = list(_PY_LOCATION_RE.finditer(body))
        if locations:
            match = locations[-1]
            location = {"file": match.group("path"), "line": int(match.group("line")), "function": ""}
    return {
        "test_name": str(node.attrib.get("name") or ""),
        "classname": str(node.attrib.get("classname") or ""),
        "failure_message": message,
        "failure_text": body,
        "location": location,
        "call_chain": call_chain,
    }


def _clean_assertion(message: str) -> str:
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if not text:
        return ""
    match = re.search(r"\[AssertionError\]\s*(.*)$", text, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"AssertionError\s*[:：]?\s*(.*)$", text, re.I)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text


def _assertion_relation_cn(relation: str) -> str:
    normalized = re.sub(r"\s+", " ", str(relation or "")).strip().lower()
    mapping = [
        ("not equal", "不等于"),
        ("greater than or equal", "大于等于"),
        ("less than or equal", "小于等于"),
        ("greater than", "大于"),
        ("less than", "小于"),
        ("equal", "等于"),
        ("true", "为 True"),
        ("false", "为 False"),
        ("contain", "包含"),
        ("include", "包含"),
    ]
    for key, value in mapping:
        if key in normalized:
            return value
    return ""


def _extract_exception_type(text: str) -> str:
    source = str(text or "")
    match = re.search(r"\[?(?P<kind>[A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout))\]?", source)
    if not match:
        return ""
    return match.group("kind").split(".")[-1][:120]


def _dynamic_reason_category(
    text: str,
    *,
    error_msg: str = "",
    caller: str = "",
    clean: str = "",
) -> str:
    """Return a source-derived grouping label without guessing business semantics.

    Priority is deliberately data-driven: explicit error msg -> concrete exception type ->
    stable assertion text -> caller. No domain keyword taxonomy is embedded here.
    """
    explicit = re.sub(r"\s+", " ", str(error_msg or "")).strip(" ,，。:：;；")
    if explicit:
        return explicit[:160]

    exception_type = _extract_exception_type(text)
    if exception_type and exception_type.lower() != "assertionerror":
        return exception_type

    clean_text = re.sub(r"\s+", " ", str(clean or "")).strip()
    # Structured expect/real assertions without an error_msg do not contain a reliable
    # business cause. Prefer the concrete source signal rather than inventing one.
    if clean_text and not re.match(r"^expect\s*:", clean_text, re.I):
        candidate = re.split(r"\s*,\s*(?:caller|expect\s+to\s+be|error\s+msg)\s*:\s*", clean_text, maxsplit=1, flags=re.I)[0]
        candidate = candidate.strip(" ,，。:：;；")
        if candidate and candidate.lower() not in {"assertionerror", "error", "failed", "failure"}:
            return candidate[:160]

    if exception_type:
        if exception_type.lower() != "assertionerror" or not caller:
            return exception_type
    caller_text = re.sub(r"\s+", " ", str(caller or "")).strip(" ,，。:：;；")
    if caller_text:
        return caller_text[:160]
    return exception_type or "未归类"


def describe_assertion(message: str, failure_text: str = "") -> dict[str, str]:
    raw = re.sub(r"\s+", " ", str(message or "")).strip()
    clean = _clean_assertion(raw)
    expect_match = _ASSERTION_EXPECT_RE.search(clean)
    expect = expect_match.group("expect").strip() if expect_match else ""
    real = expect_match.group("real").strip() if expect_match else ""
    relation_match = _ASSERTION_RELATION_RE.search(clean)
    relation = relation_match.group(1).strip() if relation_match else ""
    caller_match = _ASSERTION_CALLER_RE.search(clean)
    caller = caller_match.group("caller").strip() if caller_match else ""
    error_match = _ASSERTION_ERROR_MSG_RE.search(clean)
    error_msg = error_match.group("error").strip().rstrip(" ,，。") if error_match else ""
    relation_cn = _assertion_relation_cn(relation)

    if expect or real:
        prefix = f"{error_msg}：" if error_msg else ""
        if relation_cn == "不等于":
            summary = f"{prefix}期望不等于 {expect or '指定值'}，但实际为 {real or '未知'}。"
        elif relation_cn in {"大于", "大于等于", "小于", "小于等于"}:
            summary = f"{prefix}期望{relation_cn} {expect or '指定值'}，但实际为 {real or '未知'}。"
        elif relation_cn in {"为 True", "为 False"}:
            summary = f"{prefix}期望{relation_cn}，但实际为 {real or '未知'}。"
        else:
            summary = f"{prefix}期望为 {expect or '未知'}，但实际为 {real or '未知'}。"
    else:
        summary = clean or raw

    combined = f"{raw} {failure_text}"
    category = _dynamic_reason_category(combined, error_msg=error_msg, caller=caller, clean=clean)
    return {
        "summary": summary[:2000],
        "category": category,
        "detail": (error_msg or clean or raw)[:1000],
        "expect": expect[:1000],
        "real": real[:1000],
        "relation": relation[:500],
        "relation_cn": relation_cn,
        "caller": caller[:1000],
        "error_msg": error_msg[:1000],
    }


def parse_xytest_errors(text: str, start_time: str = "", end_time: str = "") -> list[dict[str, Any]]:
    start = _parse_timestamp(start_time)
    end = _parse_timestamp(end_time)
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _XY_ERROR_RE.match(line.strip())
        if not match:
            continue
        level = match.group("level").strip().upper()
        if level not in {"ERROR", "FATAL", "CRITICAL", "FAIL", "FAILED"}:
            continue
        ts = _parse_timestamp(match.group("time"))
        if start and ts and ts < start - timedelta(minutes=2):
            continue
        if end and ts and ts > end + timedelta(minutes=2):
            continue
        rest = match.group("rest").strip()
        result.append({
            "time": match.group("time"),
            "level": level,
            "message": rest,
            "conclusion": _clean_assertion(rest),
            "raw": line.strip(),
        })
    return result


def parse_pytest_html_excerpt(text: str, case_id: str = "") -> str:
    parser = ResultTableTextParser()
    parser.feed(text)
    extracted = parser.text(case_id)
    if extracted and "no results found" not in extracted.lower():
        return extracted
    # Some pytest-html variants keep a filter placeholder as an early row. If the structured
    # selection fails, show the report's complete human-readable text instead of an empty result.
    fallback = VisibleHtmlTextParser()
    fallback.feed(text)
    visible = fallback.text()
    lines = [line for line in visible.splitlines() if "no results found" not in line.lower() and "try to check the filters" not in line.lower()]
    return "\n".join(lines).strip()


def parse_case_html_excerpt(text: str, case_id: str) -> str:
    """Return the visible section for the current case from multi-case helper reports."""
    parser = VisibleHtmlTextParser()
    parser.feed(text)
    visible = parser.text()
    target = str(case_id or "").strip().lower()
    if not target or target not in visible.lower():
        return visible
    lines = visible.splitlines()
    matches = [index for index, line in enumerate(lines) if target in line.lower()]
    if not matches:
        return visible
    start = max(0, matches[0] - 4)
    end = len(lines)
    for index in range(matches[-1] + 1, len(lines)):
        line = lines[index].strip()
        tokens = re.findall(r"\bSN_[A-Za-z0-9_]+", line, flags=re.I)
        if any(token.lower() != target for token in tokens):
            end = index
            break
    selected = lines[start:end]
    # A pytest wrapper is a duplicate presentation layer, not the physical case the user asked for.
    selected = [line for line in selected if line.strip().lower() != "pytest"]
    return "\n".join(selected).strip() or visible



def parse_environment_report_html(text: str) -> dict[str, Any]:
    """Parse 详细日志链接.html into topology and credential hints.

    The report usually exposes a big-network topology IP in each node heading
    and an internal business-network IP in a following ``user@ip password``
    line.  Keep both values.  For L4 nodes (SCH upper machine and DHH),
    TraceLens must use the topology IP as the environment host; the 192.*
    business address is retained only as report metadata.  GPB lower-machine
    behavior remains unchanged and may still use its reported business IP.
    """
    parser = VisibleHtmlTextParser()
    parser.feed(text or "")
    lines = [line.strip() for line in parser.text().splitlines() if line.strip()]
    result: dict[str, Any] = {
        "topology": "",
        "sim_mode": "",
        "environment_label": "",
        "upper": None,
        "dhh": None,
        "lowers": [],
    }
    current: dict[str, Any] | None = None
    for line in lines:
        info_match = re.search(r"环境信息\s*\[([^\]]+)\]", line, re.I)
        if info_match:
            result["environment_label"] = info_match.group(1).strip()
        topo_match = re.search(r"环境TOPO\s*[:：]\s*([^,，]+)", line, re.I)
        if topo_match:
            result["topology"] = topo_match.group(1).strip()
        sim_match = re.search(r"SIM模式\s*[:：]\s*([^,，\s]+)", line, re.I)
        if sim_match:
            result["sim_mode"] = sim_match.group(1).strip()

        header_match = re.match(r"(?P<label>上位机[^:：]*|数据服务器DHH[^:：]*|L3-[^:：]+)\s*[:：]\s*(?P<ip>\d{1,3}(?:\.\d{1,3}){3})", line, re.I)
        if header_match:
            label = header_match.group("label").strip()
            current = {
                "label": label,
                "topology_ip": header_match.group("ip"),
                "ssh_host": "",
                "username": "",
                "password": "",
            }
            lowered = label.lower()
            if "dhh" in lowered:
                current["kind"] = "dhh"
                result["dhh"] = current
            elif label.startswith("上位机"):
                current["kind"] = "upper"
                result["upper"] = current
            else:
                current["kind"] = "lower"
                result["lowers"].append(current)
            continue

        credential_match = re.search(
            r"(?P<user>[A-Za-z0-9_.-]+)@(?P<host>\d{1,3}(?:\.\d{1,3}){3})\s*[,，]?\s*(?P<password>[^\s<]+)",
            line, re.I,
        )
        if credential_match and current is not None:
            current["username"] = credential_match.group("user").strip()
            current["ssh_host"] = credential_match.group("host").strip()
            current["password"] = credential_match.group("password").strip().rstrip(',，')

    for node in [result.get("upper"), result.get("dhh"), *(result.get("lowers") or [])]:
        if not isinstance(node, dict):
            continue
        if not node.get("ssh_host"):
            node["ssh_host"] = node.get("topology_ip", "")
        if not node.get("username"):
            node["username"] = "root"
    return result


def _atlog_environment_host(node: dict[str, Any], role: str, *, station_type: str = "") -> str:
    """Return the host TraceLens should persist for an ATLog-derived machine.

    SCH upper and DHH are L4 nodes reachable through the report's big-network
    topology IP.  The ``user@192.*`` address printed below them is an internal
    business-network hint and must not replace the environment host.  GPB lower
    machines keep the existing business-IP-first behavior.
    """
    topology_ip = str(node.get("topology_ip") or "").strip()
    business_ip = str(node.get("ssh_host") or "").strip()
    if role == "upper" or station_type == "DHH":
        return topology_ip or business_ip
    return business_ip or topology_ip


def _machine_for_atlog_environment(node: dict[str, Any], role: str, *, station_type: str = ""):
    from apps.machines.models import AuthenticationType, Machine, MachineRole

    host = _atlog_environment_host(node, role, station_type=station_type)
    username = str(node.get("username") or "root").strip()
    if not host:
        raise AtLogError("详细日志链接.html 中缺少可用的 SSH IP。")
    expected_role = MachineRole.UPPER if role == "upper" else MachineRole.LOWER
    machine = Machine.objects.filter(host=host, ssh_port=22, username=username).first()
    if machine is not None and machine.role != expected_role:
        raise AtLogError(f"机器 {username}@{host} 已存在但角色不一致，不能自动录入 ATLog 环境。")
    if machine is None:
        machine = Machine(
            # ATLog 自动录入机器统一使用实际连接 IP 作为机器名称。
            # 节点角色/槽位名称继续保存在 station_name / relation metadata 中，
            # 避免机器列表出现 SCH/DHH/SLOT 等名称而无法一眼对应连接地址。
            name=host,
            host=host,
            ssh_port=22,
            username=username,
            role=expected_role,
            auth_type=AuthenticationType.PASSWORD if node.get("password") else AuthenticationType.NONE,
            station_type=station_type,
            station_name="DHH" if station_type == "DHH" else str(node.get("label") or ""),
            description=f"ATLog 自动化用例环境；拓扑IP={node.get('topology_ip') or ''}",
        )
    else:
        # 只规范 ATLog 自己创建过的机器名称，不能为了复用连接身份而
        # 擅自覆盖用户手工维护的机器名称。
        if str(machine.description or "").startswith("ATLog 自动化用例环境"):
            machine.name = host
        if station_type:
            machine.station_type = station_type
            machine.station_name = "DHH"
        if node.get("password"):
            machine.auth_type = AuthenticationType.PASSWORD
    if node.get("password"):
        machine.set_password(str(node["password"]))
    machine.full_clean()
    machine.save()
    return machine



def _public_environment_node(
    node: dict[str, Any] | None,
    *,
    role: str = "",
    station_type: str = "",
) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    kind = str(node.get("kind") or "").strip().lower()
    resolved_role = role or ("upper" if kind == "upper" else "lower")
    resolved_station_type = station_type or ("DHH" if kind == "dhh" else "")
    return {
        "label": str(node.get("label") or ""),
        "topology_ip": str(node.get("topology_ip") or ""),
        "ssh_host": str(node.get("ssh_host") or ""),
        "environment_host": _atlog_environment_host(node, resolved_role, station_type=resolved_station_type),
        "username": str(node.get("username") or ""),
        "has_password": bool(node.get("password")),
    }


def _public_environment_info(info: dict[str, Any], *, warning: str = "") -> dict[str, Any]:
    return {
        "created": False,
        "environment_id": None,
        "environment_name": "",
        "folder_name": "自动化用例环境",
        "topology": str(info.get("topology") or ""),
        "sim_mode": str(info.get("sim_mode") or ""),
        "warning": warning,
        "upper": _public_environment_node(info.get("upper"), role="upper"),
        "dhh": _public_environment_node(info.get("dhh"), role="lower", station_type="DHH"),
        "lowers": [_public_environment_node(node, role="lower") for node in (info.get("lowers") or []) if isinstance(node, dict)],
    }

def ensure_atlog_environment(info: dict[str, Any], base_url: str = "") -> dict[str, Any]:
    """Upsert one environment under the dedicated 自动化用例环境 folder.

    Passwords are encrypted by Machine.set_password and are never returned to
    the browser. Re-analyzing another case from the same topology reuses the
    existing machine/environment instead of creating duplicates.
    """
    upper_node = info.get("upper") if isinstance(info, dict) else None
    if not isinstance(upper_node, dict) or not (upper_node.get("topology_ip") or upper_node.get("ssh_host")):
        return _public_environment_info(info if isinstance(info, dict) else {}, warning="未从详细日志链接.html 提取到上位机连接信息。")

    # The SCH upper-machine environment host comes from the heading's topology IP
    # (normally the reachable 10.x big-network address).  The 192.* business IP is
    # preserved only as report metadata and must not block automatic environment import.
    upper_environment_host = _atlog_environment_host(upper_node, "upper")
    if upper_environment_host.startswith("192."):
        return _public_environment_info(info, warning=f"上位机拓扑 IP {upper_environment_host} 属于 192.* 小网，未自动录入环境。")

    from django.db import transaction
    from django.utils import timezone
    from apps.environments.models import Environment, EnvironmentFolder, EnvironmentStatus, MachineRelation, RelationSource

    with transaction.atomic():
        folder, _ = EnvironmentFolder.objects.get_or_create(parent=None, name="自动化用例环境", defaults={"sort_order": 900})
        upper = _machine_for_atlog_environment(upper_node, "upper")
        environment = Environment.objects.filter(upper_machine=upper).first()
        created = environment is None
        if environment is None:
            label = str(info.get("environment_label") or info.get("topology") or upper_node.get("topology_ip") or upper.host).strip()
            environment = Environment.objects.create(
                name=f"ATLog-{label}",
                upper_machine=upper,
                folder=folder,
                status=EnvironmentStatus.READY,
                last_discovered_at=timezone.now(),
                description=f"自动从 ATLog 详细日志链接提取；报告={base_url}",
            )
        else:
            # Reuse an already managed environment without silently moving it out of a
            # user's existing folder. Environments previously created by ATLog continue
            # to live in the dedicated folder and receive the latest report pointer.
            auto_managed = str(environment.description or "").startswith("自动从 ATLog 详细日志链接提取")
            update_fields: list[str] = []
            if environment.folder_id is None or auto_managed:
                environment.folder = folder
                update_fields.append("folder")
            if auto_managed:
                environment.status = EnvironmentStatus.READY
                environment.last_discovered_at = timezone.now()
                environment.description = f"自动从 ATLog 详细日志链接提取；报告={base_url}"
                update_fields.extend(["status", "last_discovered_at", "description"])
            if update_fields:
                environment.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])

        desired_machine_ids: set[int] = set()
        nodes: list[tuple[dict[str, Any], str]] = []
        if isinstance(info.get("dhh"), dict):
            nodes.append((info["dhh"], "DHH"))
        nodes.extend((node, "") for node in (info.get("lowers") or []) if isinstance(node, dict))
        for node, station_type in nodes:
            machine = _machine_for_atlog_environment(node, "lower", station_type=station_type)
            desired_machine_ids.add(machine.id)
            metadata = {
                "station_name": "dhh" if station_type == "DHH" else str(node.get("label") or ""),
                "station_type": station_type or "GPB",
                "topology_ip": str(node.get("topology_ip") or ""),
                "source": "atlog",
            }
            MachineRelation.objects.update_or_create(
                environment=environment, target_machine=machine,
                defaults={
                    "source_machine": upper,
                    "source": RelationSource.MANUAL,
                    "is_active": True,
                    "discovered_at": timezone.now(),
                    "metadata": metadata,
                },
            )
        # Do not deactivate relations that were manually/XML managed. ATLog reports
        # are a source of additional topology evidence, not authority to delete an
        # existing environment relationship.


    return {
        "created": created,
        "environment_id": environment.id,
        "environment_name": environment.name,
        "folder_name": environment.folder.name if environment.folder_id else folder.name,
        "topology": str(info.get("topology") or ""),
        "sim_mode": str(info.get("sim_mode") or ""),
        "upper": _public_environment_node(info.get("upper"), role="upper"),
        "dhh": _public_environment_node(info.get("dhh"), role="lower", station_type="DHH"),
        "lowers": [_public_environment_node(node, role="lower") for node in (info.get("lowers") or []) if isinstance(node, dict)],
    }


def list_case_directory(base_url: str, relative_path: str = "") -> dict[str, Any]:
    base = normalize_base_url(base_url)
    relative = str(relative_path or "").strip().lstrip("/")
    if relative and not relative.endswith("/"):
        relative += "/"
    url = _safe_child_url(base, relative) if relative else base
    entries = list_directory(url)
    payload = []
    for item in entries:
        if item in {"../", "./"}:
            continue
        payload.append({
            "name": item.rstrip("/"),
            "is_dir": item.endswith("/"),
            "relative_path": f"{relative}{item}",
            "url": urllib.parse.urljoin(url, item),
        })
    return {"base_url": base, "relative_path": relative, "url": url, "entries": payload}

def _choose_pytest_xml(files: Iterable[str]) -> list[str]:
    values = [item for item in files if re.search(r"(?:^|/)pytest-[^/]*\.xml$", item, re.I)]
    return sorted(values, reverse=True)[:8]


def _choose_test_html(files: Iterable[str], case_id: str) -> str:
    expected = f"test_{case_id.lower()}.html"
    for item in files:
        if item.rstrip("/").split("/")[-1].lower() == expected:
            return item
    candidates = [item for item in files if re.search(r"(?:^|/)test_.*\.html$", item, re.I)]
    return sorted(candidates)[0] if candidates else ""


def _evidence(source: str, url: str, excerpt: str, *, time: str = "", level: str = "", module: str = "") -> dict[str, str]:
    return {"source": source, "url": url, "time": time, "level": level, "module": module, "excerpt": excerpt[:4000]}


def analyze_case(base_url: str) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    case_id = case_id_from_url(base)
    warnings: list[str] = []
    try:
        root_files = list_directory(base)
    except Exception as exc:
        raise AtLogError(f"无法枚举用例目录：{exc}") from exc
    file_set = {item.rstrip("/") for item in root_files}
    links: dict[str, str] = {"base": base}
    for known in ["summary_report.xml", "summary.ini", "event.log", "xytest.log", "details_report.html", "failures_report.html", "summary_report.html", "详细日志链接.html"]:
        if known in file_set:
            links[known] = _case_root_file_url(base, known)
    if "full_logs" in file_set or "full_logs/" in root_files:
        links["full_logs"] = _safe_child_url(base, "full_logs/")
    summary: dict[str, Any] | None = None
    summary_url = links.get("summary_report.xml")
    if summary_url:
        try:
            summary = parse_summary_report(fetch_text(summary_url, MAX_XML_BYTES), case_id)
        except Exception as exc:
            warnings.append(str(exc))

    status = summary.get("status", "unknown") if summary else "unknown"
    start_time = str(summary.get("start_time") or "") if summary else ""
    end_time = str(summary.get("end_time") or "") if summary else ""
    failure_message = ""
    failure_text = ""
    if summary and summary.get("failure"):
        failure_message = str(summary["failure"].get("failure_message") or "")
        failure_text = str(summary["failure"].get("failure_text") or "")

    result_files: list[str] = []
    needs_failure_detail = status != "passed" or not summary
    if needs_failure_detail and ("result/" in root_files or "result" in file_set):
        try:
            result_files = [f"result/{item}" for item in list_directory(_safe_child_url(base, "result/")) if not item.endswith("/")]
        except Exception as exc:
            warnings.append(f"result/ 枚举失败：{exc}")
    pytest_detail: dict[str, Any] = {"failure_message": "", "failure_text": "", "location": None, "call_chain": []}
    pytest_url = ""
    if needs_failure_detail:
        for relative in _choose_pytest_xml(result_files):
            url = _safe_child_url(base, relative)
            try:
                detail = parse_pytest_xml(fetch_text(url, MAX_XML_BYTES), case_id)
            except Exception as exc:
                warnings.append(str(exc))
                continue
            if detail.get("failure_message") or detail.get("failure_text"):
                pytest_detail = detail
                pytest_url = url
                links["pytest_xml"] = url
                status = "failed"
                break
    if pytest_detail.get("failure_message"):
        failure_message = str(pytest_detail["failure_message"])
    if pytest_detail.get("failure_text"):
        failure_text = str(pytest_detail["failure_text"])

    evidence: list[dict[str, str]] = []
    if summary_url and failure_message:
        evidence.append(_evidence("summary_report.xml", summary_url, failure_message))
    if pytest_url and (failure_message or failure_text):
        evidence.append(_evidence("pytest XML", pytest_url, failure_message or failure_text[:1000]))

    xy_errors: list[dict[str, Any]] = []
    xy_url = links.get("xytest.log") or ""
    if status != "passed":
        xy_candidates = [xy_url] if xy_url else []
        for ancestor in _ancestor_case_urls(base):
            candidate = urllib.parse.urljoin(ancestor, "xytest.log")
            if candidate not in xy_candidates:
                xy_candidates.append(candidate)
        last_xy_error: Exception | None = None
        for candidate in xy_candidates:
            try:
                xy_text = fetch_tail_text(candidate)
            except FileNotFoundError as exc:
                last_xy_error = exc
                continue
            except Exception as exc:
                last_xy_error = exc
                continue
            xy_url = candidate
            links["xytest.log"] = candidate
            xy_errors = parse_xytest_errors(xy_text, start_time, end_time)
            if xy_errors or xy_text:
                break
        if not xy_url and last_xy_error:
            warnings.append(f"xytest.log 读取失败：{last_xy_error}")
    preferred_xy = next((item for item in reversed(xy_errors) if "AssertionError" in item["message"]), None)
    if preferred_xy is None and xy_errors:
        preferred_xy = xy_errors[-1]
    failure_time = str(preferred_xy.get("time") or "") if preferred_xy else ""
    if preferred_xy:
        evidence.append(_evidence("xytest.log", xy_url or "", preferred_xy["raw"], time=failure_time, level=preferred_xy["level"]))

    if status == "unknown" and xy_errors:
        status = "failed"
    if status == "unknown" and summary and not summary.get("failures") and not summary.get("errors"):
        status = "passed"

    test_html_relative = _choose_test_html(root_files, case_id)
    if test_html_relative:
        links["test_html"] = _safe_child_url(base, test_html_relative)
    report_excerpt = ""
    # Failed-case HTML is a first-class report fact for AI diagnosis.  Parse the
    # visible test-case excerpt even when XML already supplied the assertion so
    # the Agent can correlate pytest HTML context with xytest/event/runtime logs.
    # The parser and 16k cap keep the heavy HTML body out of model context.
    if status != "passed" and test_html_relative:
        try:
            report_excerpt = parse_pytest_html_excerpt(fetch_text(links["test_html"], MAX_TEXT_BYTES), case_id)[:16000]
            if report_excerpt:
                evidence.append(_evidence("pytest HTML", links["test_html"], report_excerpt[:1800]))
        except Exception as exc:
            warnings.append(f"pytest HTML 解析失败：{exc}")

    failure_tail = failure_text.splitlines()[-1] if failure_text.splitlines() else ""
    assertion_source = failure_message or (preferred_xy["message"] if preferred_xy else "") or failure_tail
    assertion = _clean_assertion(assertion_source)
    assertion_diagnosis = describe_assertion(assertion_source, failure_text) if status != "passed" else {
        "summary": "用例执行通过。", "category": "通过", "detail": "", "expect": "", "real": "",
        "relation": "", "relation_cn": "", "caller": "", "error_msg": "",
    }
    if status == "passed":
        conclusion = "用例执行通过。"
    elif assertion:
        conclusion = assertion_diagnosis["summary"] or assertion
    elif failure_message:
        conclusion = failure_message
    elif preferred_xy:
        conclusion = preferred_xy["conclusion"] or preferred_xy["message"]
    elif status == "failed":
        conclusion = "报告判定用例失败，但暂未提取到明确断言。"
    else:
        conclusion = "未找到可机器判定的汇总结果，请展开查看报告产物。"

    environment_info: dict[str, Any] = {}
    # Do not depend exclusively on nginx autoindex discovering a Chinese file name.
    # Some nginx configurations percent-encode/nonstandard-render the href, while the
    # report itself is still directly reachable at the canonical fixed path. Probe the
    # canonical HTML file even when it was absent from the directory listing.
    environment_url = links.get("详细日志链接.html") or _safe_child_url(base, "详细日志链接.html")
    try:
        environment_html = fetch_text(environment_url, MAX_TEXT_BYTES)
    except FileNotFoundError:
        environment_html = ""
        environment_url = ""
    except Exception as exc:
        environment_html = ""
        # Only surface a fetch warning when nginx told us the file existed. Otherwise
        # this is merely a best-effort canonical probe and should stay quiet.
        if "详细日志链接.html" in links:
            warnings.append(f"详细日志链接.html 读取失败：{exc}")
        environment_url = ""
    if environment_url and environment_html:
        links["详细日志链接.html"] = environment_url
        try:
            environment_raw = parse_environment_report_html(environment_html)
            environment_info = ensure_atlog_environment(environment_raw, base)
        except Exception as exc:
            warnings.append(f"自动化用例环境解析/录入失败：{exc}")

    # Determine a useful default event window. Exact xytest failure point wins; otherwise case end.
    anchor = _parse_timestamp(failure_time) or _parse_timestamp(end_time) or _parse_timestamp(start_time)
    if anchor:
        event_start = _format_time(anchor - timedelta(seconds=30))
        event_end = _format_time(anchor + timedelta(seconds=15))
    else:
        event_start, event_end = start_time, end_time

    try:
        log_catalog = discover_case_log_catalog(base)
    except Exception as exc:
        logger.warning("atlog.case_catalog.failed base=%s error=%s", base, exc)
        log_catalog = []

    return {
        "case_id": case_id,
        "case_name": case_id,
        "base_url": base,
        "host": urllib.parse.urlsplit(base).netloc,
        "status": status,
        "conclusion": conclusion[:2000],
        "assertion": assertion[:4000],
        "assertion_summary": assertion_diagnosis["summary"],
        "reason_category": assertion_diagnosis["category"],
        "reason_detail": assertion_diagnosis["detail"],
        "assertion_meta": {
            "expect": assertion_diagnosis["expect"],
            "real": assertion_diagnosis["real"],
            "relation": assertion_diagnosis["relation"],
            "relation_cn": assertion_diagnosis["relation_cn"],
            "caller": assertion_diagnosis["caller"],
            "error_msg": assertion_diagnosis["error_msg"],
        },
        "start_time": start_time,
        "end_time": end_time,
        "failure_time": failure_time,
        "event_start_time": event_start,
        "event_end_time": event_end,
        "summary": summary or {},
        "failure_location": pytest_detail.get("location"),
        "call_chain": pytest_detail.get("call_chain") or [],
        "failure_text": failure_text[:24000],
        "report_excerpt": report_excerpt,
        "xytest_errors": xy_errors[-30:],
        "links": links,
        "files": sorted(root_files),
        "result_files": sorted(result_files),
        "evidence": evidence,
        "warnings": warnings,
        "environment": environment_info,
        "log_catalog": log_catalog,
    }


# --------------------------------------------------------------------------- 单文件报告分析

_REPORT_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL", "FAIL", "FAILED"}
_REPORT_ANY_LEVELS = _REPORT_ERROR_LEVELS | {"WARN", "WARNING", "INFO", "DEBUG", "TRACE", "NOTICE"}
# 报告 URL 路径里这些目录名只是"装东西的容器"，不是用例编号
_REPORT_CONTAINER_DIRS = {
    "result", "results", "full_logs", "full-log", "logs", "log", "debug", "elog",
    "report", "reports", "cpd_report", "cpd_data", "data", "run", "output", "outputs",
    "attachment", "attachments", "static", "files", "dist", "tmp",
}
# 用例编号形态：SN_SPM_RSPMCPD_DSPRSAR_MalFunc_001 / CASENAME_Alpha_01 ...
_REPORT_CASE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,11}(?:_[A-Za-z0-9]+){2,}$")


def _report_file_name(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/", 1)[-1] or "report"


def _report_case_id(url: str, explicit: str = "") -> str:
    """用例编号：优先用调用方给的；否则沿路径向上找，跳过 result/full_logs/debug 这类容器目录。

    取"最靠近文件、且形如用例编号"的那一层；都不像就从最近的非容器目录兜底，
    最后才退化成文件名主干。
    """
    if str(explicit or "").strip():
        return str(explicit).strip()[:255]
    parts = [
        urllib.parse.unquote(item)
        for item in urllib.parse.urlsplit(url).path.rstrip("/").split("/")
        if item
    ]
    if not parts:
        return ""
    ancestors = list(reversed(parts[:-1]))  # 由近及远，去掉文件名本身
    candidates = [item for item in ancestors if item.lower() not in _REPORT_CONTAINER_DIRS]
    for item in candidates:
        if _REPORT_CASE_ID_RE.match(item):
            return item
    if candidates:
        return candidates[0]
    return parts[-1].rsplit(".", 1)[0]


def _leading_groups(text: str) -> tuple[list[str], str]:
    """拆出行首连续的 ``[..]`` 分组，返回 (分组列表, 其后的自由文本)。"""
    groups: list[str] = []
    cursor = 0
    while cursor < len(text) and text[cursor] == "[":
        close = text.find("]", cursor + 1)
        if close < 0:
            break
        groups.append(text[cursor + 1:close].strip())
        cursor = close + 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return groups, text[cursor:].strip()


def _report_kind(name: str, text: str) -> str:
    """按扩展名 + 行结构判断报告类型。

    日志类必须靠**字段结构**区分，不能只看"有没有 ERROR 行"：
      * 十三字段运行事件日志  → event_log
      * 八字段子系统调试日志  → debug_log
      * ``[时间] [级别] [文件:行] 说明``  → xytest_log
    """
    lowered = name.lower()
    if lowered.endswith(".xml"):
        return "junit_xml" if re.search(r"<(?:failure|error)\b", text, re.I) else "summary_xml"
    if lowered.endswith((".html", ".htm")):
        slim = text.lower().replace("'", '"')
        return "pytest_html" if 'id="results-table"' in slim else "html"
    if lowered.endswith(".rpt"):
        return "cpd_report"
    if lowered.endswith((".xlsx", ".xlsm", ".xls")):
        return "spreadsheet"
    if lowered.endswith((".ini", ".cfg")):
        return "ini"
    if lowered.endswith((".log", ".txt", ".out")):
        for line in text.splitlines()[:40]:
            stripped = line.strip()
            if not stripped:
                continue
            groups, _ = _leading_groups(stripped)
            if len(groups) < 2 or _parse_timestamp(groups[0]) is None:
                continue
            if len(groups) >= 12:
                return "event_log"
            if len(groups) >= 6:
                return "debug_log"
            if len(groups) >= 3 and re.search(r"\.py:\d+$|:\w+:\d+$", groups[2]):
                return "xytest_log"
            if groups[1].strip().upper() in _REPORT_ANY_LEVELS:
                return "xytest_log"
            break
        return "log"
    return "text"


def _report_findings(text: str, kind: str = "", limit: int = 200) -> list[dict[str, str]]:
    """统一抽取"值得注意的行"。

    结构化日志（xytest / 调试 / 事件）只认行首 ``[..]`` 分组里的**级别字段**；
    自由文本才允许按大写关键词兜底，且要求出现在行首附近——避免把
    "position error within tolerance" 这类正文里的 error 当成错误行。
    """
    structured = kind in {"xytest_log", "debug_log", "event_log"}
    findings: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        groups, rest = _leading_groups(stripped)
        time_value = groups[0] if groups and _parse_timestamp(groups[0]) is not None else ""
        level = ""
        level_index = -1
        scan = groups[1:] if time_value else groups
        for offset, item in enumerate(scan):
            token = item.strip().upper()
            if token in _REPORT_ERROR_LEVELS:
                level = token
                level_index = offset + (1 if time_value else 0)
                break
        if level:
            if len(groups) <= 4:
                # xytest 形态：级别后紧跟 [file.py:line] [ErrorClass] 说明，全部保留
                tail = " ".join(f"[{item}]" for item in groups[level_index + 1:])
                message = f"{tail} {rest}".strip()
            else:
                message = stripped
            findings.append({"time": time_value, "level": level, "message": message[:600]})
        elif not structured:
            head = stripped[:90]
            if re.search(r"\b(?:ERROR|FATAL|CRITICAL|ALARM)\b", head):
                stamp = _TIMESTAMP_RE.search(stripped)
                findings.append({
                    "time": stamp.group(1) if stamp else "",
                    "level": "ERROR",
                    "message": stripped[:600],
                })
        if len(findings) >= limit:
            break
    return findings


def _spreadsheet_sections(raw: bytes, *, max_rows: int = 200, max_cols: int = 40) -> list[dict[str, Any]]:
    """把一份测校数据表格解析成"列名 + 数据行"的预览。

    只取前几个工作表、每表前 ``max_rows`` 行，避免大表把接口撑爆。
    """
    import io

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    sections: list[dict[str, Any]] = []
    try:
        for sheet in book.worksheets[:3]:
            rows: list[list[Any]] = []
            for row in sheet.iter_rows(values_only=True):
                if all(cell is None for cell in row):
                    continue
                rows.append([_cell_text(cell) for cell in list(row)[:max_cols]])
                if len(rows) > max_rows + 1:
                    break
            header = rows[0] if rows else []
            sections.append({
                "name": sheet.title,
                "columns": [str(item) for item in header],
                "table": rows[1:max_rows + 1],
                "row_count": max(0, len(rows) - 1),
                "truncated": len(rows) > max_rows + 1,
            })
    finally:
        book.close()
    return sections


def _cell_text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


def _spreadsheet_conclusion(name: str, sections: list[dict[str, Any]]) -> tuple[str, str]:
    """从表格里读出结论：行数、时间范围、非 OK 行数。"""
    if not sections:
        return "unknown", f"{name}：表格里没有可读的工作表。"
    sheet = sections[0]
    columns = [str(item).casefold() for item in sheet.get("columns") or []]
    table = sheet.get("table") or []
    stamp_index = next(
        (index for index, item in enumerate(columns) if item in {"timestamp", "time", "date", "时间", "时间戳"}),
        None,
    )
    result_index = next((index for index, item in enumerate(columns) if item == "result"), None)
    times = [
        str(row[stamp_index]) for row in table
        if stamp_index is not None and stamp_index < len(row) and str(row[stamp_index] or "").strip()
    ]
    bad_rows = 0
    if result_index is not None:
        bad_rows = sum(
            1 for row in table
            if result_index < len(row) and str(row[result_index] or "").strip().upper() not in {"", "OK", "PASS", "PASSED"}
        )
    span = f"{times[0]} → {times[-1]}" if times else "未识别到时间列"
    text = (
        f"{name}：测校数据表「{sheet.get('name')}」{len(table)} 行 × {len(sheet.get('columns') or [])} 列，"
        f"时间范围 {span}"
    )
    if result_index is not None:
        text += f"，其中非 OK 行 {bad_rows} 行"
    text += "。"
    if bad_rows:
        return "failed", text
    return ("passed" if table else "unknown"), text


def analyze_report_file(url: str, *, case_id: str = "") -> dict[str, Any]:
    """按 URL 分析**单个报告文件**。

    与 ``analyze_case``（按用例目录整体分析）互补：这里只给一个报告文件的链接，
    按类型分派到已有的解析器，回答"这份报告说了什么"。沿用同一条 SSRF 约束：
    只允许内网/回环地址。
    """
    target = str(url or "").strip()
    if not target:
        raise AtLogError("报告文件 URL 不能为空。")
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AtLogError("仅支持 http/https 报告链接。")
    if not _is_private_host(parsed.hostname):
        raise AtLogError("报告 URL 必须指向内网地址，已拒绝访问公网主机。")

    name = _report_file_name(target)
    lowered = name.lower()
    # 表格是二进制通道：不能走文本抓取，得整份下来交给 openpyxl
    binary = lowered.endswith((".xlsx", ".xlsm", ".xls"))
    log_like = lowered.endswith((".log", ".txt", ".out"))
    raw = b""
    if binary:
        try:
            raw = _request(target, max_bytes=MAX_TEXT_BYTES).content
        except FileNotFoundError as exc:
            raise AtLogError(f"报告文件不存在：{target}") from exc
        text = ""
    else:
        try:
            # 日志类用尾部读取：错误通常在末尾，且大文件不会整份拉下来
            text = fetch_tail_text(target) if log_like else fetch_text(target, MAX_TEXT_BYTES)
        except FileNotFoundError as exc:
            raise AtLogError(f"报告文件不存在：{target}") from exc

    kind = _report_kind(name, text)
    resolved_case = _report_case_id(target, case_id)
    notes: list[str] = []
    custom_conclusion = ""
    payload: dict[str, Any] = {
        "url": target,
        "file_name": name,
        "kind": kind,
        "case_id": resolved_case,
        "line_count": len(text.splitlines()),
        "char_count": len(raw) if binary else len(text),
        "findings": _report_findings(text, kind),
        "summary": None,
        "pytest": None,
        "cpd_report": None,
        "sections": [],
        "assertion": "",
        "text_excerpt": "",
        "notes": notes,
    }

    if kind in {"junit_xml", "summary_xml"}:
        try:
            payload["summary"] = parse_summary_report(text, resolved_case)
        except AtLogError as exc:
            notes.append(str(exc))
        try:
            detail = parse_pytest_xml(text, resolved_case)
        except AtLogError as exc:
            notes.append(str(exc))
            detail = {}
        if detail.get("failure_message") or detail.get("failure_text"):
            payload["pytest"] = detail
        summary = payload["summary"] or {}
        payload["status"] = summary.get("status") or ("failed" if payload["pytest"] else "unknown")
        payload["assertion"] = (
            (payload["pytest"] or {}).get("failure_message")
            or ((summary.get("failure") or {}).get("failure_message") or "")
        )
        payload["text_excerpt"] = ""

    elif kind == "pytest_html":
        excerpt = parse_pytest_html_excerpt(text, resolved_case)
        if not excerpt:
            excerpt = parse_case_html_excerpt(text, resolved_case)
            notes.append("未命中 results-table 结构，已按整页可见文本提取。")
        payload["text_excerpt"] = excerpt[:16000]
        payload["assertion"] = _clean_assertion(excerpt)
        payload["status"] = "failed" if re.search(r"FAILED|AssertionError", excerpt, re.I) else "unknown"

    elif kind == "cpd_report":
        # 复用 CPD 报告解析器（跨应用只读导入）
        from apps.reports.parser import parse_report_summary as parse_cpd_report

        report = parse_cpd_report(text, file_name=name, full_path=target)
        payload["cpd_report"] = report
        payload["status"] = "passed" if str(report.get("test_run_result") or "").upper() == "OK" else "failed"
        payload["text_excerpt"] = ""

    elif kind == "spreadsheet":
        try:
            payload["sections"] = _spreadsheet_sections(raw)
            payload["status"], custom_conclusion = _spreadsheet_conclusion(name, payload["sections"])
            payload["line_count"] = (payload["sections"][0].get("row_count") or 0) if payload["sections"] else 0
        except Exception as exc:  # openpyxl 对损坏/受保护的表会抛各种异常
            logger.warning("atlog.report_file.spreadsheet_failed url=%s error=%s", target, exc)
            payload["status"] = "unknown"
            custom_conclusion = f"{name}：这份表格无法解析（{exc}）。"
        payload["text_excerpt"] = ""

    elif kind == "ini":
        values: dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line and not line.strip().startswith(("#", ";")):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        payload["sections"] = [{"name": "ini", "rows": [{"key": k, "value": v} for k, v in values.items()]}]
        payload["status"] = str(values.get("status") or "unknown")
        custom_conclusion = f"{name}：读取到 {len(values)} 项配置，status={payload['status']}，请展开原文核对。"

    else:  # xytest_log / debug_log / event_log / log / html / text
        lines = text.splitlines()
        window = lines[-400:] if kind in {"xytest_log", "debug_log", "event_log", "log"} else lines[:400]
        payload["text_excerpt"] = "\n".join(window)[:16000]
        payload["status"] = "failed" if payload["findings"] else "unknown"
        payload["assertion"] = payload["findings"][0]["message"] if payload["findings"] else ""

    if custom_conclusion:
        payload["conclusion"] = custom_conclusion
    elif payload["status"] == "passed":
        payload["conclusion"] = f"{name}：报告判定通过。"
    elif payload["assertion"]:
        payload["conclusion"] = f"{name}：报告判定失败 —— {payload['assertion'][:200]}"
    elif payload["findings"]:
        first = payload["findings"][0]
        payload["conclusion"] = (
            f"{name}：扫描到 {len(payload['findings'])} 条异常行，"
            f"最早 {first['time'] or '（无时间戳）'}：{first['message'][:120]}"
        )
    elif kind == "cpd_report" and payload["cpd_report"]:
        report = payload["cpd_report"]
        payload["conclusion"] = (
            f"{name}：CPD {report.get('cpd_name') or ''} 测校结果 "
            f"{report.get('test_run_result') or 'UNKNOWN'}，"
            f"{report.get('start_time') or '-'} → {report.get('stop_time') or '-'}。"
        )
    else:
        payload["conclusion"] = f"{name}：未从报告内容中判定出成功或失败，请展开原文查看。"
    return payload


def _normalize_event_level(tokens: Iterable[str]) -> str:
    values = [str(item or "").strip().upper() for item in tokens]
    for value in values:
        if any(word in value for word in ("ERROR", "FATAL", "CRITICAL", "ALARM")):
            return "ERROR"
    for value in values:
        if "WARN" in value:
            return "WARNING"
    for value in values:
        if "EVENT" in value or value == "INFO":
            return "EVENT"
    return next((value for value in values if value), "")


def _event_row(line: str, line_number: int) -> dict[str, Any]:
    text = line.strip()
    groups: list[str] = []
    cursor = 0
    while cursor < len(text) and text[cursor:cursor + 1] == "[":
        close = text.find("]", cursor + 1)
        if close < 0:
            break
        groups.append(text[cursor + 1:close].strip())
        cursor = close + 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    if groups and _parse_timestamp(groups[0]) is not None:
        return {
            "line_number": line_number,
            "time": groups[0],
            "component": groups[1] if len(groups) > 1 else "",
            "level": _normalize_event_level(groups[4:]),
            "source": groups[3] if len(groups) > 3 else "",
            "current_event_code": groups[6] if len(groups) > 6 else "",
            "linked_event_codes": groups[7] if len(groups) > 7 else "",
            "current_err_iid": groups[8] if len(groups) > 8 else "",
            "linked_err_iids": groups[9] if len(groups) > 9 else "",
            "display_code": groups[10] if len(groups) > 10 else "",
            "linked_display_codes": groups[11] if len(groups) > 11 else "",
            "event_type": groups[12] if len(groups) > 12 else "",
            "message": text[cursor:].strip(),
            "raw": line,
        }
    ts_match = _TIMESTAMP_RE.search(line)
    return {
        "line_number": line_number,
        "time": ts_match.group(1) if ts_match else "",
        "component": "",
        "level": "",
        "source": "",
        "current_event_code": "",
        "linked_event_codes": "",
        "current_err_iid": "",
        "linked_err_iids": "",
        "display_code": "",
        "linked_display_codes": "",
        "event_type": "",
        "message": line.strip(),
        "raw": line,
    }



def _generic_log_row(line: str, line_number: int, *, component_hint: str = "", source_path: str = "") -> dict[str, Any]:
    """Parse a regular full_logs line into the common ATLog log-row shape.

    This deliberately stays format-light. The frontend still applies TraceLens runtime
    log-format rules to ``raw`` for the final rendering, while the backend only needs
    enough structure for time filtering, component filtering and timeline ordering.
    """
    groups = re.findall(r"\[([^\]]+)\]", line)
    ts_match = _TIMESTAMP_RE.search(line)
    timestamp = ts_match.group(1) if ts_match else ""
    level = ""
    for value in groups[1:8]:
        candidate = str(value).strip().upper()
        if candidate in {"TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL", "ALARM", "EVENT", "EVT", "FAIL", "FAILED"}:
            level = "WARNING" if candidate == "WARN" else "ERROR" if candidate in {"FATAL", "CRITICAL", "ALARM", "FAIL", "FAILED"} else candidate
            break
    component = str(component_hint or "").strip()
    if not component and len(groups) > 1 and not re.fullmatch(r"\d+", groups[1].strip()):
        component = groups[1].strip()
    message = line.strip()
    if groups:
        last = line.rfind("]")
        if last >= 0 and last + 1 < len(line):
            tail = line[last + 1 :].strip()
            if tail:
                message = tail
    return {
        "line_number": line_number,
        "time": timestamp,
        "component": component,
        "level": level,
        "source": source_path,
        "message": message,
        "raw": line,
        "source_kind": "full",
        "source_path": source_path,
    }


def _component_from_log_path(relative_path: str) -> str:
    name = relative_path.rstrip("/").split("/")[-1]
    for suffix in (".tar.gz", ".log.gz", ".txt.gz", ".log", ".txt", ".out"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Many archived report files append a pure timestamp/version suffix. Keep the
    # stable module prefix for the component picker when that pattern is obvious.
    name = re.sub(r"[_-](?:20\d{6,}|\d{12,})$", "", name)
    return name or relative_path.rstrip("/").split("/")[-1]


_ATLOG_PATH_MARKERS = {"full_logs", "log", "debug", "elog", "executor", "exec", "run", "operation"}


def _is_ip_path_segment(value: str) -> bool:
    text = str(value or "").strip().strip("/")
    if not text:
        return False
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _is_case_catalog_name(value: str) -> bool:
    """Path routing tokens such as IP/elog are never hierarchy entities."""
    text = str(value or "").strip().strip("/")
    if not text or _is_ip_path_segment(text):
        return False
    return text.lower() not in _ATLOG_PATH_MARKERS


@lru_cache(maxsize=128)
def _discover_case_log_files_cached(base_url: str) -> tuple[tuple[str, str], ...]:
    """Enumerate ATLog runtime logs using the fixed report directory contract.

    Physical ATLog layouts are deliberately separated by source type::

        full_logs/log/debug/<subsystem>/<module>.log
        full_logs/log/debug/<subsystem>/<module>/<file>.log
        full_logs/log/debug/elog/<lower-ip>/<subsystem>/<module>_cp*.log
        full_logs/log/elog/<lower-ip>/<subsystem>/<module>_cp*.log  # legacy mirror

    ``elog`` is the only execution-log tree.  There is no ``executor`` directory.
    The executor walk is bounded to IPv4 folders and one subsystem level so a
    malformed report cannot turn this into a generic recursive crawler.
    """
    base = normalize_base_url(base_url)
    debug_root = "full_logs/log/debug/"
    # The runtime log-positioning profile stores execution logs below debug/elog.
    # Some older ATLog exports copied elog directly under log/, so keep that as
    # an explicit compatibility root rather than a recursive fallback.
    elog_roots = (
        "full_logs/log/debug/elog/",
        "full_logs/log/elog/",
    )
    max_files = 720
    discovered: list[tuple[str, str]] = []
    seen_files: set[str] = set()

    def add_file(relative_path: str, component: str) -> None:
        if relative_path in seen_files or len(discovered) >= max_files:
            return
        seen_files.add(relative_path)
        discovered.append((relative_path, component or _component_from_log_path(relative_path)))

    def is_log_file(name: str) -> bool:
        return bool(re.search(r"\.(?:log|txt|out)$", name.lower()))

    # Debug logs: only descend through <subsystem> and an optional <module>
    # directory.  Do not fall back to scanning full_logs/log/ recursively.
    try:
        debug_entries = list_directory(_safe_child_url(base, debug_root))
    except Exception:
        debug_entries = []
    for subsystem_entry in debug_entries:
        if len(discovered) >= max_files:
            break
        if not subsystem_entry.endswith('/'):
            continue
        subsystem = subsystem_entry.rstrip('/')
        if not _is_case_catalog_name(subsystem):
            continue
        subsystem_root = f"{debug_root}{subsystem}/"
        try:
            module_entries = list_directory(_safe_child_url(base, subsystem_root))
        except Exception:
            continue
        for module_entry in module_entries:
            if len(discovered) >= max_files:
                break
            if is_log_file(module_entry):
                relative = f"{subsystem_root}{module_entry}"
                add_file(relative, _component_from_log_path(module_entry))
                continue
            if not module_entry.endswith('/'):
                continue
            module = module_entry.rstrip('/')
            if not _is_case_catalog_name(module):
                continue
            module_root = f"{subsystem_root}{module}/"
            try:
                files = list_directory(_safe_child_url(base, module_root))
            except Exception:
                continue
            for filename in files:
                if filename.endswith('/') or not is_log_file(filename):
                    continue
                add_file(f"{module_root}{filename}", module)
                if len(discovered) >= max_files:
                    break

    # Execution logs: elog/<IP>/<subsystem>/<file>.  IP folders are routing
    # selectors only.  They are never subsystem/module candidates.
    for elog_root in elog_roots:
        try:
            elog_entries = list_directory(_safe_child_url(base, elog_root))
        except Exception:
            elog_entries = []
        for ip_entry in elog_entries:
            if len(discovered) >= max_files:
                break
            if not ip_entry.endswith('/'):
                continue
            ip_text = ip_entry.rstrip('/')
            if not _is_ip_path_segment(ip_text):
                continue
            parsed_ip = ipaddress.ip_address(ip_text)
            if parsed_ip.version != 4:
                continue
            ip_root = f"{elog_root}{ip_text}/"
            try:
                subsystem_entries = list_directory(_safe_child_url(base, ip_root))
            except Exception:
                continue
            for subsystem_entry in subsystem_entries:
                if len(discovered) >= max_files:
                    break
                if not subsystem_entry.endswith('/'):
                    continue
                subsystem = subsystem_entry.rstrip('/')
                if not _is_case_catalog_name(subsystem):
                    continue
                subsystem_root = f"{ip_root}{subsystem}/"
                try:
                    files = list_directory(_safe_child_url(base, subsystem_root))
                except Exception:
                    continue
                for filename in files:
                    if filename.endswith('/') or not is_log_file(filename):
                        continue
                    component = _executor_component_from_log_name(filename)
                    if not _is_case_catalog_name(component):
                        continue
                    add_file(f"{subsystem_root}{filename}", component)
                    if len(discovered) >= max_files:
                        break

    return tuple(discovered)


def _executor_component_from_log_name(name: str) -> str:
    """Extract ``<module>`` from an execution-log filename.

    Production execution logs use names such as ``rspme_cp_xx.log``.  When a
    file does not follow that convention we retain the filename-derived value
    instead of inventing a module mapping.
    """
    component = _component_from_log_path(name)
    match = re.match(r"^(?P<module>.+?)_cp(?:_|$)", component, re.IGNORECASE)
    if not match:
        return component
    module = match.group('module').strip('_- ')
    return module or component


def _case_log_hierarchy(relative_path: str, fallback_component: str = "") -> tuple[str, str]:
    """Return only real ``(subsystem, module)`` values from fixed ATLog paths.

    For execution logs, the IP directory is a routing selector only:
    ``elog/<IP>/<subsystem>/<file>`` -> ``(<subsystem>, <module>)``.
    It can never be emitted as a subsystem or module.
    """
    parts = [part for part in relative_path.strip("/").split("/") if part]
    lowered = [part.lower() for part in parts]

    if "elog" in lowered:
        index = lowered.index("elog")
        tail = parts[index + 1:]
        if len(tail) < 3 or not _is_ip_path_segment(tail[0]):
            return "", ""
        subsystem = tail[1]
        module = fallback_component or _executor_component_from_log_name(tail[-1])
        if not _is_case_catalog_name(subsystem) or not _is_case_catalog_name(module):
            return "", ""
        return subsystem, module

    if "debug" in lowered:
        index = lowered.index("debug")
        tail = parts[index + 1:]
        if not tail:
            return "", ""
        subsystem = tail[0]
        if not _is_case_catalog_name(subsystem):
            return "", ""
        if len(tail) >= 3:
            module = tail[1]
        elif len(tail) >= 2:
            module = fallback_component or _component_from_log_path(tail[1])
        else:
            module = fallback_component or subsystem
        if not _is_case_catalog_name(module):
            return "", ""
        return subsystem, module

    return "", ""


def _case_log_subsystem(relative_path: str) -> str:
    return _case_log_hierarchy(relative_path)[0]


def _case_log_kind(relative_path: str) -> str:
    lowered = [part.lower() for part in relative_path.strip("/").split("/") if part]
    return "executor" if "elog" in lowered else "debug"


def discover_case_log_files(base_url: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for path, fallback_component in _discover_case_log_files_cached(normalize_base_url(base_url)):
        subsystem, module = _case_log_hierarchy(path, fallback_component)
        if not _is_case_catalog_name(subsystem) or not _is_case_catalog_name(module):
            continue
        result.append({
            "relative_path": path,
            "component": module or fallback_component,
            "subsystem": subsystem,
            "module": module or fallback_component,
            "kind": _case_log_kind(path),
        })
    return result


@lru_cache(maxsize=128)
def _discover_case_log_catalog_cached(base_url: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Discover hierarchy names without descending into historical log files.

    The component picker only needs ``subsystem -> module``.  Do not build that
    picker by recursively enumerating every physical log.  Module directories
    and direct ``<module>.log``/rotated filenames at the subsystem level are
    sufficient and make catalog loading independent from search execution.
    """
    base = normalize_base_url(base_url)
    debug_root = "full_logs/log/debug/"
    try:
        subsystem_entries = list_directory(_safe_child_url(base, debug_root))
    except Exception:
        return tuple()
    catalog: dict[str, set[str]] = {}
    for subsystem_entry in subsystem_entries:
        if not subsystem_entry.endswith("/"):
            continue
        subsystem = subsystem_entry.rstrip("/")
        if not _is_case_catalog_name(subsystem):
            continue
        subsystem_root = f"{debug_root}{subsystem}/"
        try:
            entries = list_directory(_safe_child_url(base, subsystem_root))
        except Exception:
            continue
        modules: set[str] = set()
        for entry in entries:
            if entry.endswith("/"):
                module = entry.rstrip("/")
            elif re.search(r"\.(?:log|txt|out)$", entry, re.I):
                module = _component_from_log_path(entry)
            else:
                continue
            if _is_case_catalog_name(module):
                modules.add(module)
        if modules:
            catalog[subsystem] = modules
    return tuple(
        (subsystem, tuple(sorted(modules, key=str.lower)))
        for subsystem, modules in sorted(catalog.items(), key=lambda item: item[0].lower())
    )


def discover_case_log_catalog(base_url: str) -> list[dict[str, Any]]:
    return [
        {"subsystem": subsystem, "modules": list(modules)}
        for subsystem, modules in _discover_case_log_catalog_cached(normalize_base_url(base_url))
    ]


def _select_case_log_names_for_window(
    names: Iterable[str],
    *,
    start: datetime | None,
    end: datetime | None,
    module: str = "",
    subsystem: str = "",
    source_category: str = "debug",
) -> list[str]:
    # Physical-file window selection is owned by the shared log-positioning
    # layer.  The logical module hint groups executor ``*_cp_*`` files exactly
    # like environment log positioning instead of treating each filename as an
    # independent current stream.
    return select_url_log_names_for_window(
        names, start, end, fm_hint=module, subsystem=subsystem, source_category=source_category
    )

def discover_case_log_files_for_query(
    base_url: str,
    *,
    targets: Iterable[tuple[str, str]],
    source_categories: Iterable[str],
    start: datetime | None,
    end: datetime | None,
    operation_id: str = "",
) -> list[dict[str, str]]:
    """Resolve only selected ATLog target directories for one search request.

    This is intentionally different from ``discover_case_log_files`` (catalog
    discovery).  Search must never enumerate the complete case tree first.  It
    enters only selected subsystem/module directories, performs filename-time
    pruning, and returns the minimal URL artifacts needed by the shared reader.
    """
    base = normalize_base_url(base_url)
    requested_sources = {str(item).strip().lower() for item in source_categories if str(item).strip()}
    target_pairs = {
        (str(subsystem).strip(), str(module).strip())
        for subsystem, module in targets
        if _is_case_catalog_name(str(subsystem)) and _is_case_catalog_name(str(module))
    }
    result: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    def cancelled() -> None:
        if operation_id:
            _log_search_progress().raise_if_cancelled(operation_id)

    def add(relative_path: str, subsystem: str, module: str, kind: str) -> None:
        if relative_path in seen_paths:
            return
        seen_paths.add(relative_path)
        result.append({
            "relative_path": relative_path,
            "component": module,
            "subsystem": subsystem,
            "module": module,
            "kind": kind,
        })

    debug_root = "full_logs/log/debug/"
    if "debug" in requested_sources:
        for subsystem, module in sorted(target_pairs, key=lambda pair: (pair[0].lower(), pair[1].lower())):
            cancelled()
            subsystem_root = f"{debug_root}{subsystem}/"
            if operation_id:
                _log_search_progress().directory_started(
                    operation_id,
                    directory=subsystem_root,
                    subsystem=subsystem,
                    modules=[module],
                    action=f"正在定位 {subsystem}/{module} 时间窗口文件",
                )
            try:
                entries = list_directory(_safe_child_url(base, subsystem_root))
            except Exception:
                entries = []
            cancelled()

            # Direct layout: debug/<subsystem>/<module>.log or rotated variants.
            direct_names = [
                name for name in entries
                if not name.endswith("/")
                and re.search(r"\.(?:log|txt|out)$", name, re.I)
                and _component_from_log_path(name).upper() == module.upper()
            ]
            selected_direct = _select_case_log_names_for_window(direct_names, start=start, end=end, module=module, subsystem=subsystem, source_category="debug")
            for name in selected_direct:
                add(f"{subsystem_root}{name}", subsystem, module, "debug")

            # Nested layout: debug/<subsystem>/<module>/<rotated/current files>.
            module_dir = next(
                (name.rstrip("/") for name in entries if name.endswith("/") and name.rstrip("/").upper() == module.upper()),
                "",
            )
            selected_nested: list[str] = []
            if module_dir:
                module_root = f"{subsystem_root}{module_dir}/"
                try:
                    nested_entries = list_directory(_safe_child_url(base, module_root))
                except Exception:
                    nested_entries = []
                cancelled()
                selected_nested = _select_case_log_names_for_window(nested_entries, start=start, end=end, module=module, subsystem=subsystem, source_category="debug")
                for name in selected_nested:
                    add(f"{module_root}{name}", subsystem, module, "debug")

            if operation_id:
                _log_search_progress().candidates_found(
                    operation_id,
                    directory=subsystem_root,
                    subsystem=subsystem,
                    discovered=len(direct_names) + (len(nested_entries) if module_dir else 0),
                    selected=len(selected_direct) + len(selected_nested),
                    action="已按时间范围筛选调试日志文件",
                )

    if "executor" in requested_sources:
        elog_roots = ("full_logs/log/debug/elog/", "full_logs/log/elog/")
        for elog_root in elog_roots:
            cancelled()
            try:
                ip_entries = list_directory(_safe_child_url(base, elog_root))
            except Exception:
                continue
            ip_names = [name.rstrip("/") for name in ip_entries if name.endswith("/") and _is_ip_path_segment(name.rstrip("/"))]
            for ip_text in ip_names:
                for subsystem, module in sorted(target_pairs, key=lambda pair: (pair[0].lower(), pair[1].lower())):
                    cancelled()
                    subsystem_root = f"{elog_root}{ip_text}/{subsystem}/"
                    if operation_id:
                        _log_search_progress().directory_started(
                            operation_id,
                            directory=subsystem_root,
                            subsystem=subsystem,
                            modules=[module],
                            action=f"正在定位执行器 {ip_text}/{subsystem}/{module} 时间窗口文件",
                        )
                    try:
                        entries = list_directory(_safe_child_url(base, subsystem_root))
                    except Exception:
                        continue
                    cancelled()
                    module_names = [
                        name for name in entries
                        if not name.endswith("/")
                        and re.search(r"\.(?:log|txt|out)$", name, re.I)
                        and _executor_component_from_log_name(name).upper() == module.upper()
                    ]
                    selected_names = _select_case_log_names_for_window(module_names, start=start, end=end, module=module, subsystem=subsystem, source_category="executor")
                    for name in selected_names:
                        add(f"{subsystem_root}{name}", subsystem, module, "executor")
                    if operation_id:
                        _log_search_progress().candidates_found(
                            operation_id,
                            directory=subsystem_root,
                            subsystem=subsystem,
                            discovered=len(module_names),
                            selected=len(selected_names),
                            action="已按时间范围筛选执行器日志文件",
                        )

    return result


def query_case_logs(
    base_url: str,
    *,
    start_time: str = "",
    end_time: str = "",
    components: Iterable[str] | None = None,
    targets: Iterable[dict[str, Any]] | None = None,
    source_categories: Iterable[str] | None = None,
    levels: Iterable[str] | None = None,
    keyword: str = "",
    anomaly_rules: Iterable[dict[str, Any]] | None = None,
    include_event: bool = True,
    max_lines: int = 12000,
    operation_id: str = "",
) -> dict[str, Any]:
    """Query ATLog through the shared log-positioning engine.

    This function is only the *case URL adapter*: it resolves the ATLog directory
    contract into URL artifacts, then delegates time-window file reads to
    ``apps.logsources.services.unified_search``. It deliberately does not own a
    second large-file reader, byte-range algorithm, or progress store.
    """
    base = normalize_base_url(base_url)
    start = _parse_timestamp(start_time)
    end = _parse_timestamp(end_time)
    if start and end and start > end:
        raise AtLogError("日志开始时间不能晚于结束时间。")

    selected_components = {str(item).strip().upper() for item in (components or []) if str(item).strip()}
    selected_target_paths = {
        (str(item.get("subsystem") or "").strip(), str(item.get("module") or "").strip())
        for item in (targets or [])
        if isinstance(item, dict) and (str(item.get("subsystem") or "").strip() or str(item.get("module") or "").strip())
    }
    selected_targets = {(subsystem.upper(), module.upper()) for subsystem, module in selected_target_paths}
    source_filter_active = source_categories is not None
    requested_sources = {str(item).strip().lower() for item in (source_categories or []) if str(item).strip()}
    include_event = bool(include_event and (not source_filter_active or "event" in requested_sources))
    if selected_targets and not source_filter_active:
        requested_sources.update({"debug", "executor"})
    include_full_logs = not source_filter_active or bool({"debug", "executor"} & requested_sources)

    selected_levels = {str(item).strip().upper() for item in (levels or []) if str(item).strip()}
    keyword_normalized = str(keyword or "").strip().lower()
    active_anomaly_rules = normalize_anomaly_rules(list(anomaly_rules or []))
    compiled_anomaly_rules = compile_anomaly_rules(active_anomaly_rules)
    max_lines = max(100, min(int(max_lines or 12000), 50000))

    if operation_id:
        _log_search_progress().patch(
            operation_id,
            stage="indexing",
            percent=12,
            current_action="正在解析 ATLog 用例日志路径",
            message="正在解析 ATLog 用例日志路径",
            current_root=base,
            current_directory=base,
        )

    # Search path resolution is target-scoped.  Never enumerate the complete
    # full_logs tree before applying the user's subsystem/module/time filters.
    # Catalog discovery is a separate lightweight concern handled by
    # ``discover_case_log_catalog`` / ``analyze_case``.
    discovered: list[dict[str, str]] = []
    try:
        if include_full_logs and selected_targets:
            effective_sources = requested_sources or {"debug", "executor"}
            discovered = discover_case_log_files_for_query(
                base,
                targets=selected_target_paths,
                source_categories=effective_sources,
                start=start,
                end=end,
                operation_id=operation_id,
            )
        elif include_full_logs and selected_components:
            # Legacy component-only callers do not provide enough hierarchy
            # information for direct URL addressing. Keep the bounded cached
            # fallback for compatibility; interactive UI and AI target queries
            # use explicit subsystem/module targets and therefore never hit it.
            discovered = discover_case_log_files(base)
    except (FileNotFoundError, AtLogError, OSError) as exc:
        logger.warning("atlog.case_logs.discover_failed base=%s error=%s", base, exc)
        discovered = []

    components_available: set[str] = {
        str(item.get("component") or "").strip()
        for item in discovered
        if _is_case_catalog_name(str(item.get("component") or ""))
    }
    # Return the stable picker catalog independently from physical search
    # candidates.  This prevents a narrow search from shrinking the selector and
    # prevents an event-only search from crawling the runtime tree.
    log_catalog = discover_case_log_catalog(base)

    matches: list[dict[str, str]] = []
    if include_full_logs and (selected_components or selected_targets):
        for item in discovered:
            kind = str(item.get("kind") or "debug").lower()
            if source_filter_active and kind not in requested_sources:
                continue
            component = str(item.get("component") or "").strip()
            subsystem = str(item.get("subsystem") or "").strip()
            module = str(item.get("module") or component).strip()
            relative = str(item.get("relative_path") or "")
            if not _is_case_catalog_name(subsystem) or not _is_case_catalog_name(module):
                continue
            target_match = any(
                (not wanted_subsystem or wanted_subsystem == subsystem.upper())
                and (not wanted_module or wanted_module == module.upper())
                for wanted_subsystem, wanted_module in selected_targets
            ) if selected_targets else False
            haystack = f"{component} {subsystem} {module}".upper()
            component_match = any(token == component.upper() or token == module.upper() or token in haystack for token in selected_components)
            if target_match or component_match:
                matches.append(item)

    # Reuse the log-positioning file index for URL-backed current logs. Filename
    # rotations were already pruned above; boundary-less files still need the
    # same real head/tail timestamp overlap check used by environment searches.
    artifact_by_path: dict[str, UrlLogArtifact] = {}
    for item in matches:
        relative = str(item.get("relative_path") or "")
        component_hint = str(item.get("component") or item.get("module") or "")
        artifact_by_path[relative] = UrlLogArtifact(
            url=_safe_child_url(base, relative),
            source_path=relative,
            source_category=str(item.get("kind") or "debug"),
            subsystem=str(item.get("subsystem") or ""),
            fm=str(item.get("module") or component_hint),
        )
    if artifact_by_path and start is not None and end is not None:
        indexed_artifacts = filter_url_artifacts_for_window(
            artifact_by_path.values(), start, end, operation_id=operation_id or "-"
        )
        selected_paths = {artifact.source_path for artifact in indexed_artifacts}
        matches = [item for item in matches if str(item.get("relative_path") or "") in selected_paths]
        artifact_by_path = {artifact.source_path: artifact for artifact in indexed_artifacts}

    event_artifact_count = 1 if include_event else 0
    if operation_id:
        _log_search_progress().patch(
            operation_id,
            stage="matching",
            percent=45,
            discovered_files=len(discovered),
            checked_files=len(discovered),
            selected_files=len(matches) + event_artifact_count,
            message=f"时间索引筛选完成，目标日志文件 {len(matches) + event_artifact_count} 个",
        )
        _log_search_progress().plan_ready(operation_id, artifact_total=len(matches) + event_artifact_count)

    rows: list[dict[str, Any]] = []
    event_raw: list[str] = []
    sources: set[str] = set()
    truncated = False
    event_result: dict[str, Any] = {"components": [], "rows": [], "truncated": False}
    event_query = {
        "requested": bool(include_event),
        "url": _case_root_file_url(base, "event.log"),
        "found": False,
        "row_count": 0,
        "error": "",
    }

    if include_event:
        try:
            event_result = query_event_log(
                base,
                start_time=start_time,
                end_time=end_time,
                modules=selected_components,
                levels=selected_levels,
                keyword=keyword,
                max_lines=max_lines,
                operation_id=operation_id,
            )
            event_query["found"] = True
            event_query["row_count"] = len(event_result.get("rows") or [])
            components_available.update(str(item) for item in event_result.get("components") or [])
            truncated = truncated or bool(event_result.get("truncated"))
            for row in event_result.get("rows") or []:
                raw_line = str(row.get("raw") or "")
                matched_rules = matching_compiled_anomaly_rules(raw_line, compiled_anomaly_rules) if active_anomaly_rules else []
                if active_anomaly_rules and not matched_rules:
                    continue
                enriched = dict(row)
                enriched["source_kind"] = "event"
                enriched["source_path"] = "event.log"
                enriched["source_url"] = str(event_result.get("event_url") or event_query["url"])
                if matched_rules:
                    enriched["matched_anomaly_rules"] = [
                        str(rule.get("keyword") or "") for rule in matched_rules if str(rule.get("keyword") or "").strip()
                    ]
                rows.append(enriched)
                event_raw.append(raw_line)
            if event_result.get("rows"):
                sources.add("event.log")
        except (FileNotFoundError, AtLogError, UnifiedLogSearchError) as exc:
            event_query["error"] = str(exc)
            event_result = {"components": [], "rows": [], "truncated": False}

    # The case adapter now hands every direct debug/elog file to the exact same
    # URL time-window reader. No tail-only fallback and no ATLog-specific byte
    # search remain here.
    for item in matches:
        if len(rows) >= max_lines:
            truncated = True
            break
        relative = str(item.get("relative_path") or "")
        component_hint = str(item.get("component") or item.get("module") or "")
        artifact = artifact_by_path.get(relative) or UrlLogArtifact(
            url=_safe_child_url(base, relative),
            source_path=relative,
            source_category=str(item.get("kind") or "debug"),
            subsystem=str(item.get("subsystem") or ""),
            fm=str(item.get("module") or component_hint),
        )
        try:
            window = read_url_log_window(artifact, start, end, operation_id=operation_id or "-")
        except (FileNotFoundError, UnifiedLogSearchError) as exc:
            logger.warning("atlog.case_logs.read_failed path=%s error=%s", relative, exc)
            continue
        truncated = truncated or bool(window.truncated)
        source_added = False
        for index, line in enumerate(window.text.splitlines(), start=1):
            parsed = _generic_log_row(line, index, component_hint=component_hint, source_path=relative)
            # ``read_url_log_window`` already performs exact time filtering. Keep
            # this defensive parse for malformed/nonstandard lines only.
            ts = _parse_timestamp(parsed["time"])
            if start and ts and ts < start:
                continue
            if end and ts and ts > end:
                continue
            level = str(parsed.get("level") or "").upper()
            if selected_levels and level and level not in selected_levels:
                continue
            if keyword_normalized and keyword_normalized not in line.lower():
                continue
            matched_rules = matching_compiled_anomaly_rules(line, compiled_anomaly_rules) if active_anomaly_rules else []
            if active_anomaly_rules and not matched_rules:
                continue
            if matched_rules:
                parsed["matched_anomaly_rules"] = [
                    str(rule.get("keyword") or "") for rule in matched_rules if str(rule.get("keyword") or "").strip()
                ]
                parsed["anomaly_rules"] = [
                    {
                        "id": str(rule.get("id") or ""),
                        "keyword": str(rule.get("keyword") or ""),
                        "case_sensitive": bool(rule.get("case_sensitive")),
                        "whole_word": bool(rule.get("whole_word")),
                    }
                    for rule in matched_rules[:12]
                ]
            rows.append(parsed)
            source_added = True
            if len(rows) >= max_lines:
                truncated = True
                break
        if source_added:
            sources.add(relative)

    if operation_id:
        _log_search_progress().patch(
            operation_id,
            stage="finalizing",
            percent=96,
            current_action="正在整理用例日志结果",
            message="正在整理用例日志结果",
        )

    def row_sort_key(row: dict[str, Any]) -> tuple[int, datetime, str, int]:
        ts = _parse_timestamp(str(row.get("time") or ""))
        if ts is None:
            return (1, datetime.max, str(row.get("source_path") or ""), int(row.get("line_number") or 0))
        return (0, ts, str(row.get("source_path") or ""), int(row.get("line_number") or 0))

    rows.sort(key=row_sort_key)
    if len(rows) > max_lines:
        rows = rows[:max_lines]
        truncated = True
    ai_payload = compact_log_rows_for_ai(rows, max_chars=12000, max_nodes=40, max_groups=24)
    return {
        "base_url": base,
        "start_time": start_time,
        "end_time": end_time,
        "count": len(rows),
        "truncated": truncated,
        "components": sorted(components_available, key=str.lower),
        "log_catalog": log_catalog,
        "levels": sorted({str(row.get("level") or "") for row in rows if row.get("level")}),
        "sources": sorted(sources, key=str.lower),
        "rows": rows,
        "event_raw_text": "\n".join(line for line in event_raw if line),
        "event_query": event_query,
        "source_roots": {
            "event": _case_root_file_url(base, "event.log"),
            "debug": _safe_child_url(base, "full_logs/log/debug/"),
            "executor": _safe_child_url(base, "full_logs/log/debug/elog/"),
        },
        "search_engine": "logsources.unified_search",
        "search_origin": "atlog_url",
        "anomaly_rule_count": len(active_anomaly_rules),
        "matched_target_count": len(selected_targets),
        "matched_file_count": len(matches),
        "matched_files": [str(item.get("relative_path") or "") for item in matches],
        **ai_payload,
    }

def query_event_log(
    base_url: str,
    *,
    start_time: str = "",
    end_time: str = "",
    modules: Iterable[str] | None = None,
    levels: Iterable[str] | None = None,
    keyword: str = "",
    max_lines: int = 8000,
    operation_id: str = "",
) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    event_url = _case_root_file_url(base, "event.log")
    start = _parse_timestamp(start_time)
    end = _parse_timestamp(end_time)
    if start and end and start > end:
        raise AtLogError("event.log 开始时间不能晚于结束时间。")

    artifact = UrlLogArtifact(
        url=event_url,
        source_path="event.log",
        source_category="event",
        subsystem="",
        fm="event",
    )
    try:
        window = read_url_log_window(artifact, start, end, operation_id=operation_id or "-")
    except UnifiedLogSearchError as exc:
        raise AtLogError(str(exc)) from exc
    text = window.text

    selected_modules = {str(item).strip().upper() for item in (modules or []) if str(item).strip()}
    selected_levels = {str(item).strip().upper() for item in (levels or []) if str(item).strip()}
    keyword_normalized = str(keyword or "").strip().lower()
    rows: list[dict[str, Any]] = []
    components: set[str] = set()
    available_levels: set[str] = set()
    display_codes: set[str] = set()
    max_lines = max(100, min(int(max_lines or 8000), 50000))
    for index, line in enumerate(text.splitlines(), start=1):
        row = _event_row(line, index)
        ts = _parse_timestamp(row["time"])
        if start and ts and ts < start:
            continue
        if end and ts and ts > end:
            continue
        component = str(row["component"] or "").upper()
        level = str(row["level"] or "").upper()
        if row["component"]:
            components.add(str(row["component"]))
        if level:
            available_levels.add(level)
        if str(row.get("display_code") or "").strip():
            display_codes.add(str(row.get("display_code") or "").strip())
        if selected_modules and component not in selected_modules:
            continue
        if selected_levels and level not in selected_levels:
            continue
        if keyword_normalized and keyword_normalized not in line.lower():
            continue
        rows.append(row)
        if len(rows) >= max_lines:
            break
    truncated = bool(window.truncated or len(rows) >= max_lines)
    ai_payload = compact_log_rows_for_ai(rows, max_chars=8000, max_nodes=28, max_groups=18)
    return {
        "base_url": base,
        "url": event_url,
        "event_url": event_url,
        "source_location": "case_root",
        "start_time": start_time,
        "end_time": end_time,
        "count": len(rows),
        "truncated": truncated,
        "read_mode": window.mode,
        "start_offset": window.start_offset,
        "scanned_bytes": window.scanned_bytes,
        "components": sorted(components, key=str.lower),
        "levels": sorted(available_levels),
        "display_codes": sorted(display_codes, key=str.lower),
        "rows": rows,
        "raw_text": "\n".join(row["raw"] for row in rows),
        **ai_payload,
    }


def read_case_file(base_url: str, relative_path: str, max_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    url = _safe_child_url(base, relative_path)
    safe_limit = max(64 * 1024, min(int(max_bytes or 2 * 1024 * 1024), 8 * 1024 * 1024))
    text = fetch_text(url, safe_limit)
    display_text = text
    if relative_path.lower().endswith(".html"):
        filename = relative_path.rstrip("/").split("/")[-1].lower()
        current_case_id = case_id_from_url(base)
        if filename.startswith("test_"):
            display_text = parse_pytest_html_excerpt(text, current_case_id)
        elif filename == "failures_report.html":
            display_text = parse_case_html_excerpt(text, current_case_id)
        if not display_text:
            parser = VisibleHtmlTextParser()
            parser.feed(text)
            display_text = parser.text()
    return {"base_url": base, "relative_path": relative_path, "url": url, "content": display_text, "size": len(text.encode("utf-8", errors="ignore"))}


def analyze_case_tool(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return analyze_case(str(payload.get("url") or payload.get("base_url") or ""))
    except AtLogError as exc:
        from apps.tooling.services import ToolInputError
        raise ToolInputError(str(exc)) from exc


def query_case_logs_tool(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return query_case_logs(
            str(payload.get("url") or payload.get("base_url") or ""),
            start_time=str(payload.get("start_time") or ""),
            end_time=str(payload.get("end_time") or ""),
            components=payload.get("components") or [],
            targets=payload.get("targets") or [],
            levels=payload.get("levels") or [],
            keyword=str(payload.get("keyword") or ""),
            anomaly_rules=payload.get("anomaly_rules") or [],
            include_event=bool(payload.get("include_event", True)),
            max_lines=int(payload.get("max_lines") or 5000),
        )
    except (AtLogError, TypeError, ValueError) as exc:
        from apps.tooling.services import ToolInputError
        raise ToolInputError(str(exc)) from exc


def query_event_log_tool(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return query_event_log(
            str(payload.get("url") or payload.get("base_url") or ""),
            start_time=str(payload.get("start_time") or ""),
            end_time=str(payload.get("end_time") or ""),
            modules=payload.get("modules") or [],
            levels=payload.get("levels") or [],
            keyword=str(payload.get("keyword") or ""),
            max_lines=int(payload.get("max_lines") or 8000),
        )
    except (AtLogError, TypeError, ValueError) as exc:
        from apps.tooling.services import ToolInputError
        raise ToolInputError(str(exc)) from exc
