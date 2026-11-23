import json
import logging
from datetime import timezone as dt_timezone

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Device, Telemetry
from .mqtt import publish_json

logger = logging.getLogger(__name__)
DEVICE_ID = "raspberry-pi-01"


@login_required
def dashboard(request):
    return render(request, "dashboard/index.html")


@require_GET
def health(request):
    return JsonResponse({"status": "ok", "service": "django-cloud-dashboard"})


def _iso(value):
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).isoformat()


@login_required
@require_GET
def api_state(request):
    devices = []
    for device in Device.objects.all():
        devices.append(
            {
                "device_id": device.device_id,
                "name": device.name,
                "kind": device.kind,
                "is_online": device.is_online,
                "last_seen": _iso(device.last_seen),
                "state": device.current_state or {},
            }
        )

    recent = [
        {
            "device_id": item.device_id,
            "name": item.device.name,
            "topic": item.topic,
            "payload": item.payload,
            "received_at": _iso(item.received_at),
            "qos": item.qos,
        }
        for item in Telemetry.objects.select_related("device")[:25]
    ]
    return JsonResponse(
        {
            "devices": devices,
            "recent_messages": recent,
            "total_messages": Telemetry.objects.count(),
            "server_time": timezone.now().isoformat(),
        }
    )


@login_required
@require_POST
def api_command(request, device_id):
    if device_id != DEVICE_ID:
        return JsonResponse({"error": "این دستگاه در پروژه ثبت نشده است."}, status=404)

    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"error": "بدنهٔ درخواست باید JSON معتبر باشد."}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "بدنهٔ JSON باید یک شیء باشد."}, status=400)

    command = body.get("command")
    if command == "set_output" and body.get("state") in {"on", "off"}:
        payload = {"command": "set_output", "state": body["state"]}
    else:
        return JsonResponse({"error": "فرمان مجاز: set_output با state برابر on یا off است."}, status=400)

    topic = f"{settings.MQTT_TOPIC_PREFIX}/devices/{DEVICE_ID}/command"
    try:
        publish_json(topic, payload, qos=1)
    except Exception:
        logger.exception("Could not publish command to the Raspberry Pi")
        return JsonResponse(
            {"error": "ارسال فرمان به Broker ناموفق بود؛ اتصال MQTT سرور را بررسی کنید."},
            status=503,
        )

    return JsonResponse({"status": "published", "topic": topic, "payload": payload, "qos": 1})
