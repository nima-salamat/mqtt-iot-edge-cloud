from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("devices/<slug:device_id>/", views.device_detail, name="device-detail"),
    path("alarms/<int:alarm_id>/image/", views.alarm_image, name="alarm-image"),
    path("alarms/<int:alarm_id>/acknowledge/", views.acknowledge_alarm, name="acknowledge-alarm"),
    path("videos/<int:segment_id>/", views.segment_file, name="segment-file"),
    path("settings/retention/", views.update_retention, name="update-retention"),
]
