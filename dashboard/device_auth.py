from __future__ import annotations
from functools import wraps
from django.contrib.auth.hashers import check_password
from django.http import JsonResponse
from .models import Device


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
