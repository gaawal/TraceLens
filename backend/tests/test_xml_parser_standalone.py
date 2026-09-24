import importlib.util
from pathlib import Path
import unittest
import sys

MODULE_PATH = Path(__file__).parents[1] / "apps" / "environments" / "services" / "xml_parser.py"
spec = importlib.util.spec_from_file_location("xml_parser", MODULE_PATH)
xml_parser = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = xml_parser
spec.loader.exec_module(xml_parser)


class StandaloneXmlParserTests(unittest.TestCase):
    def test_attribute_and_child_selectors(self):
        xml = """
        <Environment>
          <LowerMachine name="L3-A"><host>192.168.0.21</host></LowerMachine>
          <LowerMachine name="L3-B"><host>192.168.0.22</host></LowerMachine>
        </Environment>
        """
        result = xml_parser.parse_lower_machines(
            xml,
            lower_machine_xpath=".//LowerMachine",
            host_selector="host",
            name_selector="@name",
        )
        self.assertEqual([item.host for item in result], ["192.168.0.21", "192.168.0.22"])
        self.assertEqual([item.name for item in result], ["L3-A", "L3-B"])

    def test_duplicate_hosts_are_removed(self):
        xml = '<E><M ip="1.1.1.1"/><M ip="1.1.1.1"/></E>'
        result = xml_parser.parse_lower_machines(
            xml,
            lower_machine_xpath=".//M",
            host_selector="@ip",
        )
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
