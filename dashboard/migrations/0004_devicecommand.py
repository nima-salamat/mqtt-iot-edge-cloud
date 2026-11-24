from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_alter_device_options"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviceCommand",
            fields=[
                ("command_id", models.CharField(max_length=32, primary_key=True, serialize=False)),
                ("command_type", models.CharField(max_length=64)),
                ("payload", models.JSONField(default=dict)),
                ("status", models.CharField(
                    choices=[
                        ("pending", "Pending"),
                        ("published", "Published"),
                        ("applied", "Applied"),
                        ("failed", "Failed"),
                        ("expired", "Expired"),
                    ],
                    default="pending",
                    max_length=12,
                )),
                ("result", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("device", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="commands",
                    to="dashboard.device",
                )),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
