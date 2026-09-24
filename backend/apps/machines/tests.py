from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.machines.models import AuthenticationType, MachineRole


@override_settings(TRACELENS_CREDENTIAL_KEY="MDAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ1Njc4OTA=")
class MachineApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_create_upper_machine_creates_environment_and_hides_password(self):
        response = self.client.post(
            "/api/machines/",
            {
                "name": "上位机A",
                "host": "192.168.10.10",
                "username": "root",
                "role": MachineRole.UPPER,
                "auth_type": AuthenticationType.PASSWORD,
                "password": "secret",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("password", response.data)
        self.assertIsNotNone(response.data["environment_id"])
