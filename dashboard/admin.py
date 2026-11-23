from django.contrib import admin
from .models import Device, Telemetry


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "name", "kind", "is_online", "last_seen")
    list_filter = ("kind", "is_online")
    search_fields = ("device_id", "name")


@admin.register(Telemetry)
class TelemetryAdmin(admin.ModelAdmin):
    list_display = ("device", "topic", "qos", "received_at")
    list_filter = ("device", "qos")
    readonly_fields = ("received_at",)
