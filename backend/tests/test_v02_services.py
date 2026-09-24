from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stations = load("stations_parser_v02", "apps/environments/services/stations_parser.py")
selector = load("archive_selector_v02", "apps/logsources/services/archive_selector.py")
reverse_reader = load("reverse_reader_v02", "apps/logsources/services/reverse_reader.py")


def test_stations_xml():
    topology = stations.parse_stations_xml('''
    <stations userId="11">
      <station id="1" type="SCH" name="master" host="10.1.124.94" />
      <station id="11" type="LCH" name="SPUR_SUBRACK0_SLOT5_GPB" host="10.29.94.46" />
      <station id="12" type="LCH" name="SPUR_SUBRACK0_SLOT6_GPB" host="10.29.94.47" />
    </stations>
    ''')
    assert topology.user_id == "11"
    assert topology.upper.station_type == "SCH"
    assert topology.upper.host == "10.1.124.94"
    assert [item.station_id for item in topology.lowers] == ["11", "12"]


def test_archive_right_boundary_selection():
    A = selector.LogArtifact
    items = [
        A(1, "upper", "spws", "DSPWSFT", "/x/DSPWSFT_20260101020000.log", "archived", datetime(2026, 1, 1, 2)),
        A(1, "upper", "spws", "DSPWSFT", "/x/DSPWSFT_20260101040000.log", "archived", datetime(2026, 1, 1, 4)),
        A(1, "upper", "spws", "DSPWSFT", "/x/DSPWSFT.log", "current", None),
    ]
    selected = selector.select_artifacts_for_window(items, datetime(2026, 1, 1, 1, 1), datetime(2026, 1, 1, 3))
    assert [item.path for item in selected] == ["/x/DSPWSFT_20260101020000.log", "/x/DSPWSFT_20260101040000.log"]
    selected = selector.select_artifacts_for_window(items, datetime(2026, 1, 1, 3), datetime(2026, 1, 1, 5))
    assert [item.path for item in selected] == ["/x/DSPWSFT_20260101040000.log", "/x/DSPWSFT.log"]


def test_reverse_block_reader():
    class Stat:
        def __init__(self, size): self.st_size = size
    class Handle:
        def __init__(self, data): self.data = data; self.pos = 0
        def stat(self): return Stat(len(self.data))
        def seek(self, pos): self.pos = pos
        def read(self, size=-1):
            chunk = self.data[self.pos:] if size < 0 else self.data[self.pos:self.pos + size]
            self.pos += len(chunk)
            return chunk
    handle = Handle(b"first\nsecond\nthird\n")
    assert list(reverse_reader.reverse_lines(handle, block_size=5)) == [b"third", b"second", b"first"]


def test_short_timestamp_and_tar_member_name():
    assert selector.parse_log_name("DSPWSFT_080102.log", datetime(2026, 8, 1))[1] == datetime(2026, 8, 1, 2)
    fm, boundary = selector.parse_log_name("folder/RSPM_20260805154139.log")
    assert fm == "RSPM"
    assert boundary == datetime(2026, 8, 5, 15, 41, 39)


if __name__ == "__main__":
    test_stations_xml()
    test_archive_right_boundary_selection()
    test_reverse_block_reader()
    test_short_timestamp_and_tar_member_name()
    print("v0.2 services smoke tests passed")
