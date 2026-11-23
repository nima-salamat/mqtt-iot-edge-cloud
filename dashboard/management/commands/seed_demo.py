from django.core.management.base import BaseCommand
from dashboard.models import Device


class Command(BaseCommand):
    help = "Register the real Raspberry Pi classroom device."

    def handle(self, *args, **options):
        Device.objects.update_or_create(
            device_id="raspberry-pi-01",
            defaults={
                "name": "Raspberry Pi 4 — دستگاه آزمایشگاه",
                "kind": Device.RASPBERRY_PI,
            },
        )
        self.stdout.write(self.style.SUCCESS("Raspberry Pi device is registered."))
