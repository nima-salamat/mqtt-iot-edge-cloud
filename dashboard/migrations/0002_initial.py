# Generated for the multi-user camera alarm platform.
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="device",
            name="kind",
            field=models.CharField(
                choices=[("raspberry-pi", "Raspberry Pi Camera")],
                default="raspberry-pi",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="device",
            name="owner",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="iot_devices", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="device",
            name="api_token_hash",
            field=models.CharField(blank=True, max_length=256),
        ),
        migrations.AddField(
            model_name="device",
            name="stream_path",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="device",
            name="last_heartbeat",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="device",
            name="reported_video_fps",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="device",
            name="reported_inference_fps",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="device",
            name="model_loaded",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="device",
            name="created_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="device",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.CreateModel(
            name="PairingRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=256)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("device_id", models.CharField(blank=True, max_length=64)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name="device_pairings", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="StreamGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=256)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="stream_grants", to="dashboard.device")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="iot_stream_grants", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Alarm",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("alert_id", models.CharField(max_length=64)),
                ("event_type", models.CharField(default="person_detected", max_length=64)),
                ("confidence", models.FloatField(default=0)),
                ("bbox", models.JSONField(blank=True, default=list)),
                ("snapshot", models.ImageField(blank=True, null=True, upload_to="alarms/%Y/%m/%d/")),
                ("detected_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("mqtt_received_at", models.DateTimeField(blank=True, null=True)),
                ("acknowledged", models.BooleanField(default=False)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="alarms", to="dashboard.device")),
            ],
            options={"ordering": ["-detected_at"]},
        ),
        migrations.AddConstraint(
            model_name="alarm",
            constraint=models.UniqueConstraint(fields=("device", "alert_id"), name="unique_device_alert"),
        ),
        migrations.CreateModel(
            name="VideoSegment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("segment_id", models.CharField(max_length=96)),
                ("file", models.FileField(upload_to="videos/%Y/%m/%d/")),
                ("started_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("duration_seconds", models.PositiveSmallIntegerField(default=5)),
                ("checksum_sha256", models.CharField(blank=True, max_length=64)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="video_segments", to="dashboard.device")),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.AddConstraint(
            model_name="videosegment",
            constraint=models.UniqueConstraint(fields=("device", "segment_id"), name="unique_device_segment"),
        ),
        migrations.CreateModel(
            name="DeviceLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(blank=True, max_length=64)),
                ("level", models.CharField(choices=[("debug", "Debug"), ("info", "Info"),
                                                    ("warning", "Warning"), ("error", "Error")],
                                           default="info", max_length=12)),
                ("category", models.CharField(default="device", max_length=64)),
                ("message", models.CharField(max_length=500)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="logs", to="dashboard.device")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="RetentionPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("server_video_minutes", models.PositiveIntegerField(default=10)),
                ("alarm_image_days", models.PositiveIntegerField(default=30)),
                ("device_log_days", models.PositiveIntegerField(default=30)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="iot_retention", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
