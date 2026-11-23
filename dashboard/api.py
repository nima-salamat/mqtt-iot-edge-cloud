from __future__ import annotations
import base64
import hashlib
import io
import json
import re
import secrets
from datetime import datetime, timedelta
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from PIL import Image
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import qrcode
from qrcode.image.svg import SvgPathImage
from .device_auth import device_required
from .models import Alarm, Device, DeviceLog, PairingRequest, StreamGrant, VideoSegment

DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$")
SEGMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,96}$")


def _json_body(request):
    try:
        value = json.loads(request.body or b"{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


@login_required
@require_POST
def create_pairing(request):
    now = timezone.now()
    PairingRequest.objects.filter(owner=request.user, consumed_at__isnull=True, expires_at__gt=now).update(consumed_at=now)
    raw_code = secrets.token_urlsafe(32)
    # Pairing secrets are cryptographically random (256 bits); a SHA-256 digest
    # is safe for lookup and avoids expensive password hashing on the camera API.
    pairing = PairingRequest.objects.create(
        owner=request.user,
        token_hash=hashlib.sha256(raw_code.encode("utf-8")).hexdigest(),
        expires_at=now + timedelta(minutes=2),
    )
    qr_payload = f"iot-alarm-pair:{raw_code}"
    image = qrcode.make(qr_payload, image_factory=SvgPathImage, border=3)
    buffer = io.BytesIO()
    image.save(buffer)
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return JsonResponse({
        "pairing_id": pairing.pk,
        "qr_payload": qr_payload,
        "qr_data_url": qr_data_url,
        "expires_at": pairing.expires_at.isoformat(),
        "expires_in": 120,
    })


@csrf_exempt
@require_POST
@transaction.atomic
def pair_device(request):
    body = _json_body(request)
    code = str(body.get("pairing_code", ""))
    device_id = str(body.get("device_id", ""))
    name = str(body.get("name", "Camera")).strip()[:120] or "Camera"
    if not DEVICE_ID_RE.fullmatch(device_id) or len(code) < 32:
        return JsonResponse({"error": "invalid_pairing_request"}, status=400)
    now = timezone.now()
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    pairing = PairingRequest.objects.select_for_update().select_related("owner").filter(
        token_hash=code_hash,
        consumed_at__isnull=True,
        expires_at__gt=now,
    ).first()
    if pairing is None or not pairing.owner.is_active:
        return JsonResponse({"error": "pairing_code_invalid_or_expired"}, status=403)

    device = Device.objects.filter(device_id=device_id).first()
    if device and device.owner_id not in (None, pairing.owner_id):
        return JsonResponse({"error": "device_already_owned"}, status=409)
    # Re-pairing an already-owned device is intentionally allowed. It rotates
    # the device token and recovers from a lost pairing response or replaced SD card.
    # Devices owned by another account are rejected by the ownership check above.
    if device is None:
        device = Device(device_id=device_id, kind=Device.RASPBERRY_PI)
    api_token = secrets.token_urlsafe(48)
    device.owner = pairing.owner
    device.name = name
    device.kind = Device.RASPBERRY_PI
    device.api_token_hash = make_password(api_token)
    device.stream_path = device_id
    device.is_online = True
    device.last_seen = now
    device.last_heartbeat = now
    device.save()
    pairing.device_id = device_id
    pairing.consumed_at = now
    pairing.save(update_fields=["device_id", "consumed_at"])
    return JsonResponse({
        "device_id": device_id, "api_token": api_token,
        "api_base_url": request.build_absolute_uri("/").rstrip("/"),
        "paired_at": now.isoformat(), "stream_path": device.stream_path,
    }, status=201)


@csrf_exempt
@device_required
@require_POST
def heartbeat(request):
    payload = _json_body(request)
    device = request.iot_device
    now = timezone.now()
    try:
        video_fps = max(0.0, min(120.0, float(payload.get("video_fps") or 0)))
        inference_fps = max(0.0, min(60.0, float(payload.get("inference_fps") or 0)))
        pending_alerts = max(0, int(payload.get("pending_alerts") or 0))
        pending_video_segments = max(0, int(payload.get("pending_video_segments") or 0))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid_device_metrics"}, status=400)
    device.is_online = True
    device.last_seen = device.last_heartbeat = now
    device.reported_video_fps = video_fps
    device.reported_inference_fps = inference_fps
    device.model_loaded = bool(payload.get("model_loaded", False))
    state = device.current_state if isinstance(device.current_state, dict) else {}
    state.update({
        "mqtt_connected": bool(payload.get("mqtt_connected", False)),
        "rtsp_connected": bool(payload.get("rtsp_connected", False)),
        "camera_ready": bool(payload.get("camera_ready", False)),
        "pending_alerts": pending_alerts,
        "pending_video_segments": pending_video_segments,
        "model": str(payload.get("model", "yolov5n-ncnn"))[:64],
        "software_version": str(payload.get("software_version", ""))[:64],
    })
    device.current_state = state
    device.save(update_fields=[
        "is_online", "last_seen", "last_heartbeat", "reported_video_fps",
        "reported_inference_fps", "model_loaded", "current_state", "updated_at",
    ])
    return JsonResponse({"status": "ok", "server_time": now.isoformat()})


@csrf_exempt
@device_required
@require_POST
def upload_alarm(request):
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_payload"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid_payload"}, status=400)
    device = request.iot_device
    alert_id = str(payload.get("alert_id", ""))
    if not re.fullmatch(r"[a-fA-F0-9]{16,64}", alert_id):
        return JsonResponse({"error": "invalid_alert_id"}, status=400)
    if payload.get("device_id") != device.device_id:
        return JsonResponse({"error": "device_identity_mismatch"}, status=403)
    snapshot = request.FILES.get("snapshot")
    if snapshot:
        if snapshot.size > 12 * 1024 * 1024 or (snapshot.content_type or "") != "image/jpeg":
            return JsonResponse({"error": "invalid_snapshot"}, status=413)
        try:
            snapshot.seek(0)
            with Image.open(snapshot) as image:
                if image.format != "JPEG" or image.width > 4096 or image.height > 4096:
                    return JsonResponse({"error": "invalid_snapshot_dimensions_or_format"}, status=400)
                image.verify()
            snapshot.seek(0)
        except Exception:
            return JsonResponse({"error": "snapshot_is_not_a_valid_jpeg"}, status=400)
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0)))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid_confidence"}, status=400)
    raw_boxes = payload.get("bbox", [])
    if not isinstance(raw_boxes, list) or len(raw_boxes) > 100:
        return JsonResponse({"error": "invalid_bounding_boxes"}, status=400)
    boxes = []
    try:
        for item in raw_boxes:
            if not isinstance(item, dict):
                raise ValueError
            box = {key: float(item[key]) for key in ("x1", "y1", "x2", "y2")}
            box_confidence = max(0.0, min(1.0, float(item.get("confidence", confidence))))
            if not all(0.0 <= box[key] <= 1.0 for key in box) or box["x2"] <= box["x1"] or box["y2"] <= box["y1"]:
                raise ValueError
            box["confidence"] = round(box_confidence, 4)
            boxes.append(box)
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "invalid_bounding_boxes"}, status=400)
    defaults = {
        "event_type": str(payload.get("event", "person_detected"))[:64],
        "confidence": confidence,
        "bbox": boxes,
        "detected_at": _parse_time(payload.get("detected_at")),
        "details": {"model": str(payload.get("model", "yolov5n-ncnn"))[:64], "source": "edge"},
    }
    alarm, created = Alarm.objects.get_or_create(device=device, alert_id=alert_id, defaults=defaults)
    if snapshot and not alarm.snapshot:
        alarm.snapshot.save(f"{alert_id}.jpg", snapshot, save=True)
    device.last_seen = timezone.now()
    device.save(update_fields=["last_seen", "updated_at"])
    return JsonResponse({"status": "stored", "alert_id": alert_id, "created": created}, status=201 if created else 200)


