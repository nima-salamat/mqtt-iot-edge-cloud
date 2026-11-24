from __future__ import annotations
from django.conf import settings
from django.db import models
from django.utils import timezone


class Device(models.Model):
    RASPBERRY_PI = "raspberry-pi"
    KIND_CHOICES = [(RASPBERRY_PI, "Raspberry Pi Camera")]

    device_id = models.SlugField(primary_key=True, max_length=64)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="iot_devices")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, default=RASPBERRY_PI)
    api_token_hash = models.CharField(max_length=256, blank=True)
    stream_path = models.CharField(max_length=128, blank=True)
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    reported_video_fps = models.FloatField(default=0)
    reported_inference_fps = models.FloatField(default=0)
    model_loaded = models.BooleanField(default=False)
    current_state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "device_id"]

    def __str__(self):
        return f"{self.name} ({self.device_id})"


class Telemetry(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="telemetry")
    topic = models.CharField(max_length=255)
    qos = models.PositiveSmallIntegerField(default=0)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.device.device_id} @ {self.received_at:%Y-%m-%d %H:%M:%S}"


class PairingRequest(models.Model):
    token_hash = models.CharField(max_length=256)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="device_pairings")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    device_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_valid(self):
        return self.consumed_at is None and self.expires_at > timezone.now()


class StreamGrant(models.Model):
    token_hash = models.CharField(max_length=256)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="iot_stream_grants")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="stream_grants")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Alarm(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="alarms")
    alert_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=64, default="person_detected")
    confidence = models.FloatField(default=0)
    bbox = models.JSONField(default=list, blank=True)
    snapshot = models.ImageField(upload_to="alarms/%Y/%m/%d/", blank=True, null=True)
    detected_at = models.DateTimeField(default=timezone.now, db_index=True)
    mqtt_received_at = models.DateTimeField(null=True, blank=True)
    acknowledged = models.BooleanField(default=False)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-detected_at"]
        constraints = [models.UniqueConstraint(fields=["device", "alert_id"], name="unique_device_alert")]

    def __str__(self):
        return f"{self.event_type}: {self.device.device_id} ({self.detected_at})"


class VideoSegment(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="video_segments")
    segment_id = models.CharField(max_length=96)
    file = models.FileField(upload_to="videos/%Y/%m/%d/")
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    duration_seconds = models.PositiveSmallIntegerField(default=5)
    checksum_sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [models.UniqueConstraint(fields=["device", "segment_id"], name="unique_device_segment")]


class DeviceLog(models.Model):
    LEVELS = [("debug", "Debug"), ("info", "Info"), ("warning", "Warning"), ("error", "Error")]
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="logs")
    event_id = models.CharField(max_length=64, blank=True)
    level = models.CharField(max_length=12, choices=LEVELS, default="info")
    category = models.CharField(max_length=64, default="device")
    message = models.CharField(max_length=500)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class RetentionPolicy(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="iot_retention")
    server_video_minutes = models.PositiveIntegerField(default=10)
    alarm_image_days = models.PositiveIntegerField(default=30)
    device_log_days = models.PositiveIntegerField(default=30)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Retention for {self.user}"


class DeviceCommand(models.Model):
    """A short-lived remote command and its signed MQTT acknowledgement."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("published", "Published"),
        ("applied", "Applied"),
        ("failed", "Failed"),
        ("expired", "Expired"),
    ]

    command_id = models.CharField(max_length=32, primary_key=True)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="commands")
    command_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.command_type} -> {self.device.device_id} ({self.status})"
