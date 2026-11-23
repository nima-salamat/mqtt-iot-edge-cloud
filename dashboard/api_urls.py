from django.urls import path
from . import api, views

urlpatterns = [
    path("dashboard/overview/", views.dashboard_overview, name="dashboard-overview"),
    path("dashboard/alerts/", views.dashboard_alerts, name="dashboard-alerts"),
    path("pairings/", api.create_pairing, name="create-pairing"),
    path("pairings/<int:pairing_id>/", api.pairing_status, name="pairing-status"),
    path("device/pair/", api.pair_device, name="pair-device"),
    path("device/heartbeat/", api.heartbeat, name="device-heartbeat"),
    path("device/disconnect/", api.disconnect_device, name="device-disconnect"),
    path("device/alarms/", api.upload_alarm, name="device-upload-alarm"),
    path("device/segments/", api.upload_segment, name="device-upload-segment"),
    path("device/logs/", api.device_log, name="device-log"),
    path("devices/<slug:device_id>/stream-grant/", api.create_stream_grant, name="stream-grant"),
    path("mediamtx/auth/", api.mediamtx_auth, name="mediamtx-auth"),
]