@csrf_exempt
@device_required
@require_POST
def upload_segment(request):
    device = request.iot_device
    uploaded = request.FILES.get("file")
    segment_id = str(request.POST.get("segment_id", ""))
    if not uploaded or not SEGMENT_ID_RE.fullmatch(segment_id):
        return JsonResponse({"error": "invalid_segment"}, status=400)
    if uploaded.size > 160 * 1024 * 1024 or (uploaded.content_type not in {"video/mp4", "application/octet-stream"}):
        return JsonResponse({"error": "invalid_or_oversized_video"}, status=413)
    header = uploaded.read(16)
    uploaded.seek(0)
    if len(header) < 12 or header[4:8] != b"ftyp":
        return JsonResponse({"error": "uploaded_file_is_not_an_mp4"}, status=400)
    expected_sha = str(request.POST.get("sha256", "")).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        return JsonResponse({"error": "missing_checksum"}, status=400)
    digest = hashlib.sha256()
    for block in uploaded.chunks():
        digest.update(block)
    if digest.hexdigest() != expected_sha:
        return JsonResponse({"error": "checksum_mismatch"}, status=400)
    uploaded.seek(0)
    segment = VideoSegment.objects.filter(device=device, segment_id=segment_id).first()
    if segment is not None:
        return JsonResponse({"status": "already_stored", "segment_id": segment_id, "created": False})
    try:
        duration = max(1, min(60, int(request.POST.get("duration_seconds") or 5)))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid_duration"}, status=400)
    segment = VideoSegment(
        device=device, segment_id=segment_id, started_at=_parse_time(request.POST.get("started_at")),
        duration_seconds=duration, checksum_sha256=expected_sha, size_bytes=uploaded.size,
    )
    segment.file.save(f"{segment_id}.mp4", uploaded, save=True)
    device.last_seen = timezone.now()
    device.save(update_fields=["last_seen", "updated_at"])
    return JsonResponse({"status": "stored", "segment_id": segment_id, "created": True}, status=201)


