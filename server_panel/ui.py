from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd
import streamlit as st

from .mqtt_control import publish_device_command, read_broker_metrics
from .repository import get_database_status, get_messages, get_snapshot, get_telemetry


PAGES = [
    "نمای کلی",
    "کنترل دستگاه",
    "تله‌متری و نمودارها",
    "مرکز پیام‌ها",
    "Broker و Topicها",
    "وضعیت سیستم",
]


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap');
        :root { color-scheme: dark; }
        html, body, [class*="css"], [data-testid="stAppViewContainer"] {
            font-family: 'Vazirmatn', Tahoma, sans-serif !important;
        }
        [data-testid="stAppViewContainer"] {
            background:
              radial-gradient(ellipse at 8% 0%, rgba(20, 91, 105, .22), transparent 34rem),
              radial-gradient(ellipse at 88% 8%, rgba(79, 65, 130, .13), transparent 36rem),
              #08111f;
            direction: rtl;
        }
        [data-testid="stHeader"] { background: rgba(8,17,31,.72); }
        [data-testid="stToolbar"] { right: auto; left: 1rem; }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #101e30 0%, #0c1726 100%);
            border-left: 1px solid rgba(145,174,198,.14);
            border-right: 0;
            direction: rtl;
        }
        [data-testid="stSidebar"] * { text-align: right; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #91a7bb; }
        .block-container { max-width: 1500px; padding-top: 1.7rem; padding-bottom: 3.5rem; }
        .stMarkdown, label, .stMetric, .stCaptionContainer { direction: rtl; text-align: right; }
        .stMarkdown p, .stMarkdown li { line-height: 1.9; }
        h1, h2, h3, h4 { letter-spacing: -.3px; }
        .stButton > button, .stDownloadButton > button {
            border-radius: 10px; border: 1px solid rgba(115,157,179,.25);
            font-weight: 600; min-height: 42px; transition: .16s ease;
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            border-color: #63d8c5; transform: translateY(-1px);
        }
        [data-testid="stMetric"] {
            background: linear-gradient(145deg, rgba(20,38,58,.98), rgba(13,27,43,.98));
            border: 1px solid rgba(123,158,187,.17);
            border-radius: 16px; padding: 16px 18px; min-height: 116px;
            box-shadow: 0 14px 34px rgba(0,0,0,.16);
        }
        [data-testid="stMetricLabel"] { color: #9aafc2 !important; font-size: .83rem; }
        [data-testid="stMetricValue"] { color: #edf7fc !important; font-weight: 750; }
        [data-testid="stMetricDelta"] { font-size: .78rem; }
        [data-testid="stDataFrame"], [data-testid="stTable"] {
            border: 1px solid rgba(123,158,187,.16); border-radius: 12px; overflow: hidden;
        }
        [data-testid="stExpander"] { border: 1px solid rgba(123,158,187,.18); border-radius: 12px; }
        .eyebrow { color: #68ddca; letter-spacing: 1.8px; font: 700 10px Arial,sans-serif; }
        .hero {
            border: 1px solid rgba(97,207,199,.19);
            background: linear-gradient(105deg, rgba(19,57,68,.8), rgba(17,31,49,.92) 58%, rgba(34,31,62,.74));
            border-radius: 22px; padding: 25px 28px; margin-bottom: 22px;
            box-shadow: 0 18px 45px rgba(0,0,0,.17);
        }
        .hero h1 { margin: 8px 0 7px; font-size: clamp(1.65rem, 3vw, 2.35rem); color: #f1f8fc; }
        .hero p { margin: 0; color: #a7bdcc; font-size: .92rem; line-height: 2; max-width: 820px; }
        .section-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin: 24px 0 12px; }
        .section-head h3 { margin:0; font-size:1.12rem; }
        .section-head span { color:#7894a9; font:9px Arial,sans-serif; letter-spacing:1.35px; }
        .soft-card {
            border:1px solid rgba(123,158,187,.17); background:linear-gradient(145deg,#14253a,#101c2c);
            border-radius:16px; padding:17px 19px; height:100%;
        }
        .soft-card .muted { color:#8fa5b9; font-size:.8rem; }
        .soft-card strong { color:#eff8ff; font-size:1.2rem; }
        .status-dot { display:inline-block; width:8px;height:8px;border-radius:50%;margin-left:8px;background:#55dc9b;box-shadow:0 0 12px #55dc9b; }
        .status-off { background:#f17d8c;box-shadow:0 0 10px #f17d8c; }
        code { direction:ltr; text-align:left; overflow-wrap:anywhere; }
        .login-wrap {
            max-width: 620px; margin: 7vh auto 16px; padding: 26px 30px;
            border:1px solid rgba(97,207,199,.2); border-radius:20px;
            background:linear-gradient(145deg,#14283a,#101b2b);
        }
        .login-wrap h1 { font-size:1.8rem; margin:8px 0; }
        .login-wrap p { color:#a0b4c6; }
        div[data-testid="stForm"] { border:1px solid rgba(123,158,187,.17); border-radius:15px; padding:18px; }
        .stAlert { border-radius:12px; }
        hr { border-color: rgba(123,158,187,.15); }
        @media (max-width: 700px) {
            .block-container { padding-top:1rem; }
            .hero { padding:19px; }
            .hero h1 { font-size:1.55rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _hero(title: str, description: str, eyebrow: str = "MQTT IOT EDGE-CLOUD") -> None:
    st.markdown(
        f"""
        <div class="hero">
          <div class="eyebrow">{eyebrow}</div>
          <h1>{title}</h1>
          <p>{description}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _section(title: str, hint: str = "") -> None:
    st.markdown(
        f'<div class="section-head"><h3>{title}</h3><span>{hint}</span></div>',
        unsafe_allow_html=True,
    )


def _fmt_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d  %H:%M:%S")
    except Exception:
        return str(value)


def _device(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    devices = snapshot.get("devices") or []
    return devices[0] if devices else None


def _device_status(device: dict[str, Any] | None) -> tuple[bool, str]:
    if not device:
        return False, "دستگاه ثبت نشده"
    return bool(device.get("is_online")), ("آنلاین" if device.get("is_online") else "آفلاین")


def _render_recent_messages(messages: list[dict[str, Any]], limit: int = 8) -> None:
    if not messages:
        st.info("هنوز پیامی از Raspberry Pi دریافت نشده است. سایت محلی رزبری‌پای را باز کن و یک پیام یا نمونه سنسور بفرست.")
        return
    rows = []
    for item in messages[:limit]:
        rows.append(
            {
                "کانال": item.get("channel", "—"),
                "Topic": item.get("topic", "—"),
                "محتوا": json.dumps(item.get("payload", {}), ensure_ascii=False),
                "QoS": item.get("qos", 0),
                "زمان دریافت": _fmt_time(item.get("received_at")),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True, height=min(370, 54 + 39 * len(rows)))


def _snapshot_or_error() -> dict[str, Any] | None:
    try:
        return get_snapshot()
    except Exception as exc:
        st.error(f"خواندن اطلاعات پایگاه داده ناموفق بود: {exc}")
        return None


def _render_overview() -> None:
    snapshot = _snapshot_or_error()
    if snapshot is None:
        return

    device = _device(snapshot)
    online, status = _device_status(device)
    broker = read_broker_metrics(timeout=1.0)
    broker_online = bool(broker.get("available"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("وضعیت Raspberry Pi", status, delta="دستگاه واقعی" if device else "منتظر اتصال")
    c2.metric("کل پیام‌های دریافت‌شده", f"{snapshot['total_messages']:,}")
    c3.metric("نمونه‌های تله‌متری", f"{snapshot['telemetry_count']:,}")
    c4.metric("وضعیت Broker", "متصل" if broker_online else "در دسترس نیست", delta="Mosquitto / MQTT" if broker_online else "اتصال داخلی ناموفق")

    if device:
        d1, d2 = st.columns([1.35, 1])
        with d1:
            _section("وضعیت دستگاه", "DEVICE STATUS")
            state = device.get("current_state") or {}
            output = state.get("output_state", state.get("state", "unknown"))
            output_label = ("روشن" if output == "on" else "خاموش") if output in {"on", "off"} else "نامشخص"
            st.markdown(
                f"""
                <div class="soft-card">
                  <div class="muted">Raspberry Pi 4 · {device.get('device_id')}</div>
                  <h2 style="margin:10px 0 5px;font-size:1.35rem">
                    <span class="status-dot {'status-off' if not online else ''}"></span>{status}
                  </h2>
                  <div class="muted">آخرین ارتباط: {_fmt_time(device.get('last_seen'))}</div>
                  <hr>
                  <div style="display:flex;justify-content:space-between;gap:12px">
                    <span class="muted">وضعیت آخرین خروجی گزارش‌شده</span><strong>{output_label}</strong>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with d2:
            _section("توزیع پیام‌ها", "MESSAGE BREAKDOWN")
            st.bar_chart(
                pd.DataFrame(
                    {
                        "تعداد": [
                            snapshot["event_count"],
                            snapshot["telemetry_count"],
                            snapshot["ack_count"],
                        ]
                    },
                    index=["Events", "Telemetry", "Acknowledgements"],
                ),
                width="stretch",
            )

    _section("تله‌متری اخیر", "LAST 24 HOURS")
    samples = get_telemetry(hours=24, limit=500)
    if samples:
        df = pd.DataFrame(samples)
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")
        chart1, chart2 = st.columns(2)
        with chart1:
            st.caption("دما · °C")
            if "temperature_c" in df:
                st.line_chart(df[["temperature_c"]], width="stretch")
        with chart2:
            st.caption("رطوبت · %")
            if "humidity_percent" in df:
                st.line_chart(df[["humidity_percent"]], width="stretch")
    else:
        st.info("هنوز داده تله‌متری ثبت نشده است. از سایت محلی Raspberry Pi گزینه «ارسال نمونه سنسور» را بزن.")

    _section("آخرین پیام‌ها", "LIVE MESSAGE STREAM")
    _render_recent_messages(snapshot["recent_messages"], limit=8)


def _render_device_control() -> None:
    _hero(
        "کنترل مستقیم دستگاه",
        "فرمان‌ها از پنل Streamlit به Mosquitto منتشر می‌شوند و Raspberry Pi از Topic اختصاصی خود آن‌ها را دریافت و تأیید می‌کند.",
        "DEVICE COMMAND CENTER",
    )
    snapshot = _snapshot_or_error()
    if snapshot is None:
        return
    device = _device(snapshot)
    online, status = _device_status(device)
    a, b = st.columns([1, 1])
    with a:
        st.metric("وضعیت دستگاه", status, delta="raspberry-pi-01")
    with b:
        st.metric("آخرین ارتباط", _fmt_time(device.get("last_seen") if device else None))
    if not device:
        st.warning("دستگاه هنوز در پایگاه داده ثبت نشده است.")
        return

    _section("کنترل خروجی", "GPIO / SIMULATED OUTPUT")
    st.caption("در حالت پیش‌فرض خروجی نرم‌افزاری است. برای LED واقعی، GPIO17 را روی Raspberry Pi فعال کن.")
    left, right = st.columns(2)
    with left:
        if st.button("روشن کردن خروجی", type="primary", width="stretch", key="output_on"):
            try:
                result = publish_device_command(device["device_id"], "set_output", state="on")
                st.success("فرمان روشن‌کردن با QoS 1 منتشر شد.")
                st.code(json.dumps(result, ensure_ascii=False, indent=2), language="json")
            except Exception as exc:
                st.error(f"ارسال فرمان ناموفق بود: {exc}")
    with right:
        if st.button("خاموش کردن خروجی", width="stretch", key="output_off"):
            try:
                result = publish_device_command(device["device_id"], "set_output", state="off")
                st.success("فرمان خاموش‌کردن با QoS 1 منتشر شد.")
                st.code(json.dumps(result, ensure_ascii=False, indent=2), language="json")
            except Exception as exc:
                st.error(f"ارسال فرمان ناموفق بود: {exc}")

    _section("ارسال پیام به Raspberry Pi", "CLOUD → DEVICE")
    with st.form("send_device_message", clear_on_submit=True):
        message = st.text_area("متن پیام", max_chars=500, placeholder="یک پیام کوتاه برای نمایش در صفحه محلی رزبری‌پای بنویس…")
        submitted = st.form_submit_button("ارسال پیام به دستگاه", type="primary", width="stretch")
    if submitted:
        try:
            result = publish_device_command(device["device_id"], "send_message", message=message)
            st.success("پیام از طریق MQTT به دستگاه ارسال شد.")
            st.json(result)
        except Exception as exc:
            st.error(f"ارسال پیام ناموفق بود: {exc}")

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("بررسی پاسخ دستگاه (Ping)", width="stretch"):
            try:
                result = publish_device_command(device["device_id"], "ping")
                st.success("درخواست Ping منتشر شد؛ پاسخ در بخش پیام‌ها با کانال ack ظاهر می‌شود.")
                st.json(result)
            except Exception as exc:
                st.error(f"ارسال Ping ناموفق بود: {exc}")
    with c2:
        st.info("پاسخ فرمان از طریق Topic مربوط به ack به Broker برمی‌گردد و توسط Django Listener در پایگاه داده ذخیره می‌شود.")


def _render_telemetry() -> None:
    _hero(
        "تله‌متری و تحلیل داده",
        "داده‌های دما و رطوبت ارسال‌شده از Raspberry Pi را در بازه‌های زمانی مختلف بررسی کن. داده فعلی برای دمو به‌صورت نرم‌افزاری تولید می‌شود.",
        "SENSOR TELEMETRY",
    )
    hours = st.select_slider(
        "بازه زمانی",
        options=[1, 6, 12, 24, 72, 168, 720],
        value=24,
        format_func=lambda value: f"{value} ساعت" if value < 24 else (f"{value // 24} روز" if value < 168 else ("یک هفته" if value == 168 else "۳۰ روز")),
    )
    samples = get_telemetry(hours=hours, limit=3000)
    if not samples:
        st.info("در بازه انتخاب‌شده داده‌ای وجود ندارد. از رابط محلی Pi داده نمونه ارسال کن.")
        return

    df = pd.DataFrame(samples)
    df["time"] = pd.to_datetime(df["time"])
    for column in ("temperature_c", "humidity_percent"):
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    valid_temp = df["temperature_c"].dropna() if "temperature_c" in df else pd.Series(dtype=float)
    valid_humidity = df["humidity_percent"].dropna() if "humidity_percent" in df else pd.Series(dtype=float)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("تعداد نمونه‌ها", f"{len(df):,}")
    m2.metric("میانگین دما", f"{valid_temp.mean():.1f} °C" if not valid_temp.empty else "—")
    m3.metric("بیشترین دما", f"{valid_temp.max():.1f} °C" if not valid_temp.empty else "—")
    m4.metric("میانگین رطوبت", f"{valid_humidity.mean():.1f} %" if not valid_humidity.empty else "—")

    if not valid_temp.empty:
        _section("نمودار دما", "TEMPERATURE · °C")
        temp_df = df.set_index("time")[["temperature_c"]].dropna()
        st.line_chart(temp_df, width="stretch")
    if not valid_humidity.empty:
        _section("نمودار رطوبت", "HUMIDITY · %")
        humidity_df = df.set_index("time")[["humidity_percent"]].dropna()
        st.area_chart(humidity_df, width="stretch")

    _section("داده‌های ثبت‌شده", "RAW TELEMETRY")
    display = df.copy()
    display["time"] = display["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    st.dataframe(
        display.rename(
            columns={
                "time": "زمان",
                "temperature_c": "دما (°C)",
                "humidity_percent": "رطوبت (%)",
                "device_id": "شناسه دستگاه",
                "topic": "Topic",
            }
        ),
        width="stretch",
        hide_index=True,
    )
    st.download_button(
        "دانلود داده‌ها به CSV",
        data=display.to_csv(index=False).encode("utf-8-sig"),
        file_name="mqtt-telemetry.csv",
        mime="text/csv",
    )


def _render_messages() -> None:
    _hero(
        "مرکز پیام‌ها",
        "تمام پیام‌هایی که Django Listener از Topicهای رویداد، تله‌متری و تأیید فرمان دریافت کرده است در این بخش قابل جست‌وجو هستند.",
        "MQTT MESSAGE EXPLORER",
    )
    col1, col2, col3 = st.columns([1, 1.4, .8])
    with col1:
        channel = st.selectbox(
            "نوع پیام",
            ["همه پیام‌ها", "events", "telemetry", "ack"],
            format_func=lambda value: {"همه پیام‌ها": "همه کانال‌ها", "events": "رویدادها", "telemetry": "تله‌متری", "ack": "تأیید فرمان"}[value],
        )
    with col2:
        search = st.text_input("جست‌وجو در Topic و Payload", placeholder="مثلاً temperature یا hello")
    with col3:
        limit = st.selectbox("تعداد نمایش", [50, 100, 300, 500], index=1)

    rows = get_messages(
        channel="all" if channel == "همه پیام‌ها" else channel,
        search=search,
        limit=limit,
    )
    st.caption(f"{len(rows)} پیام مطابق فیلتر فعلی")
    if not rows:
        st.info("پیامی مطابق جست‌وجوی تو پیدا نشد.")
        return

    frame = pd.DataFrame(
        [
            {
                "زمان": _fmt_time(row["received_at"]),
                "کانال": row["channel"],
                "دستگاه": row["device_id"],
                "Topic": row["topic"],
                "QoS": row["qos"],
                "Payload": json.dumps(row["payload"], ensure_ascii=False),
            }
            for row in rows
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True, height=480)
    st.download_button(
        "دانلود نتایج جست‌وجو (CSV)",
        data=frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="mqtt-messages.csv",
        mime="text/csv",
    )
    with st.expander("مشاهده Payload کامل یک پیام"):
        selected = st.selectbox(
            "انتخاب پیام",
            range(len(rows)),
            format_func=lambda index: f"{_fmt_time(rows[index]['received_at'])} · {rows[index]['channel']} · {rows[index]['topic']}",
        )
        st.json(rows[selected]["payload"])


def _render_broker() -> None:
    _hero(
        "وضعیت Broker و ساختار Topicها",
        "اتصال داخلی پنل به Mosquitto، آمار قابل دریافت از SYS و مسیرهای پیام‌رسانی پروژه را بررسی کن.",
        "MOSQUITTO BROKER"
    )
    report = read_broker_metrics(timeout=1.4)
    if report.get("available"):
        st.success("اتصال TCP داخلی به Mosquitto برقرار است.")
    else:
        st.error(f"Broker در دسترس نیست: {report.get('error') or 'خطای نامشخص'}")

    metrics = report.get("metrics", {})
    sys_map = {
        "$SYS/broker/uptime": "زمان فعالیت Broker",
        "$SYS/broker/clients/connected": "کلاینت‌های متصل",
        "$SYS/broker/messages/received": "پیام‌های ورودی",
        "$SYS/broker/messages/sent": "پیام‌های خروجی",
        "$SYS/broker/bytes/received": "بایت دریافتی",
        "$SYS/broker/bytes/sent": "بایت ارسالی",
    }
    cols = st.columns(3)
    for index, (topic, label) in enumerate(sys_map.items()):
        value = metrics.get(topic, "در انتظار آمار")
        cols[index % 3].metric(label, value)

    _section("نقشه Topicها", "MQTT TOPIC MAP")
    prefix = os.getenv("MQTT_TOPIC_PREFIX", "iotlab").strip("/")
    device_id = "raspberry-pi-01"
    topic_rows = [
        {"Topic": f"{prefix}/devices/{device_id}/events", "جهت": "Pi → Cloud", "کاربرد": "پیام متنی و رویداد"},
        {"Topic": f"{prefix}/devices/{device_id}/telemetry", "جهت": "Pi → Cloud", "کاربرد": "دما و رطوبت"},
        {"Topic": f"{prefix}/devices/{device_id}/status", "جهت": "Pi → Cloud", "کاربرد": "وضعیت آنلاین / آفلاین"},
        {"Topic": f"{prefix}/devices/{device_id}/command", "جهت": "Cloud → Pi", "کاربرد": "فرمان خروجی و پیام"},
        {"Topic": f"{prefix}/devices/{device_id}/ack", "جهت": "Pi → Cloud", "کاربرد": "تأیید اجرای فرمان"},
    ]
    st.dataframe(topic_rows, width="stretch", hide_index=True)
    st.markdown(
        """
        **تنظیمات امنیتی ارتباط**
        - ارتباط دستگاه از بیرون: WSS / TLS روی پورت 443 و مسیر /mqtt.
        - اتصال داخلی پنل و Listener: MQTT/TCP روی شبکه خصوصی Docker، پورت 1883.
        - احراز هویت Username/Password و محدودسازی دسترسی با Mosquitto ACL.
        - QoS پیش‌فرض پیام‌ها و فرمان‌ها: 1.
        """
    )
    with st.expander("آمار خام SYS"):
        if metrics:
            st.json(metrics)
        else:
            st.caption("Mosquitto هنوز آمار دوره‌ای SYS را ارسال نکرده است یا این آمار در دسترس نیست.")


def _render_system() -> None:
    _hero(
        "وضعیت و پیکربندی سیستم",
        "اطلاعات عملیاتی سرور و اتصال‌ها را بدون نمایش مقادیر محرمانه مشاهده کن.",
        "SYSTEM HEALTH"
    )
    db_ok, db_message = get_database_status()
    broker = read_broker_metrics(timeout=1.2)
    snapshot = _snapshot_or_error() or {}
    device = _device(snapshot)
    online, device_status = _device_status(device)

    c1, c2, c3 = st.columns(3)
    c1.metric("پایگاه داده", "سالم" if db_ok else "خطا")
    c2.metric("MQTT Broker", "متصل" if broker.get("available") else "قطع")
    c3.metric("Raspberry Pi", device_status)

    _section("سرویس‌ها", "SERVICE HEALTH")
    service_rows = [
        {"سرویس": "Streamlit Control Panel", "وضعیت": "فعال", "نقش": "رابط مدیریت سرور"},
        {"سرویس": "Django ORM / SQLite", "وضعیت": "سالم" if db_ok else "خطا", "نقش": "مدل‌ها و ذخیره پیام‌ها"},
        {"سرویس": "Mosquitto Broker", "وضعیت": "متصل" if broker.get("available") else "قطع", "نقش": "مسیریابی پیام MQTT"},
        {"سرویس": "Django MQTT Listener", "وضعیت": "داده دریافت شده" if snapshot.get("total_messages", 0) else "در انتظار پیام", "نقش": "دریافت و ثبت پیام‌های Pi"},
        {"سرویس": "Raspberry Pi 4", "وضعیت": device_status, "نقش": "ارسال سنسور و اجرای فرمان"},
    ]
    st.dataframe(service_rows, width="stretch", hide_index=True)

    _section("تنظیمات اتصال", "RUNTIME CONFIGURATION")
    config_rows = [
        {"پارامتر": "دامنه داشبورد", "مقدار": os.getenv("DOMAIN", "server.echonode.website")},
        {"پارامتر": "MQTT Broker (internal)", "مقدار": f"{os.getenv('MQTT_BROKER_HOST', 'broker')}:{os.getenv('MQTT_BROKER_PORT', '1883')}"},
        {"پارامتر": "MQTT external endpoint", "مقدار": f"wss://{os.getenv('DOMAIN', 'server.echonode.website')}{os.getenv('MQTT_WS_PATH', '/mqtt')}"},
        {"پارامتر": "MQTT Topic Prefix", "مقدار": os.getenv("MQTT_TOPIC_PREFIX", "iotlab")},
        {"پارامتر": "Device ID", "مقدار": "raspberry-pi-01"},
        {"پارامتر": "Storage", "مقدار": os.getenv("DATABASE_PATH", "/app/data/db.sqlite3")},
        {"پارامتر": "Panel account", "مقدار": os.getenv("PANEL_USERNAME", "not configured")},
        {"پارامتر": "Secrets", "مقدار": "مخفی · مقادیر گذرواژه نمایش داده نمی‌شوند"},
    ]
    st.dataframe(config_rows, width="stretch", hide_index=True)
    if not db_ok:
        st.error(f"Database error: {db_message}")
    if not broker.get("available"):
        st.warning("اتصال پنل به Broker برقرار نیست. وضعیت listener، شبکه داخلی Docker و اعتبار MQTT_SERVER_PASSWORD را بررسی کن.")


def render_application() -> None:
    with st.sidebar:
        st.markdown(
            """
            <div style="padding:8px 4px 18px">
              <div style="font:700 10px Arial,sans-serif;letter-spacing:2px;color:#63ddc9">ECHONODE / IOT LAB</div>
              <div style="font-size:20px;font-weight:800;color:#eef7fb;margin-top:7px">مرکز کنترل MQTT</div>
              <div style="font-size:10px;color:#7f97ab;margin-top:3px">Raspberry Pi 4 · Edge to Cloud</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("#### منوی اصلی")
        page = st.radio(
            "بخش موردنظر",
            PAGES,
            label_visibility="collapsed",
            format_func=lambda value: {
                "نمای کلی": "◈  نمای کلی",
                "کنترل دستگاه": "⌘  کنترل دستگاه",
                "تله‌متری و نمودارها": "⌁  تله‌متری و نمودارها",
                "مرکز پیام‌ها": "≋  مرکز پیام‌ها",
                "Broker و Topicها": "◎  Broker و Topicها",
                "وضعیت سیستم": "⚙  وضعیت سیستم",
            }[value],
        )
        st.divider()
        st.markdown(
            """
            <div style="font-size:11px;color:#91a8ba;line-height:2">
              <span style="color:#61dfc9">●</span> دامنه سرور<br>
              <b style="color:#e2eff8">server.echonode.website</b><br>
              اتصال دستگاه: <b style="color:#d9e7ef">MQTT / WSS</b><br>
              سطح اطمینان پیام: <b style="color:#d9e7ef">QoS 1</b>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()
        st.caption(f"حساب: {st.session_state.get('panel_username', 'operator')}")
        if st.button("خروج از حساب", width="stretch"):
            st.session_state.pop("panel_authenticated", None)
            st.session_state.pop("panel_username", None)
            st.rerun()

    _render_live_page(page)


@st.fragment(run_every="5s")
def _render_live_page(page: str) -> None:
    if page == "نمای کلی":
        _hero(
            "آزمایشگاه اینترنت اشیا",
            "نمای زنده ارتباط Raspberry Pi 4 با سرور مرکزی؛ دریافت پیام، مشاهده داده‌های سنسور و ارسال فرمان از طریق MQTT.",
            "MQTT IOT EDGE-CLOUD · CONTROL CENTER",
        )
        _render_overview()
    elif page == "کنترل دستگاه":
        _render_device_control()
    elif page == "تله‌متری و نمودارها":
        _render_telemetry()
    elif page == "مرکز پیام‌ها":
        _render_messages()
    elif page == "Broker و Topicها":
        _render_broker()
    else:
        _render_system()
