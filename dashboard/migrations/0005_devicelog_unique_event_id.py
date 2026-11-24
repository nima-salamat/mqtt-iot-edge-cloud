from django.db import migrations, models
from django.db.models import Count


def remove_duplicate_device_log_events(apps, schema_editor):
    DeviceLog = apps.get_model("dashboard", "DeviceLog")
    duplicates = (
        DeviceLog.objects.filter(event_id__gt="")
        .values("device_id", "event_id")
        .annotate(total=Count("pk"))
        .filter(total__gt=1)
    )
    # Materialize groups before deleting rows from the same table. This
    # avoids changing the underlying cursor while it is being iterated,
    # including on SQLite deployments.
    for duplicate in list(duplicates):
        ids = list(
            DeviceLog.objects.filter(
                device_id=duplicate["device_id"],
                event_id=duplicate["event_id"],
            ).order_by("created_at", "pk").values_list("pk", flat=True)
        )
        if len(ids) > 1:
            DeviceLog.objects.filter(pk__in=ids[1:]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_devicecommand"),
    ]

    operations = [
        migrations.RunPython(
            remove_duplicate_device_log_events,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="devicelog",
            constraint=models.UniqueConstraint(
                fields=("device", "event_id"),
                condition=models.Q(event_id__gt=""),
                name="unique_device_log_event",
            ),
        ),
    ]
