# Generated for the MQTT classroom demonstration.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Device",
            fields=[
                ("device_id", models.SlugField(max_length=64, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120)),
                ("kind", models.CharField(choices=[("raspberry-pi", "Raspberry Pi 4")], max_length=16)),
                ("is_online", models.BooleanField(default=False)),
                ("last_seen", models.DateTimeField(blank=True, null=True)),
                ("current_state", models.JSONField(blank=True, default=dict)),
            ],
            options={"ordering": ["device_id"]},
        ),
        migrations.CreateModel(
            name="Telemetry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("topic", models.CharField(max_length=255)),
                ("qos", models.PositiveSmallIntegerField(default=0)),
                ("payload", models.JSONField(default=dict)),
                ("received_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="telemetry", to="dashboard.device")),
            ],
            options={"ordering": ["-received_at"]},
        ),
    ]
