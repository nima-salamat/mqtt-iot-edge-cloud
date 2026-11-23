from django.test import TestCase

from .models import Device, Telemetry


class DeviceDataModelTests(TestCase):
    def setUp(self):
        self.device = Device.objects.create(
            device_id="raspberry-pi-01",
            name="Raspberry Pi 4",
            kind=Device.RASPBERRY_PI,
        )

    def test_device_starts_offline(self):
        self.assertFalse(self.device.is_online)
        self.assertEqual(self.device.device_id, "raspberry-pi-01")

    def test_telemetry_stores_json_payload(self):
        row = Telemetry.objects.create(
            device=self.device,
            topic="iotlab/devices/raspberry-pi-01/telemetry",
            qos=1,
            payload={"temperature_c": 24.5, "humidity_percent": 48.0},
        )
        saved = Telemetry.objects.get(pk=row.pk)
        self.assertEqual(saved.payload["temperature_c"], 24.5)
        self.assertEqual(saved.qos, 1)
