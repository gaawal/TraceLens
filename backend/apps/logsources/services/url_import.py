from __future__ import annotations

import re
import tarfile
from pathlib import PurePosixPath
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from apps.logsources.services.archive_selector import parse_archive_timestamp, parse_log_name

USER_AGENT = "TraceLens-UrlImport/1.0"
URL_TIMEOUT_SECONDS = 20
MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024 * 1024  # 1 GiB safety ceiling for one selected member.


class UrlImportError(ValueError):
    pass


def validate_log_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        raise UrlImportError("日志链接不能为空。")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UrlImportError("仅支持 http / https 日志链接。")
    if parsed.username or parsed.password:
        raise UrlImportError("日志链接中不能携带用户名或密码。")
    return url


def filename_from_url(url: str) -> str:
    parsed = urlparse(validate_log_url(url))
    filename = unquote(PurePosixPath(parsed.path).name).strip()
    if not filename:
        raise UrlImportError("链接中没有可识别的日志文件名。")
    return filename


def is_tar_archive(filename: str) -> bool:
    lowered = filename.lower()
    return lowered.endswith(".tar.gz") or lowered.endswith(".tgz")


def derive_fm_from_filename(filename: str) -> str:
    basename = PurePosixPath(filename).name.strip()
    lowered = basename.lower()
    if lowered.endswith(".tar.gz"):
        stem = basename[:-7]
        match = re.match(r"^(?P<fm>.+)_(?P<stamp>\d[\dT_:\-.]*)$", stem)
        if match and parse_archive_timestamp(match.group("stamp")) is not None:
            return match.group("fm").strip() or stem
        return stem.strip() or basename
    if lowered.endswith(".tgz"):
        stem = basename[:-4]
        match = re.match(r"^(?P<fm>.+)_(?P<stamp>\d[\dT_:\-.]*)$", stem)
        if match and parse_archive_timestamp(match.group("stamp")) is not None:
            return match.group("fm").strip() or stem
        return stem.strip() or basename
    parsed = parse_log_name(basename)
    if parsed:
        return parsed[0].strip() or basename
    suffix = PurePosixPath(basename).suffix
    return (basename[: -len(suffix)] if suffix else basename).strip() or basename


def _request(url: str):
    request = Request(validate_log_url(url), headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        return urlopen(request, timeout=URL_TIMEOUT_SECONDS)
    except HTTPError as exc:
        raise UrlImportError(f"读取链接失败：HTTP {exc.code}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise UrlImportError(f"读取链接失败：{reason}") from exc


def _member_fm(name: str) -> str:
    basename = PurePosixPath(name).name
    parsed = parse_log_name(basename)
    if parsed:
        return parsed[0]
    suffix = PurePosixPath(basename).suffix
    return basename[: -len(suffix)] if suffix else basename


def inspect_url(url: str) -> dict:
    url = validate_log_url(url)
    filename = filename_from_url(url)
    fm = derive_fm_from_filename(filename)
    if not is_tar_archive(filename):
        return {
            "url": url,
            "filename": filename,
            "fm": fm,
            "kind": "log",
            "members": [],
        }

    members: list[dict] = []
    with _request(url) as response:
        try:
            mode = "r|gz"
            with tarfile.open(fileobj=response, mode=mode) as archive:
                for item in archive:
                    if len(members) >= MAX_ARCHIVE_MEMBERS:
                        break
                    if not item.isfile():
                        continue
                    basename = PurePosixPath(item.name).name
                    lowered = basename.lower()
                    if not lowered.endswith((".log", ".txt", ".out")):
                        continue
                    members.append({
                        "name": item.name,
                        "filename": basename,
                        "fm": _member_fm(item.name),
                        "size": int(item.size or 0),
                    })
        except (tarfile.TarError, OSError) as exc:
            raise UrlImportError(f"无法读取压缩包目录：{exc}") from exc

    if not members:
        raise UrlImportError("压缩包中没有可解析的日志文件。")
    return {
        "url": url,
        "filename": filename,
        "fm": fm,
        "kind": "archive",
        "members": members,
    }


def inspect_urls(urls: list[str]) -> list[dict]:
    result: list[dict] = []
    for raw in urls:
        url = str(raw or "").strip()
        if not url:
            continue
        try:
            result.append({"status": "success", **inspect_url(url)})
        except UrlImportError as exc:
            filename = ""
            try:
                filename = filename_from_url(url)
            except UrlImportError:
                pass
            result.append({
                "status": "error",
                "url": url,
                "filename": filename,
                "fm": derive_fm_from_filename(filename) if filename else "",
                "kind": "archive" if filename and is_tar_archive(filename) else "log",
                "members": [],
                "message": str(exc),
            })
    return result


def stream_log_url(url: str, member: str = "") -> Iterator[bytes]:
    url = validate_log_url(url)
    filename = filename_from_url(url)
    if not member:
        if is_tar_archive(filename):
            raise UrlImportError("压缩包必须先选择一个内部日志文件。")
        with _request(url) as response:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        return

    if not is_tar_archive(filename):
        raise UrlImportError("普通日志链接不能指定压缩包成员。")

    requested_member = str(member).strip()
    if not requested_member:
        raise UrlImportError("请选择压缩包内日志文件。")

    with _request(url) as response:
        try:
            with tarfile.open(fileobj=response, mode="r|gz") as archive:
                for item in archive:
                    if not item.isfile() or item.name != requested_member:
                        continue
                    if int(item.size or 0) > MAX_ARCHIVE_MEMBER_BYTES:
                        raise UrlImportError("选择的压缩包日志超过 1 GiB，暂不支持在线导入。")
                    extracted = archive.extractfile(item)
                    if extracted is None:
                        raise UrlImportError("无法读取选择的压缩包日志。")
                    while True:
                        chunk = extracted.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                    return
        except UrlImportError:
            raise
        except (tarfile.TarError, OSError) as exc:
            raise UrlImportError(f"读取压缩包日志失败：{exc}") from exc
    raise UrlImportError("压缩包中未找到选择的日志文件。")
