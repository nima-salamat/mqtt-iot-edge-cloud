from __future__ import annotations
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from datetime import timedelta
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Alarm, Device, DeviceCommand, DeviceLog, RetentionPolicy, VideoSegment
from .device_auth import revoke_device_access
from .services import cleanup_iot_data
from .mqtt_security import device_mqtt_signing_key, sign_mqtt_payload
from .mqtt_commands import publish_mqtt_command

logger = logging.getLogger(__name__)


def _safe_state_number(value, default=0.0, minimum=0.0, maximum=1_000_000.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number) or number < minimum or number > maximum:
        return default
    return number


def _safe_state_count(value, default=0, maximum=1_000_000):
    number = _safe_state_number(value, default=float(default), minimum=0, maximum=maximum)
    return int(number)


def _visible_devices(user):
    return Device.objects.select_related("owner") if user.is_staff else Device.objects.filter(owner=user)


@login_required
def home(request):
    devices = _visible_devices(request.user).order_by("name", "device_id")
    online_count = devices.filter(
        api_token_hash__gt="",
        last_heartbeat__gte=timezone.now() - timedelta(seconds=50)
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
    logs = device.logs.all()[:50]
    return render(request, "dashboard/device_detail.html", {
        "device": device, "device_online": device.is_online_now,
        "alarms": alarms, "alarm_cursor": alarm_cursor, "logs": logs,
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
    try:
        pending_video_segments = max(
            0, int(state.get("pending_video_segments", 0) or 0)
        )
    except (TypeError, ValueError):
        pending_video_segments = 0
    latest_segment = device.video_segments.order_by("-uploaded_at").only("uploaded_at").first()
    latest_uploaded_at = (
        timezone.localtime(latest_segment.uploaded_at).isoformat()
        if latest_segment else str(state.get("last_video_upload_at") or "")
    )
    response = JsonResponse({
        "device_id": device.device_id,
        "system": state.get("system", {}) if isinstance(state.get("system"), dict) else {},
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
        "pending_video_segments": pending_video_segments,
        "last_video_upload_at": latest_uploaded_at,
        "last_video_upload_error": str(state.get("last_video_upload_error", ""))[:500],
    })
    response["Cache-Control"] = "private, no-store"
    return response


def _ffprobe_h264_codec(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,profile,level",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("ویدئوی ذخیره‌شده قابل شناسایی نیست.")
    try:
        stream = (json.loads(result.stdout).get("streams") or [])[0]
    except (ValueError, IndexError, AttributeError, TypeError):
        raise ValueError("اطلاعات کدک ویدئو قابل خواندن نیست.")
    if stream.get("codec_name") != "h264":
        raise ValueError("آرشیو فعلی فقط برای ویدئوی H.264 پشتیبانی می‌شود.")
    profile = str(stream.get("profile") or "").lower()
    profile_prefix = {
        "constrained baseline": "42e0",
        "baseline": "4200",
        "main": "4d00",
        "high": "6400",
        "high 10": "6e00",
        "high 4:2:2": "7a00",
        "high 4:4:4 predictive": "f400",
    }.get(profile)
    if not profile_prefix:
        raise ValueError("پروفایل H.264 این آرشیو پشتیبانی نمی‌شود.")
    try:
        level = max(10, min(62, int(stream.get("level") or 31)))
    except (TypeError, ValueError):
        level = 31
    return f"avc1.{profile_prefix}{level:02x}"


@login_required
def device_video_chunk(request, device_id):
    """Remux adjacent stored MP4 files into one short fragmented-MP4 MSE chunk."""
    device = get_object_or_404(_visible_devices(request.user), device_id=device_id)
    try:
        start_id = int(request.GET.get("start_id", ""))
        requested_seconds = int(request.GET.get("seconds", "12"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "شناسه شروع یا مدت قطعه معتبر نیست."}, status=400)
    if start_id <= 0 or not 10 <= requested_seconds <= 30:
        return JsonResponse({"error": "مدت دریافت هر قطعه باید بین ۱۰ تا ۳۰ ثانیه باشد."}, status=400)

    anchor = get_object_or_404(
        VideoSegment.objects.filter(device=device).exclude(file=""),
        pk=start_id,
    )
    candidates = list(
        VideoSegment.objects.filter(device=device)
        .exclude(file="")
        .filter(
            Q(started_at__gt=anchor.started_at)
            | Q(started_at=anchor.started_at, pk__gte=anchor.pk)
        )
        .order_by("started_at", "pk")[:24]
    )
    selected = []
    source_bytes = 0
    selected_seconds = 0
    maximum_chunk_bytes = 48 * 1024 * 1024
    paths = []
    for segment in candidates:
        try:
            file_size = int(segment.size_bytes or segment.file.size or 0)
            file_path = Path(segment.file.path)
        except (AttributeError, NotImplementedError, ValueError, OSError):
            return JsonResponse({
                "error": "فضای ذخیره‌سازی ویدئو امکان خواندن مستقیم برای ساخت قطعه پیوسته را نمی‌دهد."
            }, status=503)
        if not file_path.is_file():
            continue
        if selected and source_bytes + file_size > maximum_chunk_bytes:
            break
        if not selected and file_size > maximum_chunk_bytes:
            return JsonResponse({
                "error": "حجم این قطعه از سقف پردازش سرور بیشتر است."
            }, status=413)
        selected.append(segment)
        paths.append(file_path)
        source_bytes += file_size
        selected_seconds += max(1, min(60, int(segment.duration_seconds or 5)))
        if selected_seconds >= requested_seconds:
            break

    if not selected:
        return JsonResponse({"error": "برای این بازه فایل ویدئویی ذخیره‌شده‌ای پیدا نشد."}, status=404)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return JsonResponse({
            "error": "پردازشگر FFmpeg روی سرور نصب نیست؛ ایمیج api را با نسخه جدید بازسازی کن."
        }, status=503)

    try:
        codec = _ffprobe_h264_codec(paths[0])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=415)

    temporary_directory = tempfile.TemporaryDirectory(prefix="iot-video-chunk-")
    temp_root = Path(temporary_directory.name)
    concat_path = temp_root / "inputs.ffconcat"
    output_path = temp_root / "chunk.mp4"

    def ffconcat_quote(value):
        return "'" + str(value).replace("\\", "\\\\").replace("'", "'\\''").replace("\n", "") + "'"

    try:
        with concat_path.open("w", encoding="utf-8") as manifest:
            manifest.write("ffconcat version 1.0\n")
            for path in paths:
                manifest.write("file " + ffconcat_quote(path) + "\n")

        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts", "-f", "concat", "-safe", "0",
            "-i", str(concat_path), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-c:v", "copy", "-avoid_negative_ts", "make_zero",
            "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
            "-frag_duration", "2000000", "-f", "mp4", str(output_path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size < 512:
            detail = (result.stderr or "FFmpeg نتوانست قطعه پیوسته بسازد.").strip()[-700:]
            logger.warning("Video chunk remux failed for %s: %s", device_id, detail)
            temporary_directory.cleanup()
            return JsonResponse({"error": "ساخت قطعه ویدئویی ناموفق بود: " + detail}, status=422)

        first = selected[0]
        total_duration = sum(max(1, min(60, int(item.duration_seconds or 5))) for item in selected)
        handle = output_path.open("rb")
        response = FileResponse(handle, content_type="video/mp4")
        response["Content-Length"] = str(output_path.stat().st_size)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Video-Codec"] = codec
        response["X-Video-Start-Ms"] = str(int(first.started_at.timestamp() * 1000))
        response["X-Video-Duration"] = str(total_duration)
        response["X-Video-Segment-Count"] = str(len(selected))
        response["X-Video-Segment-Ids"] = ",".join(str(item.pk) for item in selected)
        response._resource_closers.append(temporary_directory.cleanup)
        return response
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        temporary_directory.cleanup()
        logger.exception("Could not build a video archive chunk for %s", device_id)
        return JsonResponse({"error": "پردازش قطعه ویدئو با خطا روبه‌رو شد: " + str(exc)[:300]}, status=500)


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
    payload_devices = []
    for device in devices:
        state = device.current_state if isinstance(device.current_state, dict) else {}
        system = state.get("system")
        payload_devices.append({
            "device_id": device.device_id,
            "name": device.name,
            "is_online": device.is_online_now,
            "is_paired": bool(device.api_token_hash),
            "last_heartbeat": device.last_heartbeat.isoformat() if device.last_heartbeat else None,
            "video_fps": _safe_state_number(device.reported_video_fps, maximum=120),
            "inference_fps": _safe_state_number(device.reported_inference_fps, maximum=60),
            "model_loaded": bool(device.model_loaded),
            "mqtt_connected": bool(state.get("mqtt_connected", False)),
            "camera_ready": bool(state.get("camera_ready", False)),
            "rtsp_connected": bool(state.get("rtsp_connected", False)),
            "tracking_active": bool(state.get("tracking_active", False)),
            "tracking_seconds": round(_safe_state_number(
                state.get("tracking_seconds"), maximum=86400
            ), 1),
            "pending_alerts": _safe_state_count(state.get("pending_alerts")),
            "pending_video_segments": _safe_state_count(state.get("pending_video_segments")),
            "system": system if isinstance(system, dict) else {},
        })
    return JsonResponse({"devices": payload_devices})


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
            "confidence": round(_safe_state_number(alarm.confidence, maximum=1), 3),
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


@login_required
@require_POST
def send_device_command(request, device_id):
    """Publish the deliberately small, owner-scoped remote command surface."""
    device = get_object_or_404(_visible_devices(request.user), device_id=device_id)
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload, dict):
            raise ValueError
        command_type = str(payload.get("command_type", ""))
        command_payload = payload.get("payload")
        if (
            command_type != "recording.set"
            or not isinstance(command_payload, dict)
            or set(command_payload) != {"enabled"}
            or type(command_payload.get("enabled")) is not bool
        ):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "فرمان ارسالی معتبر نیست."}, status=400)

    now = timezone.now()
    if not device.api_token_hash:
        return JsonResponse({"error": "دوربین Pair نشده است."}, status=409)
    if not device.is_online_now:
        return JsonResponse({"error": "دوربین آنلاین نیست؛ آخرین Heartbeat قدیمی است."}, status=409)
    state = device.current_state if isinstance(device.current_state, dict) else {}
    if not state.get("mqtt_connected"):
        return JsonResponse({"error": "کانال MQTT دوربین متصل نیست."}, status=503)

    command_id = uuid.uuid4().hex
    expires_at = now + timedelta(seconds=60)
    command = DeviceCommand.objects.create(
        command_id=command_id,
        device=device,
        command_type=command_type,
        payload=command_payload,
        status="pending",
        expires_at=expires_at,
    )
    try:
        envelope = sign_mqtt_payload({
            "device_id": device.device_id,
            "command_id": command_id,
            "command_type": command_type,
            "payload": command_payload,
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }, device_mqtt_signing_key(device))
        publish_mqtt_command(device.device_id, envelope)
    except Exception as exc:
        logger.warning("Could not publish remote command %s for %s: %s", command_id, device_id, exc)
        DeviceCommand.objects.filter(
            command_id=command_id, status="pending"
        ).update(status="failed", result={"error": "mqtt_publish_failed"})
        return JsonResponse({"error": "ارسال فرمان از طریق MQTT ناموفق بود."}, status=503)

    # The device may execute and ACK immediately after broker delivery. Only
    # transition pending -> published so a fast ACK cannot be overwritten.
    DeviceCommand.objects.filter(
        command_id=command_id, status="pending"
    ).update(status="published", published_at=timezone.now())
    return JsonResponse({
        "status": "published",
        "command_id": command_id,
        "expires_at": expires_at.isoformat(),
    }, status=202)


@login_required
def device_command_status(request, device_id, command_id):
    device = get_object_or_404(_visible_devices(request.user), device_id=device_id)
    command = get_object_or_404(
        DeviceCommand,
        device=device,
        command_id=command_id,
    )
    now = timezone.now()
    if command.status in {"pending", "published"} and command.expires_at <= now:
        command.status = "expired"
        command.save(update_fields=["status"])
    return JsonResponse({
        "command_id": command.command_id,
        "command_type": command.command_type,
        "status": command.status,
        "result": command.result,
        "created_at": command.created_at.isoformat(),
        "expires_at": command.expires_at.isoformat(),
        "acknowledged_at": command.acknowledged_at.isoformat() if command.acknowledged_at else None,
    })
