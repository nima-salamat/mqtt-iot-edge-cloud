import json
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from .models import Alarm, Device, PairingRequest, RetentionPolicy, StreamGrant

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

    def test_mediamtx_publish_auth_accepts_internal_compose_service_host(self):
        response = self.client.post(
            reverse("mediamtx-auth"),
            data=json.dumps({
                "user": self.device.device_id,
                "password": self.token,
                "action": "publish",
                "path": self.device.stream_path,
            }),
            content_type="application/json",
            HTTP_HOST="api:8000",
        )

        self.assertEqual(response.status_code, 204)

    def test_device_can_disconnect_itself_and_revoke_stream_grants(self):
        grant = StreamGrant.objects.create(
            token_hash="a" * 64,
            user=self.owner,
            device=self.device,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        response = self.client.post(
            reverse("device-disconnect"),
            data="{}",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "disconnected")
        self.device.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(self.device.api_token_hash, "")
        self.assertEqual(self.device.stream_path, "")
        self.assertFalse(self.device.is_online)
        self.assertFalse(self.device.model_loaded)
        self.assertEqual(self.device.current_state["connection_state"], "disconnected")
        self.assertIsNotNone(grant.revoked_at)
        retry = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({"video_fps": 30}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(retry.status_code, 401)

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

    def test_owner_can_disconnect_device_but_another_user_cannot(self):
        StreamGrant.objects.create(
            token_hash="b" * 64,
            user=self.owner_a,
            device=self.device_a,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        self.client.force_login(self.owner_b)
        hidden = self.client.post(
            reverse("dashboard:device-disconnect", args=[self.device_a.device_id])
        )
        self.assertEqual(hidden.status_code, 404)
        self.client.force_login(self.owner_a)
        response = self.client.post(
            reverse("dashboard:device-disconnect", args=[self.device_a.device_id])
        )
        self.assertEqual(response.status_code, 200)
        self.device_a.refresh_from_db()
        self.assertFalse(self.device_a.api_token_hash)
        self.assertFalse(self.device_a.is_online)
        self.assertFalse(self.device_a.stream_path)
        self.assertIsNotNone(StreamGrant.objects.get(device=self.device_a).revoked_at)

    def test_each_user_gets_an_independent_retention_policy(self):
        policy_a, _ = RetentionPolicy.objects.get_or_create(user=self.owner_a)
        policy_b, _ = RetentionPolicy.objects.get_or_create(user=self.owner_b)
        policy_a.alarm_image_days = 12
        policy_a.save()
        self.assertEqual(policy_b.alarm_image_days, 30)



class LiveDashboardAlertTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="live-alert-owner", password="Strong-Pass-12345"
        )
        self.other_owner = User.objects.create_user(
            username="other-live-alert-owner", password="Strong-Pass-12345"
        )
        self.device = Device.objects.create(
            device_id="cam-live-alert-01", owner=self.owner, name="Front Camera"
        )
        self.other_device = Device.objects.create(
            device_id="cam-live-alert-02", owner=self.other_owner, name="Other Camera"
        )
        self.client.force_login(self.owner)

    def _alarm(self, device, alert_id):
        return Alarm.objects.create(
            device=device,
            alert_id=alert_id,
            confidence=0.91,
            bbox=[{"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.8}],
        )

    def test_alert_feed_returns_only_alerts_after_cursor_for_current_user(self):
        first = self._alarm(self.device, "live-alert-first")
        second = self._alarm(self.device, "live-alert-second")
        self._alarm(self.other_device, "not-visible-alert")

        response = self.client.get(
            reverse("dashboard-alerts"), {"after": first.pk}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["alerts"]], [second.pk])
        self.assertEqual(payload["next_cursor"], second.pk)
        self.assertEqual(payload["alerts"][0]["device_name"], "Front Camera")
        self.assertEqual(payload["alerts"][0]["image_url"], None)
        self.assertEqual(payload["alerts"][0]["device_url"],
                         reverse("dashboard:device-detail", args=[self.device.device_id]))

    def test_alert_feed_rejects_access_to_another_users_device(self):
        response = self.client.get(
            reverse("dashboard-alerts"),
            {"after": 0, "device_id": self.other_device.device_id},
        )
        self.assertEqual(response.status_code, 404)

    def test_alert_feed_rejects_invalid_cursor(self):
        response = self.client.get(reverse("dashboard-alerts"), {"after": "invalid"})
        self.assertEqual(response.status_code, 400)
