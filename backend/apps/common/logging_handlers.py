from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path


class CopyTruncateRotatingFileHandler(logging.FileHandler):
    """Size-based log rotation that never renames the active log file.

    Windows does not allow ``os.rename``/``os.replace`` when another process has
    the file open without delete sharing. Django's standard RotatingFileHandler
    therefore raises WinError 32 in that situation. This handler copies the full
    active log to a timestamped archive and then truncates the active file in
    place, so ``tracelens.log`` keeps the same path for every process.

    Rotation is best-effort: a transient copy/truncate/lock failure must never
    break application logging. In that case the active file is simply allowed to
    grow until a later emit can rotate it successfully.
    """

    def __init__(
        self,
        filename,
        mode="a",
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        errors=None,
    ):
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay, errors=errors)
        self.maxBytes = max(0, int(maxBytes or 0))
        self.backupCount = max(0, int(backupCount or 0))
        self._lock_path = f"{self.baseFilename}.rotate.lock"

    def _should_rollover(self, record: logging.LogRecord) -> bool:
        if self.maxBytes <= 0:
            return False
        try:
            current_size = os.path.getsize(self.baseFilename)
        except OSError:
            current_size = 0
        try:
            rendered = self.format(record) + self.terminator
            incoming_size = len(rendered.encode(self.encoding or "utf-8", errors="replace"))
        except Exception:
            incoming_size = 0
        return current_size + incoming_size >= self.maxBytes

    def _try_acquire_rotation_lock(self) -> int | None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            return os.open(self._lock_path, flags)
        except FileExistsError:
            # A crashed process may leave the tiny coordination file behind.
            try:
                if (datetime.now().timestamp() - os.path.getmtime(self._lock_path)) > 120:
                    os.unlink(self._lock_path)
                    return os.open(self._lock_path, flags)
            except OSError:
                pass
            return None
        except OSError:
            return None

    def _release_rotation_lock(self, fd: int | None) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(self._lock_path)
        except OSError:
            pass

    def _archive_path(self) -> Path:
        active = Path(self.baseFilename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        candidate = active.with_name(f"{active.stem}.{timestamp}{active.suffix}")
        sequence = 1
        while candidate.exists():
            candidate = active.with_name(f"{active.stem}.{timestamp}_{sequence}{active.suffix}")
            sequence += 1
        return candidate

    def _cleanup_archives(self) -> None:
        if self.backupCount <= 0:
            return
        active = Path(self.baseFilename)
        archives = sorted(
            active.parent.glob(f"{active.stem}.[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_*{active.suffix}"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in archives[self.backupCount :]:
            try:
                stale.unlink()
            except OSError:
                pass

    def doRollover(self) -> bool:
        lock_fd = self._try_acquire_rotation_lock()
        if lock_fd is None:
            return False
        try:
            try:
                if not os.path.exists(self.baseFilename) or os.path.getsize(self.baseFilename) < self.maxBytes:
                    return False
            except OSError:
                return False

            if self.stream:
                try:
                    self.stream.flush()
                except OSError:
                    pass

            archive = self._archive_path()
            try:
                # Crucially, do not rename/delete the active file. Both operations
                # can fail with WinError 32 while another process still has it open.
                with open(self.baseFilename, "rb") as source, open(archive, "xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()

                # Close only this handler's stream, truncate the same path, then
                # reopen it in append mode. Other processes can keep their handle.
                if self.stream:
                    try:
                        self.stream.close()
                    finally:
                        self.stream = None
                with open(self.baseFilename, "r+b") as active_file:
                    active_file.truncate(0)
                self._cleanup_archives()
                return True
            except OSError:
                # A failed attempt is not a completed archive. Remove a partial or
                # duplicate copy so repeated retries cannot fill the runtime folder.
                try:
                    if archive.exists():
                        archive.unlink()
                except OSError:
                    pass
                # Continue writing the active file and retry rotation on a later emit.
                return False
            finally:
                if self.stream is None and not self.delay:
                    try:
                        self.stream = self._open()
                    except OSError:
                        self.stream = None
        finally:
            self._release_rotation_lock(lock_fd)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._should_rollover(record):
                self.doRollover()
            super().emit(record)
        except Exception:
            # Match logging's failure containment, but avoid an additional noisy
            # traceback caused solely by a failed rollover on Windows.
            if logging.raiseExceptions:
                try:
                    super().handleError(record)
                except Exception:
                    pass
