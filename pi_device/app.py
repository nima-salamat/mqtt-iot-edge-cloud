"""Password-protected local Raspberry Pi camera portal and service runtime."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import requests
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from waitress import serve

from .bridge import RaspberryPiMqttBridge
from .camera import CameraPipeline
from .settings_store import SettingsStore
from .system_stats import collect_system_stats
from .uploader import DurableUploader

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pi-device.web")


def load_device_id() -> str:
    configured = os.getenv("DEVICE_ID", "").strip()
    if configured:
        return configured
    data_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot"))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "device_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = f"cam-{secrets.token_hex(12)}"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return value


def load_credentials() -> dict:
    path = Path(os.getenv("DEVICE_CREDENTIALS_PATH", "/var/lib/mqttiot/device.json"))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def create_local_app(camera, settings_store, bridge, uploader) -> Flask:
    flask_app = Flask(__name__, template_folder="templates", static_folder="static")
    flask_app.secret_key = os.getenv("FLASK_SECRET_KEY", "")
    flask_app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    local_password = os.getenv("LOCAL_WEB_PASSWORD", "")

    def authenticated(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                if request.path.startswith("/api/") or request.path == "/preview.mjpg":
                    return jsonify({"error": "ابتدا وارد پنل محلی شوید."}), 401
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    @flask_app.before_request
    def ensure_csrf():
        if session.get("authenticated"):
            session.setdefault("csrf_token", secrets.token_urlsafe(24))

    def csrf_valid() -> bool:
        expected = session.get("csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token", "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    @flask_app.get("/login/")
    def login():
        return render_template("login.html", error=None)

    @flask_app.post("/login/")
    def login_post():
        password = request.form.get("password", "")
        if not local_password or local_password.startswith("REPLACE_"):
            return render_template(
                "login.html",
                error="ابتدا LOCAL_WEB_PASSWORD را در /etc/mqtt-iot-pi.env تنظیم کن.",
            ), 503
        if hmac.compare_digest(password, local_password):
            session.clear()
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(24)
            return redirect(url_for("index"))
        return render_template("login.html", error="گذرواژه صحیح نیست."), 401

    @flask_app.post("/logout/")
    @authenticated
    def logout():
        if not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
            return "درخواست معتبر نیست", 403
        session.clear()
        return redirect(url_for("login"))

    @flask_app.get("/")
    @authenticated
    def index():
        return render_template(
            "index.html",
            csrf_token=session["csrf_token"],
            device_id=camera.device_id,
            settings=settings_store.get(),
        )

    @flask_app.get("/api/cameras/")
    @authenticated
    def api_cameras():
        cameras = []
        try:
            from picamera2 import Picamera2
            for index, info in enumerate(Picamera2.global_camera_info()):
                cameras.append({
                    "backend": "picamera2",
                    "source": str(info.get("Num", index)),
                    "label": " / ".join(
                        str(value) for value in (
                            info.get("Model", "Raspberry Pi camera"),
                            info.get("Location", ""),
                            f"camera {info.get('Num', index)}",
                        ) if value
                    ),
                })
        except Exception:
            log.debug("Picamera2 camera discovery is unavailable", exc_info=True)

        for device in sorted(Path("/dev").glob("video*")):
            if device.name[5:].isdigit():
                cameras.append({
                    "backend": "opencv",
                    "source": str(device),
                    "label": f"USB / V4L2 - {device}",
                })

        # Keep a manual OpenCV index available even when device discovery is
        # restricted by permissions or the selected camera is a network URL.
        if not any(item["backend"] == "opencv" for item in cameras):
            cameras.append({
                "backend": "opencv",
                "source": "0",
                "label": "OpenCV default camera index 0",
            })
        return jsonify({
            "backends": [
                {"id": "picamera2", "label": "Raspberry Pi CSI / Picamera2"},
                {"id": "opencv", "label": "USB / UVC / V4L2 / network source (OpenCV)"},
            ],
            "cameras": cameras,
        })

    @flask_app.post("/api/disconnect/")
    @authenticated
    def disconnect_camera():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        try:
            result = uploader.disconnect_from_server()
            if camera.live_output:
                camera.live_output.credentials_changed()
            camera._pair_status = "not_paired"
            camera.bridge.publish_log("warning", "Camera access disconnected by local operator", {
                "device_id": camera.device_id,
            })
            return jsonify(result)
        except requests.RequestException as exc:
            log.exception("Could not revoke camera access on the server")
            return jsonify({"error": "ارتباط با سرور برای لغو اتصال برقرار نشد؛ اعتبارنامه محلی نگه داشته شد."}), 502
        except OSError as exc:
            log.exception("Could not clear local camera credentials")
            return jsonify({"error": "لغو اتصال انجام شد اما پاک‌کردن اعتبارنامه محلی موفق نبود."}), 500

    @flask_app.get("/api/state/")
    @authenticated
    def api_state():
        state = camera.snapshot()
        state["system"] = collect_system_stats(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot"))
        state["mqtt"] = bridge.snapshot()
        state["last_upload_at"] = uploader.last_upload_at
        state["last_video_upload_at"] = uploader.last_video_upload_at
        state["last_video_upload_error"] = uploader.last_video_upload_error
        state["last_heartbeat_at"] = uploader.last_heartbeat_at
        state["last_upload_error"] = uploader.last_upload_error
        state["uploaded_segments"] = uploader.uploaded_segments
        state["settings"] = settings_store.get()
        state["queues"] = uploader.queue_snapshot()
        return jsonify(state)

    @flask_app.get("/api/alarms/")
    @authenticated
    def local_alarms():
        alarm_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot")) / "alarms"
        items = []
        recent_paths = []
        for meta_path in alarm_dir.glob("*.json"):
            try:
                recent_paths.append((meta_path, meta_path.stat().st_mtime))
            except OSError:
                continue
        for meta_path, _modified_at in sorted(
            recent_paths, key=lambda item: item[1], reverse=True
        ):
            if not meta_path.is_file() or meta_path.is_symlink():
                continue
            if not re.fullmatch(r"[a-fA-F0-9]{32}", meta_path.stem):
                continue
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                image_path = alarm_dir / f"{meta_path.stem}.jpg"
                if not image_path.is_file() or image_path.is_symlink():
                    continue
                boxes = payload.get("bbox", [])
                if not isinstance(boxes, list):
                    boxes = []
                uploaded = meta_path.with_suffix(".uploaded").exists()
                published = meta_path.with_suffix(".published").exists()
                items.append({
                    "alert_id": meta_path.stem,
                    "event": str(payload.get("event", "person_detected")),
                    "detected_at": payload.get("detected_at"),
                    "confidence": payload.get("confidence"),
                    "bbox_count": len(boxes),
                    "bbox": boxes[:20],
                    "model": str(payload.get("model", "yolov5n-ncnn")),
                    "uploaded": uploaded,
                    "published": published,
                    "status": "published" if published else ("server_saved" if uploaded else "pending"),
                    "image_url": url_for("local_alarm_image", alert_id=meta_path.stem),
                })
                if len(items) >= 12:
                    break
            except (OSError, ValueError, TypeError):
                log.exception("Could not read local alarm metadata %s", meta_path.name)
        return jsonify({"alarms": items})

    @flask_app.delete("/api/alarms/<alert_id>/")
    @authenticated
    def delete_local_alarm(alert_id):
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        try:
            result = uploader.delete_alarm(alert_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "deleted", **result})

    @flask_app.post("/api/alarms/cleanup/")
    @authenticated
    def cleanup_local_alarms():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        result = uploader.clear_all_local_alarms()
        return jsonify({"status": "cleaned", **result})

    @flask_app.get("/alarms/<alert_id>.jpg")
    @authenticated
    def local_alarm_image(alert_id):
        if not re.fullmatch(r"[a-fA-F0-9]{32}", alert_id):
            abort(404)
        alarm_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot")) / "alarms"
        meta_path = alarm_dir / f"{alert_id}.json"
        image_path = alarm_dir / f"{alert_id}.jpg"
        if (
            not meta_path.is_file() or meta_path.is_symlink()
            or not image_path.is_file() or image_path.is_symlink()
        ):
            abort(404)
        return send_file(
            image_path,
            mimetype="image/jpeg",
            conditional=True,
            max_age=0,
        )

    @flask_app.post("/api/queue/clear/")
    @authenticated
    def clear_queue():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "نوع صف مشخص نشده است."}), 400
        queue_name = str(payload.get("queue", ""))
        try:
            result = uploader.clear_pending_queue(queue_name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "cleared", **result})

    @flask_app.get("/preview.mjpg")
    @authenticated
    def preview():
        def frames():
            last_frame = None
            while True:
                frame = camera.get_preview()
                if frame and frame != last_frame:
                    last_frame = frame
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + frame + b"\r\n"
                    )
                time.sleep(0.04)

        return Response(
            frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"},
        )

    @flask_app.post("/api/pair/scan/")
    @authenticated
    def pair_scan():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        return jsonify(camera.request_pairing())

    @flask_app.post("/api/pair/manual/")
    @authenticated
    def pair_manual():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "کد اتصال ارسال نشده است."}), 400
        try:
            result = camera.submit_manual_pairing_code(payload.get("pairing_code", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(result), 202

    @flask_app.post("/api/recording/")
    @authenticated
    def update_recording():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            return jsonify({"error": "وضعیت ضبط باید true یا false باشد."}), 400
        try:
            result = camera.set_recording_enabled(payload["enabled"])
        except (RuntimeError, TimeoutError, OSError) as exc:
            log.exception("Could not change local video recording state")
            return jsonify({"error": str(exc)}), 409
        return jsonify({"status": "updated", **result})

    @flask_app.post("/api/settings/")
    @authenticated
    def update_settings():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "مقادیر تنظیمات باید JSON باشند."}), 400
        allowed = {
            "video_fps", "video_fps_auto", "video_bitrate", "segment_seconds", "inference_fps", "inference_workers",
            "camera_backend", "camera_source", "video_width", "video_height",
            "alert_confidence", "alert_cooldown_seconds", "alert_clear_seconds",
            "local_video_retention_minutes", "upload_queue_retention_hours",
            "upload_queue_max_gb", "local_alarm_retention_days", "local_log_retention_days",
        }
        clean = {key: value for key, value in payload.items() if key in allowed}

        # The active camera mode list is authoritative. Reject an unsupported
        # resolution before persisting it, unless the same request changes
        # camera backend/source (those capabilities are only known after restart).
        current = settings_store.get()
        source_changed = (
            str(clean.get("camera_backend", current["camera_backend"])) != current["camera_backend"]
            or str(clean.get("camera_source", current["camera_source"])) != current["camera_source"]
        )
        if not source_changed and camera.available_video_modes and (
            "video_width" in clean or "video_height" in clean
        ):
            width = int(clean.get("video_width", current["video_width"]))
            height = int(clean.get("video_height", current["video_height"]))
            compatible_modes = [
                mode for mode in camera.available_video_modes
                if int(mode.get("size", (0, 0))[0]) >= width
                and int(mode.get("size", (0, 0))[1]) >= height
                and float(mode.get("fps", 0) or 0) > 0
            ]
            if not compatible_modes:
                resolutions = sorted({
                    f'{int(mode["size"][0])}x{int(mode["size"][1])}'
                    for mode in camera.available_video_modes
                    if mode.get("size")
                })
                return jsonify({
                    "error": (
                        f"وضوح {width}×{height} توسط دوربین فعال پشتیبانی نمی‌شود. "
                        "یکی از وضوح‌های زیر را انتخاب کن: " + "، ".join(resolutions)
                    )
                }), 400

        values, restart_required = settings_store.update(clean)
        return jsonify({
            "status": "saved",
            "settings": values,
            "restart_required": restart_required,
        })

    @flask_app.get("/health/")
    def health():
        return jsonify({"status": "ok", "service": "raspberry-pi-alarm-camera"})

    return flask_app


def main() -> None:
    if not os.getenv("LOCAL_WEB_PASSWORD") or os.getenv("LOCAL_WEB_PASSWORD", "").startswith("REPLACE_"):
        raise SystemExit("Set a strong LOCAL_WEB_PASSWORD in /etc/mqtt-iot-pi.env.")
    if not os.getenv("FLASK_SECRET_KEY") or os.getenv("FLASK_SECRET_KEY", "").startswith("REPLACE_"):
        raise SystemExit("Set a random FLASK_SECRET_KEY in /etc/mqtt-iot-pi.env.")

    device_id = load_device_id()
    settings_store = SettingsStore()
    bridge = RaspberryPiMqttBridge(device_id)
    uploader = DurableUploader(device_id, settings_store, bridge, load_credentials)
    camera = CameraPipeline(device_id, settings_store, bridge, uploader)

    bridge.start()
    uploader.start()
    # The Pi Camera is required for normal operation. A failed first open is
    # logged and retried so the local diagnostics page still comes up.
    camera_supervisor_stop = threading.Event()

    def camera_supervisor():
        startup_at = time.monotonic()
        while not camera_supervisor_stop.is_set():
            if not camera.is_running():
                try:
                    camera.start()
                    startup_at = time.monotonic()
                    camera.camera_error = ""
                except Exception as exc:
                    camera.camera_error = str(exc)
                    log.exception("Camera startup failed; retrying in 10 seconds")
                    camera_supervisor_stop.wait(10)
                    continue
            else:
                last_frame = camera.latest_frame_at
                stale_for = (time.time() - last_frame) if last_frame else (time.monotonic() - startup_at)
                if stale_for > 15:
                    log.error("Picamera2 produced no fresh frame for %.1f seconds; restarting camera", stale_for)
                    camera.stop()
                    startup_at = time.monotonic()
            camera_supervisor_stop.wait(5)

    camera_supervisor_thread = threading.Thread(
        target=camera_supervisor, name="camera-supervisor", daemon=True
    )
    camera_supervisor_thread.start()

    app = create_local_app(camera, settings_store, bridge, uploader)
    try:
        serve(
            app,
            host=os.getenv("LOCAL_WEB_HOST", "0.0.0.0"),
            port=int(os.getenv("LOCAL_WEB_PORT", "8080")),
            threads=8,
            ident="iot-pi-camera",
        )
    finally:
        camera_supervisor_stop.set()
        camera_supervisor_thread.join(timeout=6)
        camera.stop()
        uploader.stop()
        bridge.stop()


if __name__ == "__main__":
    main()
