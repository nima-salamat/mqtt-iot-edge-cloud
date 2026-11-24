from __future__ import annotations
from functools import wraps
from django.contrib.auth.hashers import check_password
from django.http import JsonResponse
from django.utils import timezone
from .models import Device, DeviceCommand, StreamGrant


def revoke_device_access(device):
    """Revoke the current device token and every short-lived stream grant."""
    now = timezone.now()
    device.api_token_hash = ""
    device.stream_path = ""
    device.is_online = False
    device.model_loaded = False
    state = device.current_state if isinstance(device.current_state, dict) else {}
    state.update({
        "mqtt_connected": False,
        "mqtt_command_subscription_ready": False,
        "recording_supported": False,
        "recording_enabled": False,
        "rtsp_connected": False,
        "camera_ready": False,
        "tracking_active": False,
        "tracking_seconds": 0.0,
        "connection_state": "disconnected",
    })
    device.current_state = state
    device.save(update_fields=[
        "api_token_hash", "stream_path", "is_online", "model_loaded",
        "current_state", "updated_at",
    ])
    StreamGrant.objects.filter(device=device, revoked_at__isnull=True).update(revoked_at=now)
    # Commands queued for the old signing key cannot be executed by a future
    # pairing session. Mark them terminal so polling clients do not wait until
    # expiry and prevent delayed ACKs from changing the new device state.
    DeviceCommand.objects.filter(
        device=device, status__in=["pending", "published"]
    ).update(status="failed", result={"error": "device_pairing_revoked"})


def authenticate_device(request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    device_id = request.headers.get("X-Device-ID", "").strip()
    if scheme.lower() != "bearer" or not token or not device_id:
        return None
    try:
        device = Device.objects.get(device_id=device_id)
    except Device.DoesNotExist:
        return None
    if device.owner_id is None or not device.owner.is_active:
        return None
    if not device.api_token_hash or not check_password(token, device.api_token_hash):
        return None
    return device


def device_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        device = authenticate_device(request)
        if device is None:
            return JsonResponse({"error": "invalid_device_credentials"}, status=401)
        request.iot_device = device
        return view(request, *args, **kwargs)
    return wrapped
