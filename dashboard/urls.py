from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("api/state/", views.api_state, name="api_state"),
    path("api/devices/<slug:device_id>/command/", views.api_command, name="api_command"),
]
