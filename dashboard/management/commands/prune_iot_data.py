import logging
import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from dashboard.models import Alarm, Device, DeviceCommand, DeviceLog, PairingRequest, RetentionPolicy, StreamGrant, VideoSegment

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Expire offline devices, old server recordings, snapshots, logs and temporary grants."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run cleanup once and exit.")
        parser.add_argument("--interval", type=int, default=15, help="Seconds between cleanup runs.")

    def handle(self, *args, **options):
        interval = max(15, options["interval"])
        while True:
            close_old_connections()
            try:
                self.prune()
            except Exception:
                logger.exception("IoT retention cleanup failed")
            finally:
                close_old_connections()
            if options["once"]:
                return
            time.sleep(interval)

    def prune(self):
        now = timezone.now()
        stale_before = now - timedelta(seconds=50)
        stale_count = Device.objects.filter(
            is_online=True, last_heartbeat__isnull=False, last_heartbeat__lt=stale_before
        ).update(is_online=False)
        if stale_count:
            logger.info("Marked %s device(s) offline after missed heartbeats", stale_count)

        policies = {
            policy.user_id: policy
            for policy in RetentionPolicy.objects.all()
        }
        removed_segments = removed_images = removed_logs = 0
        for device in Device.objects.only("device_id", "owner_id").iterator():
            policy = policies.get(device.owner_id)
            video_minutes = policy.server_video_minutes if policy else 10
            image_days = policy.alarm_image_days if policy else 30
            log_days = policy.device_log_days if policy else 30

            video_before = now - timedelta(minutes=video_minutes)
            expired_segments = VideoSegment.objects.filter(device=device, started_at__lt=video_before)
            for segment in expired_segments.iterator():
                if segment.file:
                    segment.file.delete(save=False)
                segment.delete()
                removed_segments += 1

            image_before = now - timedelta(days=image_days)
            expired_alarms = Alarm.objects.filter(
                device=device, detected_at__lt=image_before
            ).exclude(snapshot="")
            for alarm in expired_alarms.iterator():
                if alarm.snapshot:
                    alarm.snapshot.delete(save=False)
                    alarm.save(update_fields=["snapshot"])
                removed_images += 1

            log_before = now - timedelta(days=log_days)
            removed_logs += DeviceLog.objects.filter(device=device, created_at__lt=log_before).delete()[0]

        PairingRequest.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
        StreamGrant.objects.filter(expires_at__lt=now - timedelta(hours=1)).delete()
        DeviceCommand.objects.filter(created_at__lt=now - timedelta(days=7)).delete()
        logger.info(
            "Retention pass complete: segments=%s snapshots=%s logs=%s",
            removed_segments, removed_images, removed_logs,
        )
