"""Password-protected local Raspberry Pi camera portal and service runtime."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from waitress import serve

from .bridge import RaspberryPiMqttBridge
from .camera import CameraPipeline
from .settings_store import SettingsStore
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

    @flask_app.get("/api/state/")
    @authenticated
    def api_state():
        state = camera.snapshot()
        state["mqtt"] = bridge.snapshot()
        state["last_upload_at"] = uploader.last_upload_at
        state["last_heartbeat_at"] = uploader.last_heartbeat_at
        state["last_upload_error"] = uploader.last_upload_error
        state["uploaded_segments"] = uploader.uploaded_segments
        state["settings"] = settings_store.get()
        return jsonify(state)

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
                time.sleep(0.05)

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

    @flask_app.post("/api/settings/")
    @authenticated
    def update_settings():
        if not csrf_valid():
            return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کن."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "مقادیر تنظیمات باید JSON باشند."}), 400
        allowed = {
            "video_fps", "video_bitrate", "segment_seconds", "inference_fps",
            "alert_confidence", "alert_cooldown_seconds", "alert_clear_seconds",
            "local_video_retention_minutes", "upload_queue_retention_hours",
            "upload_queue_max_gb", "local_alarm_retention_days", "local_log_retention_days",
        }
        clean = {key: value for key, value in payload.items() if key in allowed}
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
    def camera_supervisor():
        while True:
            if camera.picam2 is None:
                try:
                    camera.start()
                    camera.camera_error = ""
                except Exception as exc:
                    camera.camera_error = str(exc)
                    log.exception("Camera startup failed; retrying in 10 seconds")
                    time.sleep(10)
                    continue
            time.sleep(5)

    threading.Thread(target=camera_supervisor, name="camera-supervisor", daemon=True).start()

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
        camera.stop()
        uploader.stop()
        bridge.stop()


if __name__ == "__main__":
    main()
