from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


class StationsXmlError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    station_type: str
    name: str
    host: str


@dataclass(frozen=True, slots=True)
class StationsTopology:
    user_id: str
    upper: Station
    lowers: tuple[Station, ...]
    dhh: Station | None = None


def parse_stations_xml(xml_content: str) -> StationsTopology:
    try:
        root = ElementTree.fromstring(xml_content)
    except ElementTree.ParseError as exc:
        raise StationsXmlError(f"stations.xml 解析失败：{exc}") from exc
    if root.tag != "stations":
        raise StationsXmlError("根节点必须是 stations。")

    user_id = (root.attrib.get("userId") or "").strip()
    stations: list[Station] = []
    for index, node in enumerate(root.findall("station"), start=1):
        station_type = (node.attrib.get("type") or "").strip().upper()
        station_id = (node.attrib.get("id") or "").strip()
        name = (node.attrib.get("name") or "").strip()
        host = (node.attrib.get("host") or "").strip()
        # DHH is a special station used by some environments to carry executor logs.
        if station_type not in {"SCH", "LCH", "DHH"} and name.lower() != "dhh":
            continue
        if not host:
            raise StationsXmlError(f"第 {index} 个 {station_type} 节点缺少 host。")
        stations.append(Station(station_id, station_type, name or f"{station_type}-{host}", host))

    uppers = [item for item in stations if item.station_type == "SCH"]
    lowers = [
        item for item in stations
        if item.station_type == "LCH" and item.name.strip().lower() != "dhh"
    ]
    dhh_candidates = [
        item for item in stations
        if item.station_type == "DHH" or item.name.strip().lower() == "dhh"
    ]
    if len(uppers) != 1:
        raise StationsXmlError(f"必须且只能存在一个 SCH，当前发现 {len(uppers)} 个。")
    if not lowers and not dhh_candidates:
        raise StationsXmlError("未发现任何 LCH 下位机或 DHH 节点。")
    if len(dhh_candidates) > 1:
        exact = [item for item in dhh_candidates if item.name.strip().lower() == "dhh"]
        if len(exact) == 1:
            dhh = exact[0]
        else:
            raise StationsXmlError(f"DHH 节点只能存在一个，当前发现 {len(dhh_candidates)} 个。")
    else:
        dhh = dhh_candidates[0] if dhh_candidates else None
    return StationsTopology(user_id=user_id, upper=uppers[0], lowers=tuple(lowers), dhh=dhh)
