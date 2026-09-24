from __future__ import annotations

from typing import Iterator, Protocol


class SeekableRemoteFile(Protocol):
    def seek(self, offset: int) -> None: ...
    def read(self, size: int = -1) -> bytes: ...
    def stat(self): ...


def reverse_lines(handle: SeekableRemoteFile, block_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield non-empty lines from a seekable file, newest line first."""
    size = handle.stat().st_size
    position = size
    carry = b""
    while position > 0:
        read_size = min(block_size, position)
        position -= read_size
        handle.seek(position)
        block = handle.read(read_size) + carry
        parts = block.split(b"\n")
        carry = parts[0]
        for line in reversed(parts[1:]):
            if line:
                yield line.rstrip(b"\r")
    if carry:
        yield carry.rstrip(b"\r")
