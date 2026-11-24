from django.contrib import admin
from django.utils import timezone
from .models import Alarm, Device, DeviceCommand, DeviceLog, PairingRequest, RetentionPolicy, StreamGrant, Telemetry, VideoSegment


class AlarmInline(admin.TabularInline):
    model = Alarm
    extra = 0
    fields = ("alert_id", "event_type", "confidence", "detected_at", "acknowledged")
    readonly_fields = fields
    can_delete = False
    max_num = 0


@admin.action(description="لغو اعتبارنامه دستگاه‌های انتخاب‌شده")
def revoke_device_credentials(modeladmin, request, queryset):
    from .models import StreamGrant
    device_ids = list(queryset.values_list("device_id", flat=True))
    queryset.update(api_token_hash="", is_online=False)
    StreamGrant.objects.filter(device_id__in=device_ids, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "name", "owner", "kind", "is_online", "last_heartbeat",
                    "reported_video_fps", "reported_inference_fps", "model_loaded")
    list_filter = ("kind", "is_online", "model_loaded")
    search_fields = ("device_id", "name", "owner__username", "owner__email")
    readonly_fields = (
        "stream_path", "is_online", "last_seen", "last_heartbeat",
        "reported_video_fps", "reported_inference_fps", "model_loaded",
        "current_state", "created_at", "updated_at",
    )
    exclude = ("api_token_hash",)
    autocomplete_fields = ("owner",)
    inlines = (AlarmInline,)
    actions = (revoke_device_credentials,)


@admin.register(Alarm)
class AlarmAdmin(admin.ModelAdmin):
    list_display = ("alert_id", "device", "event_type", "confidence", "detected_at", "acknowledged", "mqtt_received_at")
    list_filter = ("event_type", "acknowledged", "device")
    search_fields = ("alert_id", "device__device_id", "device__owner__username")
    readonly_fields = ("created_at", "mqtt_received_at")
    date_hierarchy = "detected_at"


@admin.register(VideoSegment)
class VideoSegmentAdmin(admin.ModelAdmin):
    list_display = ("segment_id", "device", "started_at", "duration_seconds", "size_bytes", "uploaded_at")
    list_filter = ("device",)
    search_fields = ("segment_id", "device__device_id")
    readonly_fields = ("uploaded_at",)


@admin.register(DeviceLog)
class DeviceLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "device", "level", "category", "message")
    list_filter = ("level", "category", "device")
    search_fields = ("message", "device__device_id", "device__owner__username")
    readonly_fields = ("created_at",)


@admin.register(Telemetry)
class TelemetryAdmin(admin.ModelAdmin):
    list_display = ("device", "topic", "qos", "received_at")
    list_filter = ("device", "qos")
    search_fields = ("topic", "device__device_id")
    readonly_fields = ("received_at",)


@admin.register(PairingRequest)
class PairingRequestAdmin(admin.ModelAdmin):
    list_display = ("owner", "device_id", "created_at", "expires_at", "consumed_at")
    list_filter = ("created_at", "expires_at", "consumed_at")
    search_fields = ("owner__username", "device_id")
    readonly_fields = ("created_at", "expires_at", "consumed_at")
    exclude = ("token_hash",)


@admin.register(RetentionPolicy)
class RetentionPolicyAdmin(admin.ModelAdmin):
    list_display = ("user", "server_video_minutes", "alarm_image_days", "device_log_days", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(StreamGrant)
class StreamGrantAdmin(admin.ModelAdmin):
    list_display = ("user", "device", "created_at", "expires_at", "revoked_at")
    readonly_fields = ("created_at", "expires_at")
    exclude = ("token_hash",)
    search_fields = ("user__username", "device__device_id")


@admin.register(DeviceCommand)
class DeviceCommandAdmin(admin.ModelAdmin):
    list_display = ("command_id", "device", "command_type", "status", "created_at", "expires_at", "acknowledged_at")
    list_filter = ("command_type", "status", "created_at")
    search_fields = ("command_id", "device__device_id", "device__owner__username")
    readonly_fields = (
        "command_id", "device", "command_type", "payload", "status",
        "result", "created_at", "expires_at", "published_at", "acknowledged_at",
    )
    date_hierarchy = "created_at"
