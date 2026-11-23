"""Local Raspberry Pi web app. Run with: python -m pi_device.app"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from .bridge import RaspberryPiMqttBridge

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pi-device.web")

bridge = RaspberryPiMqttBridge()
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
LOCAL_WEB_PASSWORD = os.getenv("LOCAL_WEB_PASSWORD", "")


def authenticated(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "ابتدا وارد سایت محلی شوید."}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def ensure_csrf_token():
    if session.get("authenticated"):
        session.setdefault("csrf_token", secrets.token_urlsafe(24))


@app.get("/login/")
def login():
    return render_template("login.html", error=None)


@app.route("/login/", methods=["POST"])
def login_post():
    password = request.form.get("password", "")
    if not LOCAL_WEB_PASSWORD or LOCAL_WEB_PASSWORD.startswith("REPLACE_"):
        return render_template("login.html", error="ابتدا LOCAL_WEB_PASSWORD را در تنظیمات سرویس تعیین کن."), 503
    if hmac.compare_digest(password, LOCAL_WEB_PASSWORD):
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(24)
        return redirect(url_for("index"))
    return render_template("login.html", error="گذرواژه صحیح نیست."), 401


@app.post("/logout/")
@authenticated
def logout():
    if not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
        return "درخواست معتبر نیست", 403
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@authenticated
def index():
    return render_template("index.html", csrf_token=session["csrf_token"], device_id=bridge.device_id)


def validate_csrf():
    supplied = request.headers.get("X-CSRF-Token", "")
    return bool(supplied and hmac.compare_digest(supplied, session.get("csrf_token", "")))


@app.get("/api/state/")
@authenticated
def api_state():
    return jsonify(bridge.snapshot())


@app.post("/api/send/")
@authenticated
def api_send():
    if not validate_csrf():
        return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کنید."}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "بدنهٔ درخواست باید یک شیء JSON باشد."}), 400
    message_value = body.get("message", "")
    if not isinstance(message_value, str):
        return jsonify({"error": "متن پیام باید رشته باشد."}), 400
    message = message_value.strip()
    if not message or len(message) > 500:
        return jsonify({"error": "پیام باید بین ۱ تا ۵۰۰ نویسه باشد."}), 400
    ok, payload = bridge.send_text(message)
    if not ok:
        return jsonify({"error": "ارسال ناموفق بود. اتصال رزبری‌پای به VPS را بررسی کنید."}), 503
    return jsonify({"status": "published", "topic": bridge.topic_events, "qos": 1, "payload": payload})


@app.post("/api/sample/")
@authenticated
def api_sample():
    if not validate_csrf():
        return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کنید."}), 403
    ok, payload = bridge.send_sample()
    if not ok:
        return jsonify({"error": "ارسال داده ناموفق بود. اتصال MQTT را بررسی کنید."}), 503
    return jsonify({"status": "published", "topic": bridge.topic_telemetry, "qos": 1, "payload": payload})


@app.post("/api/output/")
@authenticated
def api_output():
    if not validate_csrf():
        return jsonify({"error": "درخواست معتبر نیست؛ صفحه را دوباره بارگذاری کنید."}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "بدنهٔ درخواست باید یک شیء JSON باشد."}), 400
    state = body.get("state")
    if state not in {"on", "off"}:
        return jsonify({"error": "state باید on یا off باشد."}), 400
    ok, payload = bridge.set_local_output(state)
    if not ok:
        return jsonify({"error": "ارسال وضعیت خروجی ناموفق بود."}), 503
    return jsonify({"status": "published", "topic": bridge.topic_events, "payload": payload})


@app.get("/health/")
def health():
    return jsonify({"status": "ok", "service": "raspberry-pi-local-web"})


if __name__ == "__main__":
    if not LOCAL_WEB_PASSWORD or LOCAL_WEB_PASSWORD.startswith("REPLACE_"):
        raise SystemExit("Set a strong LOCAL_WEB_PASSWORD in /etc/mqtt-iot-pi.env before starting the service.")
    if not app.secret_key or app.secret_key.startswith("REPLACE_"):
        raise SystemExit("Set a random FLASK_SECRET_KEY in /etc/mqtt-iot-pi.env before starting the service.")
    bridge.start()
    try:
        app.run(
            host=os.getenv("LOCAL_WEB_HOST", "0.0.0.0"),
            port=int(os.getenv("LOCAL_WEB_PORT", "8080")),
            debug=False,
            threaded=True,
        )
    finally:
        bridge.stop()
