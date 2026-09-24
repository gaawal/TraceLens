"""假远端 shell：把后端发来的远端命令映射到本地模拟文件系统上执行。

大多数命令直接交给真实系统工具执行（``tar`` / ``awk`` / ``cat`` / ``tail``），
只有 ``find`` 需要自己实现 —— 因为 macOS 的 BSD find 不支持后端依赖的
``-printf '%i\\t%s\\t%T@\\t%p\\0'``（inode/size/mtime + NUL 分隔记录）。

命令里的远端绝对路径（``/log/...`` ``/home/...`` ``/data/...``）在执行前会被
重写成 ``<本地模拟根>/log/...``；反过来，``find`` 输出的路径必须还原成远端
路径，因为后端会拿它去做 SFTP 打开。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_REMOTE_PREFIXES = ("/log/", "/home/", "/data/", "/opt/", "/var/")
_PREFIX_RE = re.compile(r"(?<![\w.\-/])(" + "|".join(re.escape(item) for item in _REMOTE_PREFIXES) + r")")


@dataclass
class CommandResult:
    stdout: bytes = b""
    stderr: bytes = b""
    status: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 0


@dataclass
class _FindSpec:
    paths: list[str] = field(default_factory=list)
    kind: str | None = None
    groups: list[list[tuple[str, str]]] = field(default_factory=lambda: [[]])
    output: str = "print"
    printf_format: str = ""
    quit_first: bool = False
    mindepth: int = 0
    maxdepth: int | None = None

    def matches_name(self, name: str) -> bool:
        if not self.groups:
            return True
        for group in self.groups:
            if not group:
                continue
            if all(_name_matches(name, mode, pattern) for mode, pattern in group):
                return True
        return False


def _name_matches(name: str, mode: str, pattern: str) -> bool:
    import fnmatch

    if mode == "insensitive":
        return fnmatch.fnmatchcase(name.lower(), pattern.lower())
    return fnmatch.fnmatchcase(name, pattern)


def _parse_find(args: list[str]) -> _FindSpec:
    spec = _FindSpec()
    index = 0
    while index < len(args) and not args[index].startswith("-"):
        spec.paths.append(args[index])
        index += 1
    if not spec.paths:
        spec.paths.append(".")

    while index < len(args):
        token = args[index]
        try:
            if token in ("(", ")"):
                index += 1
                continue
            if token == "-o":
                spec.groups.append([])
                index += 1
                continue
            if token == "-mindepth":
                spec.mindepth = int(args[index + 1])
                index += 2
                continue
            if token == "-maxdepth":
                spec.maxdepth = int(args[index + 1])
                index += 2
                continue
            if token == "-type":
                spec.kind = args[index + 1]
                index += 2
                continue
            if token in ("-name", "-iname"):
                spec.groups[-1].append(
                    ("insensitive" if token == "-iname" else "sensitive", args[index + 1])
                )
                index += 2
                continue
            if token == "-print":
                spec.output = "print"
                index += 1
                continue
            if token == "-printf":
                spec.output = "printf"
                spec.printf_format = args[index + 1]
                index += 2
                continue
            if token == "-quit":
                spec.quit_first = True
                index += 1
                continue
        except (IndexError, ValueError):
            index += 1
            continue
        index += 1
    return spec


def _apply_escapes(text: str) -> str:
    for source, target in (("\\t", "\t"), ("\\n", "\n"), ("\\r", "\r"), ("\\0", "\0"), ("\\\\", "\\")):
        text = text.replace(source, target)
    return text


def _printf(format_text: str, remote_path: str, stat_result: os.stat_result) -> str:
    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code == "i":
            return str(stat_result.st_ino)
        if code == "s":
            return str(stat_result.st_size)
        if code == "T@":
            return f"{stat_result.st_mtime:.10f}"
        if code == "p":
            return remote_path
        if code == "f":
            return remote_path.rsplit("/", 1)[-1]
        if code == "n":
            return str(stat_result.st_nlink)
        if code == "%":
            return "%"
        return ""

    text = re.sub(r"%(T@|.)", replace, format_text)
    return _apply_escapes(text)


class RemoteShell:
    """一台模拟机器的远端 shell。"""

    def __init__(self, spec, root: Path | None = None) -> None:
        from . import fleet

        self.spec = spec
        self.username = fleet.SIM_USERNAME
        self.root = Path(root) if root else fleet.local_root_of(spec)
        self.home = self.root / f"home/{self.username}"

    # ------------------------------------------------------------------ 路径

    def map_path(self, remote_path: str) -> Path:
        """远端路径 → 本地路径。"""
        text = str(remote_path or "").strip()
        if not text:
            return self.root
        if text.startswith("$HOME") or text.startswith('"$HOME"'):
            text = text.replace('"$HOME"', "").replace("$HOME", "")
            text = "/" + text.lstrip("/")
        if text.startswith("~"):
            text = f"/home/{self.username}" + text[1:]
        relative = text.lstrip("/")
        return self.root / relative

    def unmap_path(self, local_path: Path) -> str:
        """本地路径 → 远端路径（find 输出必须用远端路径）。"""
        try:
            relative = Path(local_path).resolve().relative_to(self.root.resolve())
        except ValueError:
            return str(local_path)
        return "/" + relative.as_posix()

    def rewrite_command(self, command: str) -> str:
        """把命令里的远端绝对路径替换成本地路径。"""
        replacement = str(self.root).rstrip("/") + "/"
        text = command.replace('"$HOME"/', replacement).replace("$HOME/", replacement)
        return _PREFIX_RE.sub(lambda match: replacement + match.group(1), text)

    # ------------------------------------------------------------------ 执行

    def execute(self, command: str) -> CommandResult:
        text = str(command or "").strip()
        if not text:
            return CommandResult()
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            tokens = []
        if tokens and tokens[0] == "find":
            return self._run_find(text)
        mapped = self.rewrite_command(text)
        try:
            proc = subprocess.run(
                mapped,
                shell=True,
                capture_output=True,
                cwd=str(self.root) if self.root.exists() else None,
            )
        except OSError as exc:
            return CommandResult(b"", f"shell: {exc}\n".encode("utf-8"), 127)
        return CommandResult(proc.stdout, proc.stderr, proc.returncode)

    def popen(self, command: str) -> subprocess.Popen:
        """用于 tail -F 这类长驻流式命令。"""
        mapped = self.rewrite_command(command)
        return subprocess.Popen(
            mapped,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.root) if self.root.exists() else None,
        )

    # ------------------------------------------------------------------ find

    def _run_find(self, command: str) -> CommandResult:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            return CommandResult(b"", f"find: {exc}\n".encode("utf-8"), 1)
        spec = _parse_find(tokens[1:])
        out = bytearray()
        err = bytearray()
        status = 0
        stop = False
        for remote_start in spec.paths:
            local_start = self.map_path(remote_start)
            if not local_start.exists():
                err += f"find: '{remote_start}': No such file or directory\n".encode("utf-8")
                status = 1
                continue
            base_depth = len(local_start.parts)
            for dirpath, dirnames, filenames in os.walk(local_start):
                current = Path(dirpath)
                depth = len(current.parts) - base_depth
                if spec.maxdepth is not None and depth >= spec.maxdepth:
                    dirnames[:] = []
                candidates: list[Path] = []
                if spec.kind in (None, "f"):
                    candidates += [current / name for name in filenames]
                if spec.kind in (None, "d"):
                    candidates += [current / name for name in dirnames]
                for candidate in candidates:
                    if spec.maxdepth is not None and depth + 1 > spec.maxdepth:
                        continue
                    if depth + 1 < spec.mindepth:
                        continue
                    if not candidate.is_file() and spec.kind == "f":
                        continue
                    if not spec.matches_name(candidate.name):
                        continue
                    remote_path = self.unmap_path(candidate)
                    if spec.output == "printf":
                        try:
                            stat_result = candidate.stat()
                        except OSError:
                            continue
                        out += _printf(spec.printf_format, remote_path, stat_result).encode("utf-8")
                    else:
                        out += (remote_path + "\n").encode("utf-8")
                    if spec.quit_first:
                        stop = True
                        break
                if stop:
                    break
            if stop:
                break
        return CommandResult(bytes(out), bytes(err), status)