@csrf_exempt
@device_required
@require_POST
def device_log(request):
    payload = _json_body(request)
    event_id = str(payload.get("event_id", ""))[:64]
    if event_id and DeviceLog.objects.filter(device=request.iot_device, event_id=event_id).exists():
        return JsonResponse({"status": "already_stored"})
    DeviceLog.objects.create(
        device=request.iot_device, event_id=event_id,
        level=payload.get("level", "info") if payload.get("level") in {"debug", "info", "warning", "error"} else "info",
        category=str(payload.get("category", "device"))[:64],
        message=str(payload.get("message", "Device log"))[:500],
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
    )
    return JsonResponse({"status": "stored"})


@login_required
@require_POST
def create_stream_grant(request, device_id):
    device = get_object_or_404(Device, device_id=device_id)
    if not (request.user.is_staff or device.owner_id == request.user.id):
        return JsonResponse({"error": "forbidden"}, status=403)
    raw_token = secrets.token_urlsafe(36)
    StreamGrant.objects.create(
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        user=request.user, device=device,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return JsonResponse({"username": "stream", "token": raw_token, "expires_in": 600})


@csrf_exempt
@require_POST
def mediamtx_auth(request):
    """MediaMTX external auth hook for TLS-protected publishing and owner-scoped playback."""
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponse(status=403)
    username = str(payload.get("user", ""))
    password = str(payload.get("password", ""))
    action = str(payload.get("action", ""))
    path = str(payload.get("path", ""))
    # MediaMTX first probes without credentials for protocols such as RTSP.
    # Return 401 so it can issue its challenge and retry with Basic credentials.
    if not username or not password:
        return HttpResponse(status=401)

    if action == "publish":
        device = Device.objects.select_related("owner").filter(device_id=username, stream_path=path).first()
        if (
            device and device.owner_id is not None and device.owner.is_active
            and device.api_token_hash and check_password(password, device.api_token_hash)
        ):
            return HttpResponse(status=204)
        return HttpResponse(status=403)

    if action in {"read", "playback"} and username == "stream":
        now = timezone.now()
        token_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        grant = StreamGrant.objects.select_related("device", "device__owner", "user").filter(
            token_hash=token_hash,
            device__device_id=path,
            revoked_at__isnull=True,
            expires_at__gt=now,
            user__is_active=True,
            device__owner__is_active=True,
        ).first()
        if grant and (grant.device.owner_id == grant.user_id or grant.user.is_staff):
            # MediaMTX closes a stream after the short-lived grant expires.
            expires_at = grant.expires_at.isoformat().replace("+00:00", "Z")
            return JsonResponse({"expireAt": expires_at})
        return HttpResponse(status=403)

    return HttpResponse(status=403)


def _parse_time(value):
    if not value:
        return timezone.now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    except (ValueError, TypeError):
        return timezone.now()


@login_required
def pairing_status(request, pairing_id):
    pairing = get_object_or_404(PairingRequest, pk=pairing_id, owner=request.user)
    now = timezone.now()
    if pairing.consumed_at and pairing.device_id:
        return JsonResponse({
            "status": "paired",
            "device_id": pairing.device_id,
            "consumed_at": pairing.consumed_at.isoformat(),
        })
    if pairing.expires_at <= now:
        return JsonResponse({"status": "expired"})
    return JsonResponse({
        "status": "pending",
        "expires_at": pairing.expires_at.isoformat(),
    })
