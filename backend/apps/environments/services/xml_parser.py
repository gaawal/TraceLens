from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


class XmlDiscoveryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LowerMachineCandidate:
    host: str
    name: str
    metadata: dict[str, str]


def _extract_value(element: ElementTree.Element, selector: str) -> str:
    selector = (selector or "").strip()
    if not selector:
        return ""
    if selector == "text()":
        return (element.text or "").strip()
    if selector.startswith("@"):
        return (element.attrib.get(selector[1:]) or "").strip()
    child = element.find(selector)
    if child is None:
        return ""
    return (child.text or "").strip()


def parse_lower_machines(
    xml_content: str,
    *,
    lower_machine_xpath: str,
    host_selector: str,
    name_selector: str = "",
) -> list[LowerMachineCandidate]:
    if not xml_content.strip():
        raise XmlDiscoveryError("XML 内容不能为空。")
    if not lower_machine_xpath.strip():
        raise XmlDiscoveryError("必须配置下位机节点 XPath。")

    try:
        root = ElementTree.fromstring(xml_content)
    except ElementTree.ParseError as exc:
        raise XmlDiscoveryError(f"XML 解析失败：{exc}") from exc

    try:
        nodes = root.findall(lower_machine_xpath)
    except (SyntaxError, KeyError) as exc:
        raise XmlDiscoveryError(f"XPath 无效：{lower_machine_xpath}") from exc

    results: list[LowerMachineCandidate] = []
    seen_hosts: set[str] = set()
    for index, node in enumerate(nodes, start=1):
        host = _extract_value(node, host_selector)
        if not host:
            raise XmlDiscoveryError(f"第 {index} 个下位机节点未提取到主机地址。")
        if host in seen_hosts:
            continue
        name = _extract_value(node, name_selector) if name_selector else ""
        results.append(
            LowerMachineCandidate(
                host=host,
                name=name or f"下位机-{host}",
                metadata={"tag": node.tag, "attributes": dict(node.attrib)},
            )
        )
        seen_hosts.add(host)
    return results
