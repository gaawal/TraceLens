from django.test import TestCase

from apps.logsources.models import LogSourceRule
from apps.machines.models import Machine, MachineRole


class LogSourceRuleTests(TestCase):
    def test_resolve_default_path(self):
        machine = Machine.objects.create(
            name="L4", host="192.168.1.10", username="tester", role=MachineRole.UPPER
        )
        rule = LogSourceRule(machine=machine, name="debug")
        self.assertEqual(rule.resolve_root_path(), "/log/tester/debug")
