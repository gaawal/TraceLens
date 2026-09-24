from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.environments.models import Environment, MachineRelation
from apps.environments.services.xml_parser import XmlDiscoveryError, parse_lower_machines
from apps.environments.views import _is_small_network_lower, _probe_environment_runtime
from apps.machines.models import Machine, MachineRole


class XmlParserTests(TestCase):
    def test_parse_attribute_based_lower_machines(self):
        result = parse_lower_machines(
            '<Environment><LowerMachine name="L3-1" ip="192.168.1.20"/></Environment>',
            lower_machine_xpath=".//LowerMachine",
            host_selector="@ip",
            name_selector="@name",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].host, "192.168.1.20")
        self.assertEqual(result[0].name, "L3-1")

    def test_missing_host_raises_clear_error(self):
        with self.assertRaises(XmlDiscoveryError):
            parse_lower_machines(
                "<Environment><LowerMachine/></Environment>",
                lower_machine_xpath=".//LowerMachine",
                host_selector="@ip",
            )


class RelationValidationTests(TestCase):
    def setUp(self):
        self.upper = Machine.objects.create(name="L4", host="10.0.0.1", role=MachineRole.UPPER)
        self.lower = Machine.objects.create(name="L3", host="10.0.0.2", role=MachineRole.LOWER)
        self.environment = Environment.objects.create(name="环境A", upper_machine=self.upper)

    def test_valid_relation(self):
        relation = MachineRelation(
            environment=self.environment,
            source_machine=self.upper,
            target_machine=self.lower,
        )
        relation.full_clean()
        relation.save()
        self.assertEqual(relation.target_machine.role, MachineRole.LOWER)


class RuntimeStatusSmallNetworkTests(TestCase):
    def setUp(self):
        self.upper = Machine.objects.create(name="L4", host="10.0.0.1", username="user", role=MachineRole.UPPER)
        self.lower = Machine.objects.create(name="small-net", host="192.168.10.20", username="root", role=MachineRole.LOWER)
        self.environment = Environment.objects.create(name="小网环境", upper_machine=self.upper)
        MachineRelation.objects.create(
            environment=self.environment,
            source_machine=self.upper,
            target_machine=self.lower,
        )

    def test_192_lower_is_small_network(self):
        self.assertTrue(_is_small_network_lower("192.168.10.20"))
        self.assertFalse(_is_small_network_lower("10.168.10.20"))

    @patch("apps.environments.views.execute")
    def test_runtime_probe_skips_ssh_for_192_lower(self, execute_mock):
        execute_mock.return_value = SimpleNamespace(exit_status=0, stdout="TRACELENS_OK", stderr="")

        payload = _probe_environment_runtime(self.environment)

        self.assertEqual(execute_mock.call_count, 1)
        self.assertEqual(execute_mock.call_args.args[0].id, self.upper.id)
        self.assertEqual(len(payload["lowers"]), 1)
        lower = payload["lowers"][0]
        self.assertTrue(lower["skipped"])
        self.assertEqual(lower["skip_reason"], "small_network")
        self.assertIn("跳过 SSH", lower["message"])
