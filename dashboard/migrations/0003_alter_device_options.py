from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="device",
            options={"ordering": ["name", "device_id"]},
        ),
    ]
