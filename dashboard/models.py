from django.db import models


class Device(models.Model):
    RASPBERRY_PI = "raspberry-pi"
    KIND_CHOICES = [(RASPBERRY_PI, "Raspberry Pi 4")]

    device_id = models.SlugField(primary_key=True, max_length=64)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    current_state = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["device_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.device_id})"


class Telemetry(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="telemetry")
    topic = models.CharField(max_length=255)
    qos = models.PositiveSmallIntegerField(default=0)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.device.device_id} @ {self.received_at:%Y-%m-%d %H:%M:%S}"
