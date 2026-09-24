from __future__ import annotations

import logging
import re
from pathlib import Path

from apps.common.logging_handlers import CopyTruncateRotatingFileHandler


ROOT = Path(__file__).resolve().parents[1]


def test_settings_use_copy_truncate_handler_instead_of_rename_rotation():
    settings_text = (ROOT / "config/settings.py").read_text(encoding="utf-8")
    assert "apps.common.logging_handlers.CopyTruncateRotatingFileHandler" in settings_text
    assert "logging.handlers.RotatingFileHandler" not in settings_text


def test_rotation_keeps_active_name_and_archives_with_timestamp(tmp_path: Path):
    active = tmp_path / "tracelens.log"
    handler = CopyTruncateRotatingFileHandler(
        active,
        maxBytes=180,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.tracelens.copytruncate")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    # Keep a second handle open while rollover occurs. The active file must not
    # be renamed, which is the operation that triggers WinError 32 on Windows.
    with active.open("a", encoding="utf-8") as external_handle:
        external_handle.write("external-open\n")
        external_handle.flush()
        for index in range(40):
            logger.info("line-%02d-%s", index, "x" * 32)

    handler.close()
    logger.handlers.clear()

    assert active.exists()
    assert not (tmp_path / "tracelens.log.1").exists()
    archives = sorted(tmp_path.glob("tracelens.*.log"))
    assert 1 <= len(archives) <= 2
    assert all(re.fullmatch(r"tracelens\.\d{8}_\d{6}_\d{6}(?:_\d+)?\.log", item.name) for item in archives)
