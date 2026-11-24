import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from .models import Alarm, Device, DeviceLog, PairingRequest, RetentionPolicy, StreamGrant, VideoSegment

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
                "recording_enabled": True,
                "last_video_upload_at": "2026-11-24T10:00:00+00:00",
                "last_video_upload_error": "",
                "system": {
                    "cpu_percent": 42.5,
                    "temperature_c": 51.8,
                    "memory": {"total_bytes": 1000, "used_bytes": 400, "available_bytes": 600, "used_percent": 40.0},
                    "storage": {"total_bytes": 2000, "used_bytes": 500, "free_bytes": 1500, "used_percent": 25.0, "path": "/private/pi/path"},
                },
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
        self.assertTrue(self.device.current_state["recording_enabled"])
        self.assertEqual(
            self.device.current_state["last_video_upload_at"],
            "2026-11-24T10:00:00+00:00",
        )
        self.assertEqual(self.device.current_state["last_video_upload_error"], "")
        self.assertEqual(self.device.current_state["system"]["temperature_c"], 51.8)
        self.assertEqual(self.device.current_state["system"]["memory"]["used_percent"], 40.0)
        self.assertNotIn("path", self.device.current_state["system"]["storage"])

    def test_device_heartbeat_discards_invalid_system_metrics(self):
        response = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({
                "system": {
                    "cpu_percent": float("nan"),
                    "temperature_c": 900,
                    "memory": {"used_percent": 140},
                    "storage": {"path": "/should/not/be/stored"},
                },
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(response.status_code, 200)
        self.device.refresh_from_db()
        # Invalid/empty readings must not create an empty snapshot or erase
        # a previously valid system-health report.
        self.assertNotIn("system", self.device.current_state)

    def test_heartbeat_merges_partial_system_metrics_without_losing_valid_readings(self):
        self.device.current_state = {
            "system": {
                "cpu_percent": 23.0,
                "temperature_c": 48.0,
                "memory": {
                    "total_bytes": 8_000_000,
                    "used_bytes": 4_480_000,
                    "available_bytes": 3_520_000,
                    "used_percent": 56.0,
                },
            },
        }
        self.device.save(update_fields=["current_state"])
        response = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({
                "system": {
                    "cpu_percent": None,
                    "temperature_c": 49.0,
                    "memory": {"total_bytes": 8_100_000},
                },
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(response.status_code, 200)
        self.device.refresh_from_db()
        system = self.device.current_state["system"]
        self.assertEqual(system["cpu_percent"], 23.0)
        self.assertEqual(system["temperature_c"], 49.0)
        self.assertEqual(system["memory"]["total_bytes"], 8_100_000)
        self.assertEqual(system["memory"]["used_percent"], 56.0)

        empty_response = self.client.post(
            reverse("device-heartbeat"),
            data=json.dumps({"system": {"cpu_percent": None, "memory": {}}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
            HTTP_X_DEVICE_ID=self.device.device_id,
        )
        self.assertEqual(empty_response.status_code, 200)
        self.device.refresh_from_db()
        self.assertEqual(self.device.current_state["system"], system)

    def test_device_can_upload_an_mp4_segment_and_retry_it_idempotently(self):
        video_bytes = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
        segment_id = "1795533000000-abcd1234"
        data = {
            "segment_id": segment_id,
            "sha256": hashlib.sha256(video_bytes).hexdigest(),
            "started_at": timezone.now().isoformat(),
            "duration_seconds": "5",
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("device-upload-segment"),
                data={**data, "file": SimpleUploadedFile("clip.mp4", video_bytes, content_type="video/mp4")},
                HTTP_AUTHORIZATION=f"Bearer {self.token}",
                HTTP_X_DEVICE_ID=self.device.device_id,
            )
            self.assertEqual(response.status_code, 201, response.content)
            self.assertTrue(response.json()["created"])
            segment = VideoSegment.objects.get(device=self.device, segment_id=segment_id)
            self.assertEqual(segment.size_bytes, len(video_bytes))
            self.assertEqual(Path(segment.file.path).read_bytes(), video_bytes)

            retry = self.client.post(
                reverse("device-upload-segment"),
                data={**data, "file": SimpleUploadedFile("clip.mp4", video_bytes, content_type="video/mp4")},
                HTTP_AUTHORIZATION=f"Bearer {self.token}",
                HTTP_X_DEVICE_ID=self.device.device_id,
            )
            self.assertEqual(retry.status_code, 200, retry.content)
            self.assertFalse(retry.json()["created"])
            self.assertEqual(VideoSegment.objects.filter(device=self.device, segment_id=segment_id).count(), 1)

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



class DashboardCleanupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="cleanup-owner", password="Strong-Pass-12345"
        )
        self.other_owner = User.objects.create_user(
            username="cleanup-other", password="Strong-Pass-12345"
        )
        self.device = Device.objects.create(
            device_id="cam-cleanup-owner", owner=self.owner, name="Owner Camera"
        )
        self.other_device = Device.objects.create(
            device_id="cam-cleanup-other", owner=self.other_owner, name="Other Camera"
        )
        self.media_directory = tempfile.TemporaryDirectory()
        media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

    def _add_alarm(self, device, alert_id):
        alarm = Alarm.objects.create(
            device=device, alert_id=alert_id, confidence=0.9
        )
        alarm.snapshot.save(
            f"{alert_id}.jpg", ContentFile(b"fake-jpeg-evidence"), save=True
        )
        return alarm

    def _add_video(self, device, segment_id):
        segment = VideoSegment.objects.create(
            device=device, segment_id=segment_id, duration_seconds=5, size_bytes=10
        )
        segment.file.save(
            f"{segment_id}.mp4", ContentFile(b"0123456789"), save=True
        )
        return segment

    def test_owner_can_clean_all_evidence_for_one_camera(self):
        own_alarm = self._add_alarm(self.device, "cleanup-alarm-owner")
        own_video = self._add_video(self.device, "cleanup-video-owner")
        own_log = DeviceLog.objects.create(
            device=self.device, event_id="cleanup-log-owner", message="owner log"
        )
        other_alarm = self._add_alarm(self.other_device, "cleanup-alarm-other")
        other_video = self._add_video(self.other_device, "cleanup-video-other")
        other_log = DeviceLog.objects.create(
            device=self.other_device, event_id="cleanup-log-other", message="other log"
        )
        own_alarm_path = Path(own_alarm.snapshot.path)
        own_video_path = Path(own_video.file.path)
        other_alarm_path = Path(other_alarm.snapshot.path)
        other_video_path = Path(other_video.file.path)

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:cleanup-data"),
            data=json.dumps({"kind": "all", "device_id": self.device.device_id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed_alarms"], 1)
        self.assertEqual(response.json()["removed_videos"], 1)
        self.assertEqual(response.json()["removed_logs"], 1)
        self.assertFalse(Alarm.objects.filter(pk=own_alarm.pk).exists())
        self.assertFalse(VideoSegment.objects.filter(pk=own_video.pk).exists())
        self.assertFalse(DeviceLog.objects.filter(pk=own_log.pk).exists())
        self.assertFalse(own_alarm_path.exists())
        self.assertFalse(own_video_path.exists())
        self.assertTrue(Alarm.objects.filter(pk=other_alarm.pk).exists())
        self.assertTrue(VideoSegment.objects.filter(pk=other_video.pk).exists())
        self.assertTrue(DeviceLog.objects.filter(pk=other_log.pk).exists())
        self.assertTrue(other_alarm_path.exists())
        self.assertTrue(other_video_path.exists())

    def test_owner_cannot_clean_another_users_camera(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:cleanup-data"),
            data=json.dumps({"kind": "all", "device_id": self.other_device.device_id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Device.objects.filter(pk=self.other_device.pk).exists())

    def test_cleanup_rejects_unknown_data_kind(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:cleanup-data"),
            data=json.dumps({"kind": "mqtt"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class DashboardVideoRangeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="video-range-owner", password="Strong-Pass-12345"
        )
        self.device = Device.objects.create(
            device_id="cam-video-range", owner=self.owner, name="Range Camera"
        )
        self.media_directory = tempfile.TemporaryDirectory()
        media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

    def test_video_feed_refreshes_with_new_segments_and_reports_recording_health(self):
        old = VideoSegment.objects.create(
            device=self.device,
            segment_id="feed-old",
            started_at=timezone.now() - timedelta(seconds=10),
            duration_seconds=5,
            size_bytes=8,
        )
        old.file.save("feed-old.mp4", ContentFile(b"old-video"), save=True)
        latest = VideoSegment.objects.create(
            device=self.device,
            segment_id="feed-latest",
            started_at=timezone.now(),
            duration_seconds=5,
            size_bytes=11,
        )
        latest.file.save("feed-latest.mp4", ContentFile(b"latest-video"), save=True)
        self.device.current_state = {
            "recording_enabled": True,
            "rtsp_connected": True,
            "pending_video_segments": 2,
            "last_video_upload_error": "",
        }
        self.device.save(update_fields=["current_state"])

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("dashboard:device-videos", args=[self.device.device_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        payload = response.json()
        self.assertEqual([item["segment_id"] for item in payload["segments"]], ["feed-old", "feed-latest"])
        self.assertTrue(payload["recording_enabled"])
        self.assertTrue(payload["rtsp_connected"])
        self.assertEqual(payload["pending_video_segments"], 2)
        self.assertEqual(
            payload["segments"][-1]["url"],
            reverse("dashboard:segment-file", args=[latest.pk]),
        )
        self.assertTrue(payload["last_video_upload_at"])

    def test_video_feed_includes_persisted_hardware_metrics(self):
        self.device.current_state = {
            "system": {
                "temperature_c": 48.6,
                "cpu_percent": 21.0,
                "memory": {"used_percent": 55.0},
                "storage": {"used_percent": 67.0},
            }
        }
        self.device.save(update_fields=["current_state"])
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("dashboard:device-videos", args=[self.device.device_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["system"]["temperature_c"], 48.6)
        self.assertEqual(response.json()["system"]["memory"]["used_percent"], 55.0)

    def test_video_feed_does_not_expose_another_users_camera(self):
        other = User.objects.create_user(username="video-feed-other", password="Strong-Pass-12345")
        other_device = Device.objects.create(
            device_id="cam-video-feed-other", owner=other, name="Other Video Camera"
        )
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("dashboard:device-videos", args=[other_device.device_id])
        )
        self.assertEqual(response.status_code, 404)

    def test_device_video_chunk_is_owner_scoped_and_remuxes_adjacent_files(self):
        first = VideoSegment.objects.create(
            device=self.device, segment_id="chunk-first", duration_seconds=5
        )
        first.file.save("chunk-first.mp4", ContentFile(b"first-input"), save=True)
        second = VideoSegment.objects.create(
            device=self.device,
            segment_id="chunk-second",
            started_at=timezone.now() + timedelta(seconds=5),
            duration_seconds=5,
        )
        second.file.save("chunk-second.mp4", ContentFile(b"second-input"), save=True)

        from unittest.mock import patch, Mock

        def fake_run(command, **kwargs):
            if command[0] == "ffprobe":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [{"codec_name": "h264", "profile": "High", "level": 31}]}),
                    stderr="",
                )
            output_path = Path(command[-1])
            output_path.write_bytes(b"F" * 1024)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.client.force_login(self.owner)
        with patch("dashboard.views.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
            "dashboard.views.subprocess.run", side_effect=fake_run
        ):
            response = self.client.get(
                reverse("dashboard:device-video-chunk", args=[self.device.device_id]),
                {"start_id": first.pk, "seconds": 10},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "video/mp4")
            self.assertEqual(response["X-Video-Codec"], "avc1.64001f")
            self.assertEqual(response["X-Video-Segment-Count"], "2")
            self.assertEqual(b"".join(response.streaming_content), b"F" * 1024)
            response.close()

    def test_device_video_chunk_returns_clear_error_when_ffmpeg_is_missing(self):
        segment = VideoSegment.objects.create(
            device=self.device, segment_id="chunk-no-ffmpeg", duration_seconds=5
        )
        segment.file.save("chunk-no-ffmpeg.mp4", ContentFile(b"video"), save=True)
        from unittest.mock import patch
        self.client.force_login(self.owner)
        with patch("dashboard.views.shutil.which", return_value=None):
            response = self.client.get(
                reverse("dashboard:device-video-chunk", args=[self.device.device_id]),
                {"start_id": segment.pk, "seconds": 12},
            )
        self.assertEqual(response.status_code, 503)

    def test_archived_video_supports_browser_byte_ranges(self):
        segment = VideoSegment.objects.create(
            device=self.device, segment_id="range-segment", duration_seconds=5
        )
        segment.file.save("sample.mp4", ContentFile(b"0123456789"), save=True)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("dashboard:segment-file", args=[segment.pk]),
            HTTP_RANGE="bytes=2-5",
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(b"".join(response.streaming_content), b"2345")
