import json
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from django.urls import reverse
from .models import Device, PairingRequest, RetentionPolicy

User = get_user_model()


class PairingFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="camera-owner", password="Strong-Pass-12345")

    def test_user_can_create_short_lived_qr_pairing(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("create-pairing"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["qr_payload"].startswith("iot-alarm-pair:"))
        self.assertIn("data:image/svg+xml;base64,", payload["qr_data_url"])
        pairing = PairingRequest.objects.get(pk=payload["pairing_id"])
        self.assertEqual(pairing.owner, self.owner)
        self.assertTrue(pairing.is_valid)

    def test_owner_can_repair_device_to_rotate_lost_device_token(self):
        self.client.force_login(self.owner)
        first_qr = self.client.post(reverse("create-pairing")).json()
        first_code = first_qr["qr_payload"].split(":", 1)[1]
        first = self.client.post(
            reverse("pair-device"),
            data=json.dumps({
                "pairing_code": first_code,
                "device_id": "cam-recover-token-01",
                "name": "Recovery Camera",
            }),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 201)
        old_token = first.json()["api_token"]

        second_qr = self.client.post(reverse("create-pairing")).json()
        second_code = second_qr["qr_payload"].split(":", 1)[1]
        second = self.client.post(
            reverse("pair-device"),
            data=json.dumps({
                "pairing_code": second_code,
                "device_id": "cam-recover-token-01",
                "name": "Recovery Camera",
            }),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 201)
        new_token = second.json()["api_token"]
        self.assertNotEqual(old_token, new_token)

        heartbeat = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({"video_fps": 30}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {old_token}",
            HTTP_X_DEVICE_ID="cam-recover-token-01",
        )
        self.assertEqual(heartbeat.status_code, 401)

    def test_pairing_code_is_one_time_and_device_token_is_hashed(self):
        self.client.force_login(self.owner)
        qr_response = self.client.post(reverse("create-pairing"))
        raw_code = qr_response.json()["qr_payload"].split(":", 1)[1]
        response = self.client.post(
            reverse("pair-device"),
            data=json.dumps({
                "pairing_code": raw_code,
                "device_id": "cam-test-device-01",
                "name": "Entrance Camera",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        returned_token = response.json()["api_token"]
        device = Device.objects.get(device_id="cam-test-device-01")
        self.assertEqual(device.owner, self.owner)
        self.assertNotEqual(device.api_token_hash, returned_token)
        self.assertTrue(check_password(returned_token, device.api_token_hash))
        retry = self.client.post(
            reverse("pair-device"),
            data=json.dumps({
                "pairing_code": raw_code,
                "device_id": "cam-test-device-01",
                "name": "Entrance Camera",
            }),
            content_type="application/json",
        )
        self.assertEqual(retry.status_code, 403)


class DeviceApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="device-owner", password="Strong-Pass-12345")
        self.token = "test-device-token-that-is-not-stored-in-plain-text"
        self.device = Device.objects.create(
            device_id="cam-heartbeat-01",
            owner=self.owner,
            name="Heartbeat Camera",
            api_token_hash=make_password(self.token),
            stream_path="cam-heartbeat-01",
        )

    def test_device_heartbeat_updates_last_seen_and_health_metrics(self):
        response = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({
                "video_fps": 29.8,
                "inference_fps": 4.9,
                "model_loaded": True,
                "mqtt_connected": False,
                "rtsp_connected": True,
                "camera_ready": True,
                "pending_alerts": 2,
                "model": "yolov5n-ncnn",
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(response.status_code, 200)
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_online)
        self.assertTrue(self.device.model_loaded)
        self.assertAlmostEqual(self.device.reported_video_fps, 29.8)
        self.assertAlmostEqual(self.device.reported_inference_fps, 4.9)
        self.assertTrue(self.device.current_state["rtsp_connected"])
        self.assertEqual(self.device.current_state["pending_alerts"], 2)

    def test_bad_device_token_is_rejected(self):
        response = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({"video_fps": 30}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer wrong-token",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(response.status_code, 401)

    def test_inactive_owner_cannot_authenticate_device(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        response = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({"video_fps": 30}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(response.status_code, 401)


class UserDeviceIsolationTests(TestCase):
    def setUp(self):
        self.owner_a = User.objects.create_user(username="owner-a", password="Strong-Pass-12345")
        self.owner_b = User.objects.create_user(username="owner-b", password="Strong-Pass-12345")
        self.device_a = Device.objects.create(
            device_id="cam-owner-a-01", owner=self.owner_a, name="A Camera", kind=Device.RASPBERRY_PI
        )
        self.device_b = Device.objects.create(
            device_id="cam-owner-b-01", owner=self.owner_b, name="B Camera", kind=Device.RASPBERRY_PI
        )

    def test_user_only_sees_their_own_device(self):
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("dashboard:device-detail", args=[self.device_a.device_id]))
        self.assertEqual(response.status_code, 200)
        hidden = self.client.get(reverse("dashboard:device-detail", args=[self.device_b.device_id]))
        self.assertEqual(hidden.status_code, 404)

    def test_each_user_gets_an_independent_retention_policy(self):
        policy_a, _ = RetentionPolicy.objects.get_or_create(user=self.owner_a)
        policy_b, _ = RetentionPolicy.objects.get_or_create(user=self.owner_b)
        policy_a.alarm_image_days = 12
        policy_a.save()
        self.assertEqual(policy_b.alarm_image_days, 30)

