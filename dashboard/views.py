from __future__ import annotations
import json
import re
from datetime import timedelta
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Alarm, Device, DeviceLog, RetentionPolicy, VideoSegment
from .device_auth import revoke_device_access
from .services import cleanup_iot_data


def _visible_devices(user):
    return Device.objects.select_related("owner") if user.is_staff else Device.objects.filter(owner=user)


@login_required
def home(request):
    devices = _visible_devices(request.user).order_by("name", "device_id")
    online_count = devices.filter(
        is_online=True, last_heartbeat__gte=timezone.now() - timedelta(seconds=50)
    ).count()
    alarm_queryset = Alarm.objects.filter(device__in=devices)
    recent_alarms = alarm_queryset.select_related("device")[:12]
    alarm_cursor = alarm_queryset.aggregate(max_id=Max("pk"))["max_id"] or 0
    policy, _ = RetentionPolicy.objects.get_or_create(user=request.user)
    return render(request, "dashboard/home.html", {
        "devices": devices,
        "online_count": online_count,
        "recent_alarms": recent_alarms,
        "alarm_cursor": alarm_cursor,
        "retention": policy,
        "is_admin": request.user.is_staff,
    })


@login_required
@require_POST
def disconnect_device(request, device_id):
    device = get_object_or_404(_visible_devices(request.user), device_id=device_id)
    was_paired = bool(device.api_token_hash)
    # Revoke any orphaned stream grants as well, even if the device token
    # had already been cleared by an earlier partial disconnect.
    revoke_device_access(device)
    return JsonResponse({
        "status": "disconnected" if was_paired else "already_disconnected",
        "device_id": device.device_id,
    })


@login_required
def device_detail(request, device_id):
    device = get_object_or_404(_visible_devices(request.user), device_id=device_id)
    alarm_queryset = device.alarms.all()
    alarms = alarm_queryset[:50]
    alarm_cursor = alarm_queryset.aggregate(max_id=Max("pk"))["max_id"] or 0
    # Retention tops out at 60 minutes and segments can be as short as 2 seconds.
    # 2,000 rows cover that rolling archive without truncating the configured window.
    segments = device.video_segments.all()[:2000]
    logs = device.logs.all()[:50]
    return render(request, "dashboard/device_detail.html", {
        "device": device, "alarms": alarms, "alarm_cursor": alarm_cursor,
        "segments": segments, "logs": logs,
    })


@login_required
def device_videos(request, device_id):
    """Return a fresh, owner-scoped video archive snapshot for live page updates."""
    device = get_object_or_404(_visible_devices(request.user), device_id=device_id)
    recent = list(
        device.video_segments.exclude(file="")
        .order_by("-started_at", "-pk")[:2000]
    )
    segments = list(reversed(recent))
    state = device.current_state if isinstance(device.current_state, dict) else {}
    latest_segment = device.video_segments.order_by("-uploaded_at").only("uploaded_at").first()
    latest_uploaded_at = (
        timezone.localtime(latest_segment.uploaded_at).isoformat()
        if latest_segment else str(state.get("last_video_upload_at") or "")
    )
    response = JsonResponse({
        "device_id": device.device_id,
        "segments": [
            {
                "id": segment.pk,
                "segment_id": segment.segment_id,
                "started_at": timezone.localtime(segment.started_at).isoformat(),
                "duration_seconds": segment.duration_seconds,
                "size_bytes": segment.size_bytes or (segment.file.size if segment.file else 0),
                "url": reverse("dashboard:segment-file", args=[segment.pk]),
            }
            for segment in segments
        ],
        "recording_enabled": (
            state.get("recording_enabled")
            if isinstance(state.get("recording_enabled"), bool) else None
        ),
        "rtsp_connected": bool(state.get("rtsp_connected", False)),
        "pending_video_segments": max(0, int(state.get("pending_video_segments", 0) or 0)),
        "last_video_upload_at": latest_uploaded_at,
        "last_video_upload_error": str(state.get("last_video_upload_error", ""))[:500],
    })
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def alarm_image(request, alarm_id):
    alarm = get_object_or_404(Alarm.objects.select_related("device"), pk=alarm_id)
    if not (request.user.is_staff or alarm.device.owner_id == request.user.id):
        raise Http404
    if not alarm.snapshot:
        raise Http404
    response = FileResponse(alarm.snapshot.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def segment_file(request, segment_id):
    segment = get_object_or_404(VideoSegment.objects.select_related("device"), pk=segment_id)
    if not (request.user.is_staff or segment.device.owner_id == request.user.id):
        raise Http404
    if not segment.file:
        raise Http404
    file_handle = segment.file.open("rb")
    size = int(segment.file.size or 0)
    range_header = request.headers.get("Range", "")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match or size <= 0:
            file_handle.close()
            response = JsonResponse({"error": "invalid_range"}, status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response

        first, last = match.groups()
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        elif last:
            suffix_length = int(last)
            start = max(0, size - suffix_length)
            end = size - 1
        else:
            start, end = 0, size - 1

        if start >= size or end < start:
            file_handle.close()
            response = JsonResponse({"error": "range_not_satisfiable"}, status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response

        end = min(end, size - 1)
        remaining = end - start + 1
        file_handle.seek(start)

        def file_chunks():
            nonlocal remaining
            try:
                while remaining > 0:
                    chunk = file_handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                file_handle.close()

        response = StreamingHttpResponse(
            file_chunks(), status=206, content_type="video/mp4"
        )
        response["Content-Length"] = str(end - start + 1)
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    else:
        response = FileResponse(file_handle, content_type="video/mp4")
        response["Content-Length"] = str(size)
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def cleanup_data(request):
    """Remove stored evidence belonging only to the signed-in account."""
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload, dict):
            raise ValueError
        kind = str(payload.get("kind", ""))
        if kind not in {"alarms", "videos", "logs", "all"}:
            raise ValueError
        device_id = str(payload.get("device_id", "")).strip()
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "درخواست پاک‌سازی معتبر نیست."}, status=400)

    devices = _visible_devices(request.user)
    if device_id:
        devices = devices.filter(device_id=device_id)
        if not devices.exists():
            raise Http404

    try:
        result = cleanup_iot_data(kind, devices)
    except OSError:
        return JsonResponse({"error": "حذف فایل‌های ذخیره‌شده کامل نشد؛ لاگ سرور را بررسی کن."}, status=500)
    return JsonResponse({"status": "cleaned", "kind": kind, **result})


