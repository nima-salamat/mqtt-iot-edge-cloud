import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Device, Telemetry


class DashboardApiTests(TestCase):
    def setUp(self):
        self.device = Device.objects.create(
            device_id="raspberry-pi-01",
            name="Raspberry Pi 4",
            kind="raspberry-pi",
        )
        self.user = get_user_model().objects.create_user(
            username="presenter",
            password="A-long-test-password-123",
        )
        self.client.force_login(self.user)

    def test_state_api_returns_pi_device(self):
        response = self.client.get(reverse("api_state"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["devices"][0]["device_id"], "raspberry-pi-01")
        self.assertEqual(response.json()["total_messages"], 0)

    def test_incoming_messages_are_returned(self):
        Telemetry.objects.create(
            device=self.device,
            topic="iotlab/devices/raspberry-pi-01/events",
            qos=1,
            payload={"message": "hello from Pi"},
        )
        response = self.client.get(reverse("api_state"))
        body = response.json()
        self.assertEqual(body["total_messages"], 1)
        self.assertEqual(body["recent_messages"][0]["payload"]["message"], "hello from Pi")

    @patch("dashboard.views.publish_json")
    def test_command_is_published_to_pi(self, publish_json_mock):
        response = self.client.post(
            reverse("api_command", args=["raspberry-pi-01"]),
            data=json.dumps({"command": "set_output", "state": "on"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"]["state"], "on")
        publish_json_mock.assert_called_once_with(
            "iotlab/devices/raspberry-pi-01/command",
            {"command": "set_output", "state": "on"},
            qos=1,
        )

    @patch("dashboard.views.publish_json")
    def test_invalid_command_is_rejected(self, publish_json_mock):
        response = self.client.post(
            reverse("api_command", args=["raspberry-pi-01"]),
            data=json.dumps({"command": "unknown"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        publish_json_mock.assert_not_called()

    def test_dashboard_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_unknown_device_returns_404(self):
        response = self.client.post(
            reverse("api_command", args=["not-here"]),
            data=json.dumps({"command": "set_output", "state": "on"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