@login_required
@require_POST
def update_retention(request):
    try:
        values = json.loads(request.body or b"{}")
        if not isinstance(values, dict):
            raise ValueError
        video = int(values.get("server_video_minutes", 10))
        images = int(values.get("alarm_image_days", 30))
        logs = int(values.get("device_log_days", 30))
        if not (1 <= video <= 60 and 1 <= images <= 365 and 1 <= logs <= 365):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "مدت نگهداری خارج از محدوده مجاز است."}, status=400)
    policy, _ = RetentionPolicy.objects.get_or_create(user=request.user)
    policy.server_video_minutes = video
    policy.alarm_image_days = images
    policy.device_log_days = logs
    policy.save(update_fields=["server_video_minutes", "alarm_image_days", "device_log_days", "updated_at"])
    return JsonResponse({"status": "saved"})


@login_required
@require_POST
def acknowledge_alarm(request, alarm_id):
    alarm = get_object_or_404(Alarm.objects.select_related("device"), pk=alarm_id)
    if not (request.user.is_staff or alarm.device.owner_id == request.user.id):
        raise Http404
    alarm.acknowledged = True
    alarm.save(update_fields=["acknowledged"])
    return JsonResponse({"status": "acknowledged"})


@login_required
def dashboard_overview(request):
    devices = list(_visible_devices(request.user).order_by("name", "device_id"))
    return JsonResponse({
        "devices": [
            {
                "device_id": device.device_id,
                "name": device.name,
                "is_online": bool(
                    device.api_token_hash and device.is_online and device.last_heartbeat
                    and device.last_heartbeat >= timezone.now() - timedelta(seconds=50)
                ),
                "is_paired": bool(device.api_token_hash),
                "last_heartbeat": device.last_heartbeat.isoformat() if device.last_heartbeat else None,
                "video_fps": round(device.reported_video_fps, 1),
                "inference_fps": round(device.reported_inference_fps, 1),
                "model_loaded": device.model_loaded,
                "camera_ready": bool(device.current_state.get("camera_ready", False)),
                "rtsp_connected": bool(device.current_state.get("rtsp_connected", False)),
                "tracking_active": bool(device.current_state.get("tracking_active", False)),
                "tracking_seconds": round(float(device.current_state.get("tracking_seconds", 0) or 0), 1),
                "pending_alerts": int(device.current_state.get("pending_alerts", 0) or 0),
                "pending_video_segments": int(device.current_state.get("pending_video_segments", 0) or 0),
            }
            for device in devices
        ],
    })


@login_required
def dashboard_alerts(request):
    """Return only newly-created alerts for the signed-in user's devices."""
    try:
        after = int(request.GET.get("after", "0"))
        if after < 0:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid_alert_cursor"}, status=400)

    visible_devices = _visible_devices(request.user)
    device_id = request.GET.get("device_id", "").strip()
    if device_id:
        get_object_or_404(visible_devices, device_id=device_id)
        queryset = Alarm.objects.filter(device_id=device_id)
    else:
        queryset = Alarm.objects.filter(device__in=visible_devices)

    batch = list(
        queryset.filter(pk__gt=after)
        .select_related("device")
        .order_by("pk")[:51]
    )
    has_more = len(batch) > 50
    batch = batch[:50]
    alerts = []
    for alarm in batch:
        alerts.append({
            "id": alarm.pk,
            "device_id": alarm.device_id,
            "device_name": alarm.device.name,
            "confidence": round(alarm.confidence, 3),
            "detected_at": alarm.detected_at.isoformat(),
            "acknowledged": alarm.acknowledged,
            "bbox": alarm.bbox if isinstance(alarm.bbox, list) else [],
            "has_snapshot": bool(alarm.snapshot),
            "image_url": reverse("dashboard:alarm-image", args=[alarm.pk]) if alarm.snapshot else None,
            "device_url": reverse("dashboard:device-detail", args=[alarm.device_id]),
        })

    return JsonResponse({
        "alerts": alerts,
        "next_cursor": alerts[-1]["id"] if alerts else after,
        "has_more": has_more,
    })


def health(request):
    return JsonResponse({"status": "ok", "service": "iot-alarm-api"})
